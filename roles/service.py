"""RoleManagerService — the single entry point for every role-management action.

Both the Discord slash commands and Angela call these methods. There is no
second code path, so there is no second place for an authorization check to be
forgotten.

Every mutating method follows the same shape:

    1. resolve the ACTING user's authoritative record from their Discord ID
    2. resolve the TARGET record
    3. ask permissions.check() — never a Discord role, never the caller's claim
    4. validate the input
    5. write Google Sheets (authoritative; this is the point of no return)
    6. enqueue outbound synchronization and return success

Step 6 never affects step 5's outcome. A Discord or Roblox failure is reported
and retried; the sheet keeps what it was told.
"""

import asyncio

from roles import config as roles_config
from roles import layout, loa, permissions, roblox_oauth, rover, timezones
from roles import state as roles_state
from roles.config import Permission
from roles.errors import RoleManagerError, SyncError
from roles.models import (
    ActionContext,
    ActionOrigin,
    ActionResult,
    ChangeSource,
    Status,
    SyncStatus,
    UserRecord,
    format_entry_date,
    format_identification,
    new_record_uid,
)
from roles.repository import PersonnelRepository
from roles.sync_queue import SyncQueue


class RoleManagerService:
    """Business logic for user record management. Stateless between calls."""

    def __init__(self, repository=None, sync_queue=None, gateway=None):
        if gateway is None:
            from roles import sheets_gateway
            gateway = sheets_gateway
        self.gateway = gateway
        self.repository = repository or PersonnelRepository(gateway)
        self.sync_queue = sync_queue or SyncQueue(self.repository)
        self._started = False

    # ── Lifecycle ──

    async def start(self, discord_client=None):
        """Load the sheet, start the sync worker. Safe to call once per process."""
        if self._started:
            return

        from roles.discord_sync import synchronizer as discord_synchronizer
        if discord_client is not None:
            discord_synchronizer.set_client(discord_client)

        self.sync_queue.load_failures()
        await self.repository.load()
        # After the first load, so a revocation is cancelled if the row is back.
        self.sync_queue.load_removals()
        self.sync_queue.start()
        self._started = True

        cfg = roles_config.current()
        records = self.repository.live_records()
        print(f"[Roles] Service started: {len(records)} record(s), "
              f"footer row {self.repository.footer_row}.")

        missing = cfg.missing_configuration()
        if missing:
            print("[Roles] Awaiting configuration:")
            for item in missing:
                print(f"[Roles]   - {item}")

    # ── Lookups ──

    def actor_record(self, context):
        """The acting user's authoritative record. The basis for all authority."""
        return self.repository.by_discord_id(context.actor_discord_id)

    def find_record(self, discord_id):
        return self.repository.by_discord_id(discord_id)

    async def get_record(self, context, target_discord_id=None):
        """Read one record, subject to VIEW_RECORD."""
        actor = self.actor_record(context)
        target_id = target_discord_id or context.actor_discord_id
        target = self.repository.by_discord_id(target_id)

        decision = permissions.check(
            Permission.VIEW_RECORD, actor, target,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, Permission.VIEW_RECORD, decision, target))

        if not decision.allowed:
            return ActionResult.denied(decision.reason, permission=Permission.VIEW_RECORD)

        if target is None:
            return ActionResult.not_found(
                f"No PERSONNEL record found for <@{target_id}>."
            )

        return ActionResult.success(self._describe_record(target), record=target)

    # ── Registration ──

    def _registration_preflight(self, context):
        """Checks common to both verification methods, run before any external call.

        Returns an ActionResult to abort with, or None to continue.
        """
        cfg = roles_config.current()
        actor = self.actor_record(context)

        decision = permissions.check(
            Permission.REGISTER_SELF, actor, actor,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, Permission.REGISTER_SELF, decision, actor))

        if not decision.allowed:
            return ActionResult.denied(decision.reason, permission=Permission.REGISTER_SELF)

        if cfg.discord.main_guild_id and context.guild_id != cfg.discord.main_guild_id:
            return ActionResult.invalid(
                "Registration must be done in the main designated server."
            )

        if actor is not None:
            return ActionResult.invalid(
                f"You are already registered as {actor.label()}.",
                record_uid=actor.record_uid,
            )

        return None

    async def register(self, context, discord_username):
        """Begin registration.

        With OAuth this returns an authorization URL that the caller MUST
        deliver privately — it is placed in `detail`, never in `message`, so an
        interface has to opt into showing it. Whoever opens the link binds their
        Roblox account to this Discord ID.

        With Rover this completes in one step, as before.
        """
        cfg = roles_config.current()

        abort = self._registration_preflight(context)
        if abort is not None:
            return abort

        if cfg.registration.verification_method == "ROVER":
            return await self._register_via_rover(context, discord_username)

        auth_url, state_or_error = roblox_oauth.verifier.begin(
            context.actor_discord_id, context.guild_id, discord_username
        )
        if auth_url is None:
            return ActionResult.error(state_or_error)

        print(f"{context.log_prefix()} Registration started for "
              f"{context.actor_discord_id} (OAuth).")

        return ActionResult.success(
            "Authorize with Roblox to finish registering. The link is private to "
            "you and expires shortly — do not share it, or you will link someone "
            "else's account.",
            auth_url=auth_url,
            requires_private_delivery=True,
        )

    async def complete_registration(self, state, code):
        """Finish an OAuth registration from the callback.

        Re-runs every safeguard rather than trusting the preflight: time has
        passed since /register, so the user may have registered by another route
        and the Roblox account may have been claimed in the meantime.
        """
        result = await roblox_oauth.verifier.complete(state, code)
        if not result.ok:
            return ActionResult.invalid(
                result.error or "Verification failed.", retryable=result.retryable,
            )

        context = ActionContext(
            actor_discord_id=result.discord_id,
            origin=ActionOrigin.SYSTEM,
            guild_id=result.guild_id,
        )

        await self.repository.load()

        abort = self._registration_preflight(context)
        if abort is not None:
            return abort

        return await self._create_record(
            context,
            roblox_id=result.roblox_id,
            roblox_username=result.roblox_username,
            discord_username=result.discord_username or str(result.discord_id),
        )

    async def _register_via_rover(self, context, discord_username):
        """Legacy one-step registration through the Rover registry."""
        cfg = roles_config.current()

        rover_result = await rover.lookup_roblox(
            context.actor_discord_id, cfg.discord.main_guild_id
        )
        if not rover_result.ok:
            return ActionResult.invalid(
                f"Rover verification failed: {rover_result.error}",
                retryable=rover_result.retryable,
            )

        return await self._create_record(
            context,
            roblox_id=rover_result.roblox_id,
            roblox_username=rover_result.roblox_username,
            discord_username=discord_username,
        )

    async def _registration_rank(self, roblox_id, cfg):
        """Pick the starting rank from Roblox group membership.

        Already in the group -> `registration.in_group_rank`.
        Not in the group     -> `default_registration_rank`, and they are told
                                to join in order to be ranked properly.

        Returns (rank_key, in_group) where in_group is True / False / None.
        None means the check could not be run; the default rank is used, because
        quietly granting the higher rank on an API outage would be worse than
        making someone ask for a promotion.
        """
        default_rank = (
            cfg.rank(cfg.default_registration_rank).key
            if cfg.default_registration_rank and cfg.rank(cfg.default_registration_rank)
            else ""
        )

        target = cfg.registration.in_group_rank
        if not target or not cfg.rank(target):
            # No in-group rank configured: skip the check entirely.
            return default_rank, None

        from roles.roblox_sync import synchronizer as roblox_synchronizer
        in_group = await roblox_synchronizer.is_group_member(roblox_id, cfg)

        if in_group is True:
            print(f"[Roles] {roblox_id} is in the Roblox group -> {cfg.rank(target).key}")
            return cfg.rank(target).key, True

        if in_group is False:
            print(f"[Roles] {roblox_id} is NOT in the Roblox group -> {default_rank}")
        else:
            print(f"[Roles] Roblox group membership for {roblox_id} could not be "
                  f"verified; using {default_rank}.")

        return default_rank, in_group

    @staticmethod
    def _group_join_prompt(in_group, cfg):
        """What to tell a registrant about group membership, if anything."""
        if in_group is True or in_group is None:
            return ""

        url = cfg.roblox.group_url or (
            f"https://www.roblox.com/groups/{cfg.roblox.group_id}"
            if cfg.roblox.group_id else ""
        )
        where = f" ({url})" if url else ""
        return (
            f":warning: You are not in our Roblox group yet, so you have been "
            f"registered as an applicant. **Join the group**{where} — once you "
            f"have been accepted you will be moved up automatically and receive "
            f"the access that comes with it. There is no need to register again."
        )

    async def _create_record(self, context, roblox_id, roblox_username, discord_username):
        """Create the PERSONNEL record once a Roblox identity is verified."""
        cfg = roles_config.current()

        existing = self.repository.by_roblox_id(roblox_id)
        if existing is not None:
            return ActionResult.invalid(
                f"Roblox account {roblox_id} is already on record as "
                f"{existing.label()}. Contact an administrator if this is wrong."
            )

        rank_key, in_group = await self._registration_rank(roblox_id, cfg)

        record = UserRecord(
            record_uid=new_record_uid(),
            discord_id=context.actor_discord_id,
            roblox_id=roblox_id,
            # Column D: "RobloxName (discordname)".
            username=format_identification(roblox_username, discord_username)
                     or str(roblox_id),
            # Column K is set once, here, and never touched again.
            entry_date=format_entry_date(),
            status=Status.ACTIVE,
            rank_key=rank_key,
            branch_keys=cfg.normalize_branch_keys([cfg.registration_branch_key()]),
            sync_status=SyncStatus.NEVER,
            source=ChangeSource.SYSTEM,
        )

        try:
            async with self.gateway.write_lock:
                await self.repository.load()
                saved = await self.repository.save_record(record)
        except RoleManagerError as exc:
            return ActionResult.error(f"Could not create the record: {exc}")

        job = self.sync_queue.enqueue(saved, reason="registration")

        # Deliberately says nothing about the sheet row or section: where a record
        # physically sits is an implementation detail that moves on every layout
        # pass, and it means nothing to the member reading this.
        rank = cfg.rank(saved.rank_key)
        message = (
            f"Registered. Roblox account **{roblox_username or roblox_id}** "
            f"linked as **{rank.display if rank else 'unranked'}**, "
            f"entry date **{saved.entry_date}**."
        )
        prompt = self._group_join_prompt(in_group, cfg)
        if prompt:
            message += "\n\n" + prompt

        return ActionResult.success(
            message,
            record=saved,
            queued_targets=["DISCORD", "ROBLOX"] if job else [],
            roblox_id=roblox_id,
            in_roblox_group=in_group,
        )

    async def promote_group_joiners(self, record_uids):
        """Move applicants who are now in the Roblox group up to the in-group rank.

        The second half of the registration rule. /register hands out
        `in_group_rank` to someone already in the group and the applicant rank
        to everyone else; this is what happens when one of those applicants is
        accepted afterwards, so the outcome no longer depends on whether they
        joined the group before or after registering.

        A system action — there is no actor and no permission check, for the
        same reason /register itself needs no rank to assign one. Its reach is
        correspondingly narrow: it only ever moves a record that STILL holds
        the applicant rank, and only to the one configured rank.

        Caller must hold `gateway.write_lock` and have refreshed the snapshot
        under that same hold (see PersonnelRepository.update_usernames for why
        this does not reload by itself). Returns the promoted records.
        """
        cfg = roles_config.current()
        pair = cfg.applicant_promotion()
        if pair is None:
            return []
        applicant, joined = pair

        promoted = []
        for uid in record_uids:
            current = self.repository.by_uid(uid)
            # Re-checked against current state: the membership answer is a few
            # seconds old, and a staff member may have ranked them meanwhile.
            if current is None or current.rank_key != applicant.key:
                continue

            saved = await self.repository.save_record(current.with_changes(
                rank_key=joined.key,
                category=joined.category or current.category,
            ))
            self.sync_queue.enqueue(saved, reason="joined the Roblox group")
            promoted.append(saved)
            print(f"[Roles] {saved.username or saved.discord_id} joined the Roblox "
                  f"group: {applicant.key} -> {joined.key} (rev {saved.revision}).")

        return promoted

    # ── Field mutations ──

    async def set_timezone(self, context, raw_timezone, target_discord_id=None):
        """Set column E to a canonical UTC offset."""
        target_id = target_discord_id or context.actor_discord_id

        allowed, target, denial = self._authorize(
            context, Permission.SET_TIMEZONE, target_id
        )
        if not allowed:
            return denial

        result = timezones.normalize(raw_timezone)
        if not result.ok:
            return ActionResult.invalid(result.error)

        updated = target.with_changes(timezone=result.canonical)
        saved = await self._commit(updated, "set timezone")

        # Timezone does not affect Discord or Roblox roles, so no sync job.
        return ActionResult.success(
            f"Timezone for {saved.label()} set to **{timezones.describe(result.canonical)}**.",
            record=saved,
        )

    async def set_rank(self, context, rank_input, target_discord_id):
        """Set column F. Moves the user between category sections as needed."""
        cfg = roles_config.current()

        allowed, target, denial = self._authorize(
            context, Permission.SET_RANK, target_discord_id
        )
        if not allowed:
            return denial

        rank = cfg.rank(rank_input) or cfg.rank_by_display(rank_input)
        if rank is None:
            available = ", ".join(sorted(r.display for r in cfg.ranks.values())) or "none configured yet"
            return ActionResult.invalid(
                f"`{rank_input}` is not a configured rank. Available: {available}."
            )

        # Being allowed to re-rank this member is not being allowed to give them
        # any rank: the actor's own rank is the ceiling on what they hand out.
        ceiling = permissions.check_assignable_rank(
            self.actor_record(context), rank, cfg,
            actor_discord_id=context.actor_discord_id,
        )
        if not ceiling.allowed:
            print(f"{context.log_prefix()} AUTHZ DENY {Permission.SET_RANK} "
                  f"actor={context.actor_discord_id} rank={rank.key} :: {ceiling.reason}")
            return ActionResult.denied(ceiling.reason, permission=Permission.SET_RANK)

        if target.rank_key == rank.key:
            return ActionResult.invalid(
                f"{target.label()} already holds **{rank.display}**."
            )

        previous_rank = target.rank_key
        updated = target.with_changes(
            rank_key=rank.key,
            category=rank.category or target.category,
        )
        saved = await self._commit(updated, f"set rank {previous_rank or '-'} -> {rank.key}")

        job = self.sync_queue.enqueue(saved, reason="rank change")
        placement = layout.describe_placement(saved, self.repository.live_records(), cfg)

        return ActionResult.success(
            f"{saved.username or saved.discord_id} is now **{rank.display}** "
            f"({placement}). Discord and Roblox synchronization queued.",
            record=saved,
            queued_targets=["DISCORD", "ROBLOX"] if job else [],
            previous_rank=previous_rank,
        )

    async def set_branches(self, context, branch_inputs, target_discord_id):
        """Set column G: every branch the member belongs to.

        REPLACES the member's branches with exactly the ones given. Someone in
        ECHELON who is set to INTELLIGENCE ends up in INTELLIGENCE only; to be
        in both, both have to be given. "N/A" on its own takes the member out of
        every branch.

        `branch_inputs` is a list of branch names or keys; a single string, or
        entries holding several comma-separated names, are accepted too.
        """
        cfg = roles_config.current()

        allowed, target, denial = self._authorize(
            context, Permission.SET_BRANCH, target_discord_id
        )
        if not allowed:
            return denial

        if isinstance(branch_inputs, str):
            branch_inputs = [branch_inputs]
        requested = [
            part.strip()
            for raw in (branch_inputs or ())
            for part in str(raw or "").split(",")
            if part.strip()
        ]
        if not requested:
            return ActionResult.invalid("No branch given.")

        resolved, unknown = [], []
        for name in requested:
            branch = cfg.branch(name) or cfg.branch_by_display(name)
            if branch is None:
                unknown.append(name)
            else:
                resolved.append(branch)

        if unknown:
            available = ", ".join(sorted(b.display for b in cfg.branches.values())) or "none configured yet"
            names = ", ".join(f"`{name}`" for name in unknown)
            return ActionResult.invalid(
                f"{names} {'is' if len(unknown) == 1 else 'are'} not a configured "
                f"branch. Available: {available}."
            )

        real = [b for b in resolved if not b.is_na]
        if real and len(real) < len(resolved):
            return ActionResult.invalid(
                "N/A means no branch, so it cannot be combined with other branches. "
                "Give N/A on its own to take someone out of every branch."
            )

        new_keys = cfg.normalize_branch_keys(b.key for b in real)
        new_display = cfg.branches_display(new_keys) or "N/A"
        name = target.username or target.discord_id

        if new_keys == target.branch_keys:
            return ActionResult.invalid(f"{name} is already assigned to **{new_display}**.")

        previous = target.branch_keys
        changes = {"branch_keys": new_keys}
        primary_note = ""
        if target.primary_line and target.primary_line not in new_keys:
            # The line their Roblox role came from belongs to a branch they
            # just left, so the choice no longer makes sense.
            changes["primary_line"] = ""
            primary_note = (
                " Their Roblox primary role is back on the mainline, since they "
                "left the branch it came from."
            )

        saved = await self._commit(
            target.with_changes(**changes),
            f"set branches {','.join(previous) or 'N/A'} -> {','.join(new_keys) or 'N/A'}",
        )

        job = self.sync_queue.enqueue(saved, reason="branch change")
        replaced = (
            f" (replacing {cfg.branches_display(previous)})" if previous else ""
        )

        return ActionResult.success(
            f"{name} is now in **{new_display}**{replaced}. Discord and Roblox "
            f"synchronization queued.{primary_note}",
            record=saved,
            queued_targets=["DISCORD", "ROBLOX"] if job else [],
            previous_branches=list(previous),
        )

    # ── Roblox primary role ──

    @staticmethod
    def _line_label(cfg, line):
        if not line:
            return "mainline (N/A)"
        branch = cfg.branches.get(line)
        return f"{branch.display if branch else line} line"

    async def set_primary_role(self, context, line_input, target_discord_id=None):
        """Choose which line the member's single Roblox role comes from.

        The mainline (the rank's "N/A" role) or the line of one of the member's
        own branches, if that branch maps a Roblox role at their rank. Refused
        while branch lines are switched off.
        """
        cfg = roles_config.current()
        target_id = target_discord_id or context.actor_discord_id

        allowed, target, denial = self._authorize(
            context, Permission.SET_PRIMARY_ROLE, target_id
        )
        if not allowed:
            return denial

        rank = cfg.rank(target.rank_key)
        enabled = roles_state.branch_roles_enabled(cfg)
        name = target.username or target.discord_id

        raw = str(line_input or "").strip()
        if raw.upper() in ("", "N/A", "NA", "MAINLINE", "MAIN", "NONE", "DEFAULT"):
            line = ""
        else:
            branch = cfg.branch(raw) or cfg.branch_by_display(raw)
            if branch is None:
                return ActionResult.invalid(
                    f"`{raw}` is not a branch. Choose the mainline (N/A) or one of "
                    f"{name}'s branches."
                )
            line = "" if branch.is_na else branch.key

        if line:
            branch = cfg.branches[line]
            if not enabled:
                return ActionResult.invalid(
                    "Branch-line Roblox roles are currently disabled, so everyone "
                    "holds their mainline role."
                )
            if rank is None or not rank.syncs_roblox:
                return ActionResult.invalid(
                    f"{rank.display if rank else 'That rank'} has no Roblox role, so "
                    f"there is no primary role to choose."
                )
            if line not in target.branch_keys:
                return ActionResult.invalid(
                    f"{name} is not in **{branch.display}**, so its line is not "
                    f"available to them."
                )
            if line not in cfg.primary_line_options(rank, target.branch_keys, enabled):
                return ActionResult.invalid(
                    f"**{branch.display}** has no Roblox role at the rank of "
                    f"**{rank.display}**, so only the mainline is available there."
                )

        if line == target.primary_line:
            return ActionResult.invalid(
                f"{name}'s Roblox primary role is already the "
                f"**{self._line_label(cfg, line)}**."
            )

        saved = await self._commit(
            target.with_changes(primary_line=line),
            f"set primary line {target.primary_line or 'N/A'} -> {line or 'N/A'}",
        )
        job = self.sync_queue.enqueue(saved, reason="primary role change", targets=["ROBLOX"])

        return ActionResult.success(
            f"{name}'s Roblox primary role is now the **{self._line_label(cfg, line)}**. "
            f"Roblox synchronization queued.",
            record=saved,
            queued_targets=["ROBLOX"] if job else [],
            primary_line=line,
        )

    async def set_branch_roles_enabled(self, context, enabled):
        """Switch branch-line Roblox roles on or off for everyone.

        Off: members can no longer choose a branch line, and everyone currently
        on one is moved back to the mainline — on the sheet, not just in effect,
        so switching back on later does not silently restore old choices.
        """
        actor = self.actor_record(context)
        decision = permissions.check(
            Permission.TOGGLE_BRANCH_ROLES, actor, actor,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, Permission.TOGGLE_BRANCH_ROLES, decision))
        if not decision.allowed:
            return ActionResult.denied(decision.reason, permission=Permission.TOGGLE_BRANCH_ROLES)

        cfg = roles_config.current()
        if roles_state.branch_roles_enabled(cfg) == bool(enabled):
            return ActionResult.invalid(
                f"Branch-line Roblox roles are already {'enabled' if enabled else 'disabled'}."
            )

        # Switched first: from here on nobody can pick a branch line, and the
        # synchronizer treats every choice as the mainline even before the
        # sheet catches up.
        roles_state.set_branch_roles_enabled(enabled, context.actor_discord_id)
        print(f"{context.log_prefix()} Branch-line Roblox roles "
              f"{'ENABLED' if enabled else 'DISABLED'} by {context.actor_discord_id}.")

        if enabled:
            # Nobody's choice changed, but a line set by hand on the sheet while
            # this was off takes effect now and needs pushing.
            chosen = [r for r in self.repository.live_records()
                      if r.primary_line and not r.is_incomplete()]
            self.sync_queue.enqueue_many(chosen, reason="branch roles enabled")
            return ActionResult.success(
                "Branch-line Roblox roles are **enabled**. Members can switch their "
                "primary role with `/set primary`; everyone stays on the mainline "
                "until they do.",
                enabled=True,
            )

        moved, note = [], ""
        try:
            async with self.gateway.write_lock:
                await self.repository.load()
                on_branch_line = [
                    r for r in self.repository.live_records()
                    if r.primary_line and not r.is_incomplete()
                ]
                moved = await self.repository.save_records(
                    [r.with_changes(primary_line="") for r in on_branch_line]
                )
        except (RoleManagerError, SyncError) as exc:
            # The switch already holds, so their Roblox role still goes back to
            # the mainline; only the sheet column is behind.
            on_branch_line = [r for r in self.repository.live_records() if r.primary_line]
            moved = on_branch_line
            note = (f"\n\n⚠️ The sheet could not be updated ({exc}); their stored "
                    f"choice will be cleared on a later attempt, but their Roblox "
                    f"role is moved to the mainline regardless.")

        self.sync_queue.enqueue_many(moved, reason="branch roles disabled")
        print(f"[Roles] Branch roles disabled: {len(moved)} member(s) moved to the mainline.")

        return ActionResult.success(
            f"Branch-line Roblox roles are **disabled**. {len(moved)} member(s) "
            f"moved back to their mainline role; Roblox synchronization queued. "
            f"Nobody can switch to a branch line until this is enabled again.{note}",
            enabled=False,
            moved=len(moved),
        )

    # ── Grantable roles ──

    async def grant_role(self, context, grantable_input, target_discord_id):
        """Give a member a grantable role, on top of their rank."""
        return await self._change_grant(context, grantable_input, target_discord_id, give=True)

    async def revoke_role(self, context, grantable_input, target_discord_id):
        """Take a grantable role back."""
        return await self._change_grant(context, grantable_input, target_discord_id, give=False)

    async def _change_grant(self, context, grantable_input, target_discord_id, give):
        """Add or remove one grantable role on a record.

        A grantable role is a modifier, not a rank: column F, the category and
        the member's standing in the permission system are untouched. Authority
        to hand one out is exactly the authority to set the member's rank.
        """
        cfg = roles_config.current()

        allowed, target, denial = self._authorize(
            context, Permission.SET_RANK, target_discord_id
        )
        if not allowed:
            return denial

        grantable = cfg.grantable(grantable_input) or cfg.grantable_by_display(grantable_input)
        raw_key = str(grantable_input or "").strip().upper()

        if grantable is None:
            # A role that has since been deleted from the configuration can
            # still be sitting on a record. It must stay revocable.
            if not give and raw_key in target.grants:
                key, display = raw_key, raw_key
            else:
                available = ", ".join(
                    sorted(g.display for g in cfg.grantable_roles.values())
                ) or "none configured yet"
                return ActionResult.invalid(
                    f"`{grantable_input}` is not a configured grantable role. "
                    f"Available: {available}."
                )
        else:
            key, display = grantable.key, grantable.display

        name = target.username or target.discord_id

        if give:
            if key in target.grants:
                return ActionResult.invalid(f"{name} already holds **{display}**.")
            if not cfg.grantable_guild_ids(grantable):
                return ActionResult.invalid(
                    f"**{display}** cannot be granted yet: its guild "
                    f"`{', '.join(grantable.guilds)}` does not resolve to a "
                    f"configured server."
                )
        elif key not in target.grants:
            return ActionResult.invalid(f"{name} does not hold **{display}**.")

        def apply(current):
            held = set(current.grants)
            if give:
                held.add(key)
            else:
                held.discard(key)
            return current.with_changes(grants=tuple(sorted(held)))

        verb = "grant" if give else "revoke"
        saved = await self._commit(target, f"{verb} {key}", transform=apply)

        job = self.sync_queue.enqueue(saved, reason=f"{verb} {key}")

        if give:
            effect = []
            if grantable.add_discord_role_ids:
                effect.append(f"adds {len(grantable.add_discord_role_ids)} role(s)")
            if grantable.remove_discord_role_ids:
                effect.append(f"removes {len(grantable.remove_discord_role_ids)} role(s)")
            where = cfg.grantable_guild_label(grantable)
            message = (
                f"{name} now holds **{display}** "
                f"({', '.join(effect) or 'no roles configured'} in {where}). "
                f"Their rank is unchanged. Discord synchronization queued."
            )
        else:
            message = (
                f"**{display}** revoked from {name}. Their rank is unchanged. "
                f"Discord synchronization queued."
            )

        return ActionResult.success(
            message,
            record=saved,
            queued_targets=["DISCORD"] if job else [],
            grantable=key,
        )

    async def set_status(self, context, status_input, target_discord_id=None):
        """Set column J."""
        target_id = target_discord_id or context.actor_discord_id

        allowed, target, denial = self._authorize(
            context, Permission.SET_STATUS, target_id
        )
        if not allowed:
            return denial

        status = Status.parse(status_input)
        if status is None:
            return ActionResult.invalid(
                f"`{status_input}` is not a valid status. "
                f"Use ACTIVE, SEMI-ACTIVE or IN-ACTIVE."
            )

        if status is Status.EMPTY:
            return ActionResult.invalid(
                "EMPTY is a spreadsheet-maintenance state, not a user status. "
                "It cannot be set on a person."
            )

        updated = target.with_changes(status=status)
        saved = await self._commit(updated, f"set status {status.value}")

        return ActionResult.success(
            f"Status for {saved.label()} set to **{status.value}**.", record=saved,
        )

    async def set_loa(self, context, raw_value, target_discord_id=None):
        """Set the LOA payload.

        Stored opaquely in a technical column: the `#/#/#` semantics have not
        been defined, so only the shape is validated. See roles/loa.py.
        """
        target_id = target_discord_id or context.actor_discord_id

        allowed, target, denial = self._authorize(context, Permission.SET_LOA, target_id)
        if not allowed:
            return denial

        parsed = loa.parse(raw_value)
        if not parsed.ok:
            return ActionResult.invalid(parsed.error)

        updated = target.with_changes(loa_raw=parsed.value.to_storage())

        implied = loa.implied_status(parsed.value)
        if implied is not None:
            updated = updated.with_changes(status=implied)

        saved = await self._commit(updated, "set LOA")

        if not parsed.value.raw:
            return ActionResult.success(
                f"LOA cleared for {saved.label()}.", record=saved,
            )

        return ActionResult.success(
            f"LOA for {saved.label()} recorded as {loa.describe(parsed.value)}.",
            record=saved,
        )

    # ── Administrative ──

    async def force_sync(self, context, target_discord_id=None, clean=False):
        """Re-push a record (or everyone) to Discord and Roblox.

        `clean=True` (one member only) first wipes their Discord roles and
        rebuilds them from the record — see DiscordSynchronizer.clean_sync_record.
        """
        if clean and not target_discord_id:
            return ActionResult.invalid(
                "A clean sync strips and rebuilds one member's roles, so it needs a "
                "user. It is not available for everyone at once."
            )

        actor = self.actor_record(context)
        target = (
            self.repository.by_discord_id(target_discord_id)
            if target_discord_id else None
        )

        decision = permissions.check(
            Permission.FORCE_SYNC, actor, target or actor,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, Permission.FORCE_SYNC, decision, target))

        if not decision.allowed:
            return ActionResult.denied(decision.reason, permission=Permission.FORCE_SYNC)

        await self.repository.load()

        if target_discord_id:
            target = self.repository.by_discord_id(target_discord_id)
            if target is None:
                return ActionResult.not_found(
                    f"No PERSONNEL record for <@{target_discord_id}>."
                )
            if clean:
                return await self._clean_sync(context, target)
            self.sync_queue.enqueue(target, reason="forced")
            return ActionResult.success(
                f"Synchronization queued for {target.label()}.", record=target,
            )

        records = self.repository.live_records()
        jobs = self.sync_queue.enqueue_many(records, reason="forced (all)")
        return ActionResult.success(
            f"Synchronization queued for {len(jobs)} record(s)."
        )

    async def _clean_sync(self, context, target):
        """Run a clean Discord sync now and report it server by server.

        Done directly rather than queued, so the person who asked sees what was
        removed and given back. A normal sync is queued straight after: it
        brings Roblox up to date, retries any server the clean pass could not
        reach, and records the result on the sheet.
        """
        from roles.discord_sync import synchronizer as discord_synchronizer

        cfg = roles_config.current()
        dry_run = not cfg.sync.enabled
        print(f"{context.log_prefix()} CLEAN SYNC of {target.label()} "
              f"requested by {context.actor_discord_id}{' (dry run)' if dry_run else ''}.")

        outcomes = await discord_synchronizer.clean_sync_record(target, cfg, dry_run)
        job = self.sync_queue.enqueue(target, reason="clean sync follow-up")

        lines = [f"**Clean sync for {target.username or target.discord_id}**"
                 + (" (dry run — sync is disabled, nothing was changed)" if dry_run else "")]
        for outcome in outcomes:
            mark = "✅" if outcome.ok else "⚠️"
            lines.append(f"{mark} {outcome.message}")
        lines.append("Roblox roles and anything that failed above are handled by the "
                     "normal sync queued after this.")

        failed = [o for o in outcomes if not o.ok]
        if failed and len(failed) == len(outcomes):
            return ActionResult.error("\n".join(lines))
        return ActionResult.success(
            "\n".join(lines), record=target,
            queued_targets=["DISCORD", "ROBLOX"] if job else [],
        )

    async def restructure(self, context, purge_incomplete=False):
        """Re-sort and resize the managed area.

        `purge_incomplete=True` also clears rows that were entered by hand and
        carry no bot identity - the one-off cleanup that prepares a pre-existing
        sheet for first use. Destructive, so it is opt-in.
        """
        actor = self.actor_record(context)

        decision = permissions.check(
            Permission.RESTRUCTURE_SHEET, actor, actor,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, Permission.RESTRUCTURE_SHEET, decision))

        if not decision.allowed:
            return ActionResult.denied(decision.reason, permission=Permission.RESTRUCTURE_SHEET)

        await self.repository.load()
        incomplete = [r for r in self.repository.live_records() if r.is_incomplete()]

        # Say what would be destroyed rather than destroying it unasked.
        if incomplete and not purge_incomplete:
            preview = ", ".join(
                (r.username or f"row {r.row}") for r in incomplete[:10]
            )
            more = f" (+{len(incomplete) - 10} more)" if len(incomplete) > 10 else ""
            return ActionResult.invalid(
                f"**{len(incomplete)} incomplete row(s)** have no Discord ID or "
                f"record UID. They were entered by hand, so the system cannot "
                f"look them up, authorize them or synchronize them.\n\n"
                f"{preview}{more}\n\n"
                f"Re-run with `purge_incomplete: True` to clear them and prepare "
                f"the sheet. **This deletes those rows.** Take a backup first.",
                incomplete=len(incomplete),
            )

        try:
            async with self.gateway.write_lock:
                plan, purged = await self.repository.restructure(
                    purge_incomplete=purge_incomplete
                )
        except RoleManagerError as exc:
            return ActionResult.error(f"Restructure failed: {exc}")

        if purged:
            names = ", ".join((r.username or f"row {r.row}") for r in purged[:15])
            more = f" (+{len(purged) - 15} more)" if len(purged) > 15 else ""
            return ActionResult.success(
                f"Purged **{len(purged)}** incomplete row(s) and restructured the "
                f"sheet.\n{plan.summary()}.\n\nRemoved: {names}{more}\n\n"
                f"The sheet is now ready for use.",
                purged=len(purged),
            )

        return ActionResult.success(f"Sheet restructured: {plan.summary()}.")

    async def inspect(self, context):
        """Integrity and synchronization report."""
        actor = self.actor_record(context)

        decision = permissions.check(
            Permission.ADMIN_INSPECT, actor, actor,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, Permission.ADMIN_INSPECT, decision))

        if not decision.allowed:
            return ActionResult.denied(decision.reason, permission=Permission.ADMIN_INSPECT)

        await self.repository.load()
        report = self.repository.integrity_report()
        stats = self.sync_queue.stats()

        lines = []
        if not report["sheet_ready"]:
            lines.append(
                f"⚠️ **Sheet not ready for use** — {len(report['incomplete'])} row(s) "
                f"have no Discord ID or record UID. Run `/roles restructure "
                f"purge_incomplete: True` to clear them."
            )
            lines.append("")

        lines += [
            f"**Records:** {report['records']}  •  footer row {report['footer_row']}",
            f"**Incomplete:** {len(report['incomplete'])}",
            f"**Empty rows:** {report['empty_rows']}",
            f"**Misplaced:** {len(report['misplaced'])}",
            f"**Duplicates:** {len(report['duplicates'])}",
            f"**Unknown ranks:** {len(report['unknown_ranks'])}",
            f"**Unknown grantable roles:** {len(report['unknown_grants'])}",
            f"**Unknown branches:** {len(report['unknown_branches'])}",
            f"**Branch-line Roblox roles:** {roles_state.describe()}",
            f"**Awaiting sync:** {len(report['unsynced'])}",
            f"**Sync queue:** {stats['queued']} queued, "
            f"{stats['dead_letters']} failed, worker "
            f"{'running' if stats['worker_running'] else 'stopped'}",
        ]

        if stats.get("pending_removals"):
            lines.append(
                f"⚠️ **{stats['pending_removals']} incomplete revocation(s)** — "
                f"removed users whose roles have not been fully stripped."
            )

        missing = roles_config.current().missing_configuration()
        if missing:
            lines.append("")
            lines.append("**Awaiting configuration:**")
            lines.extend(f"• {item}" for item in missing)

        return ActionResult.success("\n".join(lines), detail=report)

    # ── Internals ──

    def _authorize(self, context, permission, target_discord_id):
        """Resolve actor + target and run the permission check.

        Returns (allowed, target_record, denial_result).
        """
        actor = self.actor_record(context)
        target = self.repository.by_discord_id(target_discord_id)

        decision = permissions.check(
            permission, actor, target,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, permission, decision, target))

        if not decision.allowed:
            return False, target, ActionResult.denied(
                decision.reason, permission=permission,
                unconfigured=decision.unconfigured,
            )

        if target is None:
            return False, None, ActionResult.not_found(
                f"No PERSONNEL record for <@{target_discord_id}>. "
                f"They need to /register first."
            )

        return True, target, None

    async def _commit(self, record, reason, transform=None):
        """Write one record authoritatively, under the sheet write lock.

        Reloads first so the write is applied to current state rather than to a
        snapshot that may have aged while the command was being processed.

        `transform` is for changes that are relative to the current value
        instead of a replacement of it — adding one grantable role to the set a
        member holds. It receives the freshly read record and returns the one
        to save, so two such changes arriving together both land instead of the
        second overwriting the first.

        Callers must not await between reading the target from the snapshot and
        calling this: `before` below has to be the snapshot they changed.
        """
        # The copy the caller started from, taken before anything can await and
        # let a poll replace the snapshot. Comparing against it is what tells
        # the intended change apart from fields that are merely stale.
        before = self.repository.by_uid(record.record_uid)

        async with self.gateway.write_lock:
            await self.repository.load()

            current = self.repository.by_uid(record.record_uid)
            if current is None:
                raise RoleManagerError(
                    "The record disappeared from the sheet before the change could be saved."
                )

            if transform is not None:
                merged = transform(current)
            else:
                # Re-apply only the fields the caller changed onto the freshly
                # read row. Comparing against `current` instead would also copy
                # every field edited on the sheet since the snapshot was taken
                # back to its old value.
                base = before or current
                merged = current.with_changes(**{
                    field: getattr(record, field)
                    for field in ("rank_key", "branch_keys", "timezone", "status",
                                  "loa_raw", "username", "category", "primary_line")
                    if getattr(record, field) != getattr(base, field)
                })

            saved = await self.repository.save_record(merged)

        print(f"[Roles] {reason}: {saved.label()} (rev {saved.revision})")
        return saved

    def _describe_record(self, record):
        cfg = roles_config.current()
        rank = cfg.rank(record.rank_key)
        category = layout.effective_category(record, cfg)
        enabled = roles_state.branch_roles_enabled(cfg)
        line = cfg.effective_primary_line(
            rank, record.branch_keys, record.primary_line, enabled
        )
        primary = self._line_label(cfg, line)
        if record.primary_line and record.primary_line != line:
            reason = ("branch lines are disabled" if not enabled
                      else "not available at this rank or branch")
            primary += (f" (chose the {self._line_label(cfg, record.primary_line)}, "
                        f"{reason})")
        options = cfg.primary_line_options(rank, record.branch_keys, enabled)

        lines = [
            f"**{record.username or record.discord_id}**",
            f"Rank: {rank.display if rank else (record.rank_key or 'unset')}",
            f"Branches: {cfg.branches_display(record.branch_keys) or 'N/A'}",
            f"Roblox primary role: {primary}"
            + (f"  •  lines available: mainline, "
               f"{', '.join(cfg.branches[k].display for k in options)}" if options else ""),
            f"Category: {category.value if category else 'unassigned'}",
            f"Status: {record.status.value if record.status else 'unset'}",
            f"Timezone: {timezones.describe(record.timezone) if record.timezone else 'unset'}",
            f"Date of entry: {record.entry_date or 'unset'}",
            f"Row: {record.row}",
            f"Discord ID: {record.discord_id or 'unset'}  •  Roblox ID: {record.roblox_id or 'unset'}",
            f"Sync: {record.sync_status.value}"
            + (f" ({record.last_sync_at})" if record.last_sync_at else ""),
        ]
        if record.loa_raw:
            lines.append(f"LOA: {loa.describe(loa.LOAValue.from_storage(record.loa_raw))}")
        if record.grants:
            names = []
            for key in record.grants:
                grantable = cfg.grantable(key)
                names.append(grantable.display if grantable else f"{key} (not configured)")
            lines.append(f"Grantable roles: {', '.join(names)}")
        return "\n".join(lines)


#: Process-wide instance, shared by the Discord commands, the poller and Angela.
service = RoleManagerService()
