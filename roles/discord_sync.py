"""Discord role synchronization.

One direction only: the sheet's rank and branches decide the member's roles.
Nothing observed on Discord is ever written back to the sheet.

    rank change              -> main-server role
    rank + branches change   -> branch-server roles, in every branch server
                                the member belongs to

Only roles listed in the configuration are touched. Any other role a member has
— cosmetic, self-assigned, from an unrelated bot — is left exactly as it is.

Every failure mode here is non-fatal by design. A missing role, a missing guild,
a member who left, a rate limit or a permissions problem all produce a
SyncOutcome that gets retried or reported; none of them can reach back and alter
the authoritative record.
"""

import asyncio

import discord

from roles import config as roles_config
from roles.errors import PermanentSyncError, TargetNotPresentError, TransientSyncError
from roles.models import SyncTarget


class SyncOutcome:
    """What happened when we tried to synchronize one target."""

    def __init__(self, target, ok, message="", added=None, removed=None, retryable=False):
        self.target = target
        self.ok = ok
        self.message = message
        self.added = added or []
        self.removed = removed or []
        self.retryable = retryable

    def __repr__(self):
        verdict = "OK" if self.ok else ("RETRY" if self.retryable else "FAILED")
        return f"SyncOutcome({self.target}, {verdict}, {self.message!r})"

    def changed(self):
        return bool(self.added or self.removed)

    def describe(self):
        if not self.changed() and self.ok:
            return f"{self.target}: already correct"
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)}")
        if self.removed:
            parts.append(f"-{len(self.removed)}")
        detail = " ".join(parts) or "no change"
        return f"{self.target}: {detail}{'' if self.ok else f' — {self.message}'}"


class DiscordSynchronizer:
    """Applies a record's rank/branch to Discord.

    The client is injected rather than imported so this works whether the role
    manager runs on its own bot or is attached to an existing one.
    """

    def __init__(self, client=None):
        self.client = client

    def set_client(self, client):
        self.client = client

    def is_ready(self):
        return self.client is not None and self.client.is_ready()

    # ── Desired state ──

    def desired_roles(self, record, guild_id, cfg=None):
        """Role IDs this member should have in one guild, and the managed set.

        Returns (desired_ids, managed_ids). Removing `managed_ids - desired_ids`
        and adding `desired_ids - current` is the whole algorithm.

        Role layout:
            main server    primary rank role
                         + auxiliary roles from the rank
                         + auxiliary roles from EVERY branch the member is in
            branch server  that branch's roles for the rank — but only if the
                           member belongs to that branch, or the rank is universal
            any server     the general roles configured for it

        Auxiliary roles are main-server-only by design, so a member's rank and
        branches are all visible in the one server everybody is in.

        Grantable roles are applied last, on top of that foundation:

            desired = (foundation - roles a grant cancels) | roles a grant adds

        An explicit add always wins over a cancel, so one grantable role can
        never silently undo what another one hands out.
        """
        cfg = cfg or roles_config.current()
        managed = cfg.managed_role_ids(guild_id)
        desired = set()

        rank = cfg.rank(record.rank_key)
        if rank is None:
            # Unknown rank: touch nothing. Stripping every managed role because
            # a rank string is not in the config yet would be destructive.
            return set(), set()

        member_of = {branch.key for branch in cfg.branches_of(record.branch_keys)}

        if guild_id == cfg.discord.main_guild_id:
            if rank.main_discord_role_id:
                desired.add(rank.main_discord_role_id)
            desired.update(rank.main_aux_discord_role_ids)
            for branch in cfg.branches_of(record.branch_keys):
                desired.update(branch.main_aux_discord_role_ids)

        for candidate in cfg.branches.values():
            if not candidate.discord_guild_id or candidate.discord_guild_id != guild_id:
                continue
            # A normal rank is only recognized in its own branches' servers. A
            # universal rank is recognized in every branch server that maps it.
            if candidate.key in member_of or rank.universal:
                desired.update(candidate.discord_roles_for(rank.key))

        desired.update(cfg.general_role_ids(guild_id))

        added, suppressed = cfg.grant_adjustments(record.grants, guild_id)
        desired = (desired - suppressed) | added

        return desired, managed

    def target_guild_ids(self, record=None, cfg=None):
        """Every guild a record is reconciled in: main plus ALL branch servers.

        Not just the member's own branch server. Where a member holds roles
        depends on more than their current branch — a universal rank and a
        branch-scoped grantable role both reach into other servers — and a
        server that is skipped is a server where stale roles are never removed:
        the old branch after a transfer, every branch server after a demotion
        out of a universal rank, a revoked grantable role. Visiting a server the
        member is not in costs a cache lookup; see _sync_guild.
        """
        cfg = cfg or roles_config.current()
        guilds = []
        if cfg.discord.main_guild_id:
            guilds.append((SyncTarget.DISCORD_MAIN, cfg.discord.main_guild_id))
        for guild_id in cfg.branch_guild_ids():
            guilds.append((SyncTarget.DISCORD_BRANCH, guild_id))
        return guilds

    # ── Application ──

    async def sync_record(self, record, cfg=None, dry_run=False, strip=False):
        """Bring every relevant guild in line with the record. Returns outcomes.

        `strip=True` revokes instead: every managed role is removed in the main
        server and in every configured branch server. Used when a record is
        removed from the sheet — the person is no longer personnel, so their
        access goes with the record.
        """
        cfg = cfg or roles_config.current()
        outcomes = []

        if not record.discord_id:
            return [SyncOutcome(
                SyncTarget.DISCORD_MAIN, False,
                "Record has no Discord ID; nothing to synchronize.",
            )]

        if not self.is_ready():
            return [SyncOutcome(
                SyncTarget.DISCORD_MAIN, False,
                "Discord client is not ready.", retryable=True,
            )]

        for target, guild_id in self.target_guild_ids(record, cfg):
            outcomes.append(
                await self._sync_guild(record, target, guild_id, cfg, dry_run, strip)
            )

        if not outcomes:
            outcomes.append(SyncOutcome(
                SyncTarget.DISCORD_MAIN, True,
                "No Discord servers configured yet; nothing to do.",
            ))

        return outcomes

    async def _find_member(self, record, target, guild_id):
        """(guild, member, None), or (guild, None, outcome) when there is nobody
        to synchronize in that server — which is not necessarily a failure."""
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return None, None, SyncOutcome(
                target, False,
                f"Bot is not in guild {guild_id} (or the guild is unavailable).",
                retryable=True,
            )

        member = guild.get_member(record.discord_id)
        if member is None and guild.chunked:
            # The member cache is complete, so absence from it is the answer.
            # Every sync visits every branch server, and most people are in one
            # or two of them — this is what keeps that from costing a failed
            # API lookup per server, per person.
            return guild, None, SyncOutcome(
                target, True,
                f"{record.discord_id} is not a member of {guild.name}; skipped.",
            )
        if member is None:
            try:
                member = await guild.fetch_member(record.discord_id)
            except discord.NotFound:
                # Not an error: the person simply is not in this server.
                return guild, None, SyncOutcome(
                    target, True,
                    f"{record.discord_id} is not a member of {guild.name}; skipped.",
                )
            except discord.Forbidden:
                return guild, None, SyncOutcome(
                    target, False,
                    f"Missing permission to read members in {guild.name}.",
                )
            except discord.HTTPException as exc:
                return guild, None, SyncOutcome(
                    target, False, f"Member lookup failed: {exc}", retryable=True,
                )

        return guild, member, None

    async def _sync_guild(self, record, target, guild_id, cfg, dry_run, strip=False):
        guild, member, skipped = await self._find_member(record, target, guild_id)
        if member is None:
            return skipped

        if strip:
            # Revocation: remove every managed role, add nothing. Unmanaged
            # roles are still left alone — this revokes personnel access, it
            # does not wipe the member.
            desired = set()
            managed = cfg.managed_role_ids(guild_id)
        else:
            desired, managed = self.desired_roles(record, guild_id, cfg)

        # A grantable role spanning several servers lists the role IDs of all of
        # them together. Role IDs are unique across Discord, so the ones that do
        # not exist here belong to another of its servers and are not this
        # server's business — drop them instead of reporting them as missing.
        elsewhere = {
            role_id for role_id in cfg.shared_grant_role_ids(guild_id)
            if guild.get_role(role_id) is None
        }
        desired -= elsewhere
        managed -= elsewhere

        if not managed:
            return SyncOutcome(
                target, True,
                f"No managed roles configured for {guild.name}; skipped.",
            )

        current = {role.id for role in member.roles}
        to_add = [rid for rid in desired if rid not in current]
        to_remove = [rid for rid in (managed - desired) if rid in current]

        if not to_add and not to_remove:
            return SyncOutcome(target, True, "Already correct.")

        if dry_run:
            return SyncOutcome(
                target, True, "Dry run — no changes applied.",
                added=to_add, removed=to_remove,
            )

        add_objects, missing_add = self._resolve_roles(guild, to_add)
        remove_objects, missing_remove = self._resolve_roles(guild, to_remove)

        reason = (
            "Role sync: record removed from PERSONNEL" if strip
            else "Role sync: PERSONNEL sheet"
        )

        try:
            if remove_objects:
                await member.remove_roles(*remove_objects, reason=reason)
                await asyncio.sleep(cfg.discord.member_edit_delay)
            if add_objects:
                await member.add_roles(*add_objects, reason=reason)

        except discord.Forbidden:
            return SyncOutcome(
                target, False,
                f"Missing permission to manage roles in {guild.name}, or the bot's "
                f"highest role sits below the roles it must assign.",
            )
        except discord.HTTPException as exc:
            retryable = getattr(exc, "status", 0) in (429, 500, 502, 503, 504)
            return SyncOutcome(
                target, False, f"Discord API error: {exc}", retryable=retryable,
            )

        message = ""
        if missing_add or missing_remove:
            missing = missing_add + missing_remove
            message = f"Role IDs not found in {guild.name}: {missing}"
            print(f"[Roles:Discord] {message}")

        return SyncOutcome(
            target, True, message,
            added=[r.id for r in add_objects],
            removed=[r.id for r in remove_objects],
        )

    # ── Clean sync ──

    async def clean_sync_record(self, record, cfg=None, dry_run=False):
        """Wipe a member's Discord roles and give back exactly what their record
        calls for, in the main server and every branch server they are in.

        Unlike a normal sync this does not stop at managed roles: everything
        the bot is able to remove goes, including roles that are not in the
        configuration at all — leftovers from old configurations, hand-given
        roles, roles from other bots. That is the point of it: clearing
        residue a normal sync deliberately leaves alone.

        What Discord does not let a bot take away stays: integration and
        server-booster roles, and any role at or above the bot's own highest
        role. Each server is rewritten in ONE call that sets the final role
        list, so the member is never left half-stripped between two requests.

        Refuses outright when the record's rank is not configured: the result
        would be a member stripped of everything and given nothing back.
        """
        cfg = cfg or roles_config.current()

        if not record.discord_id:
            return [SyncOutcome(SyncTarget.DISCORD_MAIN, False,
                                "Record has no Discord ID; nothing to clean.")]
        if not self.is_ready():
            return [SyncOutcome(SyncTarget.DISCORD_MAIN, False,
                                "Discord client is not ready.", retryable=True)]
        if cfg.rank(record.rank_key) is None:
            return [SyncOutcome(
                SyncTarget.DISCORD_MAIN, False,
                f"Rank '{record.rank_key or '(blank)'}' is not in the configuration, so "
                f"there is no role set to rebuild. Refusing to strip their roles.",
            )]

        outcomes = []
        for target, guild_id in self.target_guild_ids(record, cfg):
            outcomes.append(await self._clean_guild(record, target, guild_id, cfg, dry_run))
            await asyncio.sleep(cfg.discord.member_edit_delay)
        return outcomes

    async def _clean_guild(self, record, target, guild_id, cfg, dry_run):
        guild, member, skipped = await self._find_member(record, target, guild_id)
        if member is None:
            return skipped

        me = guild.me
        if me is None:
            return SyncOutcome(target, False, f"{guild.name}: the bot's own member is not cached.",
                               retryable=True)
        ceiling = me.top_role

        desired, _managed = self.desired_roles(record, guild_id, cfg)
        shared_elsewhere = cfg.shared_grant_role_ids(guild_id)

        current = [role for role in member.roles if not role.is_default()]
        # Roles the API will not let a bot remove. They have to be part of the
        # new role list, or the whole edit is rejected.
        kept = [role for role in current if role.managed or role >= ceiling]

        wanted, missing, out_of_reach = [], [], []
        for role_id in desired:
            role = guild.get_role(role_id)
            if role is None:
                # A multi-server grantable role lists ids of its other servers too.
                if role_id not in shared_elsewhere:
                    missing.append(role_id)
                continue
            if role.managed or role >= ceiling:
                if role not in kept:
                    out_of_reach.append(role.name)
                continue
            wanted.append(role)

        final = {role.id: role for role in kept + wanted}
        held = {role.id for role in current}
        removed = [role for role in current if role.id not in final]
        added = [role for role in wanted if role.id not in held]

        notes = []
        if missing:
            notes.append(f"configured role(s) not found here: {missing}")
        if out_of_reach:
            notes.append(f"could not give {', '.join(out_of_reach)} (above the bot's role)")
        stuck = [role.name for role in kept]
        if stuck:
            notes.append(f"kept {', '.join(stuck)} (cannot be removed by a bot)")
        note = f" — {'; '.join(notes)}" if notes else ""

        if not removed and not added:
            return SyncOutcome(target, True, f"{guild.name}: already clean{note}.")

        summary = f"{guild.name}: removed {len(removed)}, gave {len(added)}"
        if dry_run:
            return SyncOutcome(target, True, f"Dry run — {summary}{note}.",
                               added=[r.id for r in added], removed=[r.id for r in removed])

        try:
            await member.edit(
                roles=list(final.values()),
                reason="Clean role sync: roles rebuilt from the PERSONNEL sheet",
            )
        except discord.Forbidden:
            return SyncOutcome(
                target, False,
                f"{guild.name}: missing permission to manage roles, or a role sits above "
                f"the bot's highest role.",
            )
        except discord.HTTPException as exc:
            retryable = getattr(exc, "status", 0) in (429, 500, 502, 503, 504)
            return SyncOutcome(target, False, f"{guild.name}: Discord API error: {exc}",
                               retryable=retryable)

        print(f"[Roles:Discord] Clean sync of {record.discord_id} in {guild.name}: "
              f"removed {[r.name for r in removed]}, gave {[r.name for r in added]}.")
        return SyncOutcome(target, True, f"{summary}{note}.",
                           added=[r.id for r in added], removed=[r.id for r in removed])

    @staticmethod
    def _resolve_roles(guild, role_ids):
        """Split role IDs into Role objects and IDs that do not exist here."""
        resolved, missing = [], []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                missing.append(role_id)
            else:
                resolved.append(role)
        return resolved, missing

    async def fetch_usernames(self, discord_ids):
        """{discord_id: account username} for the given users.

        The account username (@handle), not a display name or server nickname:
        it is the one name that is the same in every server. Display data only.
        A user who cannot be looked up is simply absent from the result, so a
        failed lookup can never blank a name that is already on the sheet.
        """
        names = {}
        if not self.is_ready():
            return names

        for discord_id in discord_ids:
            user = self.client.get_user(discord_id)
            if user is None:
                # Not in the cache: someone who shares no server with the bot
                # any more. Rare, so the extra API call is affordable.
                try:
                    user = await self.client.fetch_user(discord_id)
                except discord.HTTPException:
                    continue
            if user is not None and user.name:
                names[discord_id] = user.name
        return names

    # ── Diagnostics ──

    async def validate_configuration(self, cfg=None):
        """Check every configured role and guild actually exists.

        Run this once the real IDs are supplied — it turns a class of silent
        sync failures into one readable report.
        """
        cfg = cfg or roles_config.current()
        problems = []

        if not self.is_ready():
            return ["Discord client is not ready."]

        if not cfg.discord.main_guild_id:
            problems.append("Main guild ID is not configured.")
        elif self.client.get_guild(cfg.discord.main_guild_id) is None:
            problems.append(f"Bot is not in the main guild {cfg.discord.main_guild_id}.")

        main_guild = (
            self.client.get_guild(cfg.discord.main_guild_id)
            if cfg.discord.main_guild_id else None
        )

        for rank in cfg.ranks.values():
            if not rank.main_discord_role_id:
                problems.append(f"Rank {rank.key}: no main-server role ID configured.")
            elif main_guild and main_guild.get_role(rank.main_discord_role_id) is None:
                problems.append(
                    f"Rank {rank.key}: role {rank.main_discord_role_id} not found "
                    f"in {main_guild.name}."
                )

            if main_guild:
                for role_id in rank.main_aux_discord_role_ids:
                    if main_guild.get_role(role_id) is None:
                        problems.append(
                            f"Rank {rank.key}: auxiliary role {role_id} not found "
                            f"in {main_guild.name}."
                        )

        for branch in cfg.branches.values():
            # Auxiliary branch roles live in the main server, so they are checked
            # even for the N/A branch.
            if main_guild:
                for role_id in branch.main_aux_discord_role_ids:
                    if main_guild.get_role(role_id) is None:
                        problems.append(
                            f"Branch {branch.key}: auxiliary role {role_id} not found "
                            f"in {main_guild.name}."
                        )

            if branch.is_na:
                continue
            if not branch.discord_guild_id:
                problems.append(f"Branch {branch.key}: no Discord server ID configured.")
                continue
            guild = self.client.get_guild(branch.discord_guild_id)
            if guild is None:
                problems.append(
                    f"Branch {branch.key}: bot is not in guild {branch.discord_guild_id}."
                )
                continue
            for rank_key, role_ids in branch.discord_roles_by_rank.items():
                if cfg.rank(rank_key) is None:
                    problems.append(
                        f"Branch {branch.key}: roles are mapped for {rank_key}, "
                        f"which is not a configured rank."
                    )
                for role_id in role_ids:
                    if guild.get_role(role_id) is None:
                        problems.append(
                            f"Branch {branch.key} / rank {rank_key}: role {role_id} "
                            f"not found in {guild.name}."
                        )

        for scope, role_ids in cfg.general_roles.items():
            guild_id = cfg._scope_guild_id(scope)
            guild = self.client.get_guild(guild_id) if guild_id else None
            if guild is None:
                problems.append(
                    f"General roles for {scope}: "
                    + ("server not configured." if not guild_id
                       else f"bot is not in guild {guild_id}.")
                )
                continue
            for role_id in role_ids:
                if guild.get_role(role_id) is None:
                    problems.append(
                        f"General roles for {scope}: role {role_id} not found in {guild.name}."
                    )

        for grantable in cfg.grantable_roles.values():
            guild_ids = cfg.grantable_guild_ids(grantable)
            if len(guild_ids) < len(grantable.guilds):
                problems.append(
                    f"Grantable role {grantable.key}: not every guild in "
                    f"{grantable.guilds} resolves to a configured server."
                )

            guilds = []
            for guild_id in guild_ids:
                guild = self.client.get_guild(guild_id)
                if guild is None:
                    problems.append(
                        f"Grantable role {grantable.key}: bot is not in guild {guild_id}."
                    )
                else:
                    guilds.append(guild)
            if not guilds:
                continue

            # Each role has to exist in at least one of the role's servers; with
            # several servers it is expected to be absent from the others.
            where = ", ".join(g.name for g in guilds)
            for role_id in grantable.add_discord_role_ids + grantable.remove_discord_role_ids:
                if not any(g.get_role(role_id) for g in guilds):
                    problems.append(
                        f"Grantable role {grantable.key}: role {role_id} not found "
                        f"in {where}."
                    )
            # A cancelled role only has an effect if a rank, branch or general
            # role grants it.
            for role_id in grantable.remove_discord_role_ids:
                owners = [g for g in guilds if g.get_role(role_id)]
                if owners and not any(role_id in cfg.managed_role_ids(g.id) for g in owners):
                    problems.append(
                        f"Grantable role {grantable.key}: removes role {role_id}, "
                        f"which no rank, branch or general role grants in "
                        f"{owners[0].name} — it has no effect."
                    )

        return problems


#: Shared instance. main.py / bot.py calls set_client() once the bot is ready.
synchronizer = DiscordSynchronizer()
