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
from roles.config import Permission
from roles.errors import RoleManagerError
from roles.models import (
    ActionContext,
    ActionOrigin,
    ActionResult,
    ChangeSource,
    Status,
    SyncStatus,
    UserRecord,
    format_entry_date,
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

    async def _create_record(self, context, roblox_id, roblox_username, discord_username):
        """Create the PERSONNEL record once a Roblox identity is verified."""
        cfg = roles_config.current()

        existing = self.repository.by_roblox_id(roblox_id)
        if existing is not None:
            return ActionResult.invalid(
                f"Roblox account {roblox_id} is already on record as "
                f"{existing.label()}. Contact an administrator if this is wrong."
            )

        record = UserRecord(
            record_uid=new_record_uid(),
            discord_id=context.actor_discord_id,
            roblox_id=roblox_id,
            discord_username=discord_username,
            # Column K is set once, here, and never touched again.
            entry_date=format_entry_date(),
            status=Status.ACTIVE,
            # TODO: set `default_registration_rank` in roles_config.json once the
            # rank definitions arrive. Until then a new record gets a blank rank,
            # which sorts to the bottom of its section and synchronizes nothing —
            # deliberately inert rather than a guessed starting rank.
            rank_key=(
                cfg.rank(cfg.default_registration_rank).key
                if cfg.default_registration_rank and cfg.rank(cfg.default_registration_rank)
                else ""
            ),
            branch_key=roles_config.NA_BRANCH_KEY,
            sync_status=SyncStatus.NEVER,
            source=ChangeSource.SYSTEM,
        )

        try:
            async with self.gateway.write_lock:
                await self.repository.load()
                saved = await self.repository.save_record(record)
        except RoleManagerError as exc:
            return ActionResult.error(f"Could not create the record: {exc}")

        placement = layout.describe_placement(
            saved, self.repository.live_records(), cfg
        )
        job = self.sync_queue.enqueue(saved, reason="registration")

        return ActionResult.success(
            f"Registered. Roblox account **{roblox_username or roblox_id}** "
            f"linked, entry date **{saved.entry_date}**, placed at {placement}.",
            record=saved,
            queued_targets=["DISCORD", "ROBLOX"] if job else [],
            roblox_id=roblox_id,
        )

    # ── Field mutations ──

    async def set_timezone(self, context, raw_timezone, target_discord_id=None):
        """Set column E to a canonical IANA identifier."""
        target_id = target_discord_id or context.actor_discord_id

        allowed, target, denial = self._authorize(
            context, Permission.SET_TIMEZONE, target_id
        )
        if not allowed:
            return denial

        result = timezones.normalize(raw_timezone)
        if result.ambiguous:
            options = ", ".join(f"`{c}`" for c in result.candidates)
            return ActionResult.invalid(
                f"`{raw_timezone}` is ambiguous. Did you mean one of: {options}?",
                candidates=result.candidates,
            )
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
            f"{saved.discord_username or saved.discord_id} is now **{rank.display}** "
            f"({placement}). Discord and Roblox synchronization queued.",
            record=saved,
            queued_targets=["DISCORD", "ROBLOX"] if job else [],
            previous_rank=previous_rank,
        )

    async def set_branch(self, context, branch_input, target_discord_id):
        """Set column G. Affects branch Discord roles and the Roblox mapping."""
        cfg = roles_config.current()

        allowed, target, denial = self._authorize(
            context, Permission.SET_BRANCH, target_discord_id
        )
        if not allowed:
            return denial

        branch = cfg.branch(branch_input) or cfg.branch_by_display(branch_input)
        if branch is None:
            available = ", ".join(sorted(b.display for b in cfg.branches.values())) or "none configured yet"
            return ActionResult.invalid(
                f"`{branch_input}` is not a configured branch. Available: {available}."
            )

        if target.branch_key == branch.key:
            return ActionResult.invalid(
                f"{target.label()} is already assigned to **{branch.display}**."
            )

        previous_branch = target.branch_key
        updated = target.with_changes(branch_key=branch.key)
        saved = await self._commit(updated, f"set branch {previous_branch or '-'} -> {branch.key}")

        job = self.sync_queue.enqueue(saved, reason="branch change")

        return ActionResult.success(
            f"{saved.discord_username or saved.discord_id} moved to branch "
            f"**{branch.display}**. Discord and Roblox synchronization queued.",
            record=saved,
            queued_targets=["DISCORD", "ROBLOX"] if job else [],
            previous_branch=previous_branch,
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

    async def force_sync(self, context, target_discord_id=None):
        """Re-push a record (or everyone) to Discord and Roblox."""
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
            self.sync_queue.enqueue(target, reason="forced")
            return ActionResult.success(
                f"Synchronization queued for {target.label()}.", record=target,
            )

        records = self.repository.live_records()
        jobs = self.sync_queue.enqueue_many(records, reason="forced (all)")
        return ActionResult.success(
            f"Synchronization queued for {len(jobs)} record(s)."
        )

    async def restructure(self, context):
        """Re-sort and resize the managed area."""
        actor = self.actor_record(context)

        decision = permissions.check(
            Permission.RESTRUCTURE_SHEET, actor, actor,
            actor_discord_id=context.actor_discord_id, origin=context.origin,
        )
        print(permissions.audit_line(context, Permission.RESTRUCTURE_SHEET, decision))

        if not decision.allowed:
            return ActionResult.denied(decision.reason, permission=Permission.RESTRUCTURE_SHEET)

        try:
            async with self.gateway.write_lock:
                plan = await self.repository.restructure()
        except RoleManagerError as exc:
            return ActionResult.error(f"Restructure failed: {exc}")

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

        lines = [
            f"**Records:** {report['records']}  •  footer row {report['footer_row']}",
            f"**Empty rows:** {report['empty_rows']}",
            f"**Misplaced:** {len(report['misplaced'])}",
            f"**Duplicates:** {len(report['duplicates'])}",
            f"**Unknown ranks:** {len(report['unknown_ranks'])}",
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

    async def _commit(self, record, reason):
        """Write one record authoritatively, under the sheet write lock.

        Reloads first so the write is applied to current state rather than to a
        snapshot that may have aged while the command was being processed.
        """
        async with self.gateway.write_lock:
            await self.repository.load()

            current = self.repository.by_uid(record.record_uid)
            if current is None:
                raise RoleManagerError(
                    "The record disappeared from the sheet before the change could be saved."
                )

            # Re-apply the intended change onto the freshly read row, so a
            # concurrent edit to an unrelated field is not clobbered.
            merged = current.with_changes(**{
                field: getattr(record, field)
                for field in ("rank_key", "branch_key", "timezone", "status",
                              "loa_raw", "discord_username", "category")
                if getattr(record, field) != getattr(current, field)
            })

            saved = await self.repository.save_record(merged)

        print(f"[Roles] {reason}: {saved.label()} (rev {saved.revision})")
        return saved

    def _describe_record(self, record):
        cfg = roles_config.current()
        rank = cfg.rank(record.rank_key)
        branch = cfg.branch(record.branch_key)
        category = layout.effective_category(record, cfg)

        lines = [
            f"**{record.discord_username or record.discord_id}**",
            f"Rank: {rank.display if rank else (record.rank_key or 'unset')}",
            f"Branch: {branch.display if branch else (record.branch_key or 'N/A')}",
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
        return "\n".join(lines)


#: Process-wide instance, shared by the Discord commands, the poller and Angela.
service = RoleManagerService()
