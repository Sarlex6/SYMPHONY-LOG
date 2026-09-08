"""Discord role synchronization.

One direction only: the sheet's rank and branch decide the member's roles.
Nothing observed on Discord is ever written back to the sheet.

    rank change            -> main-server role
    rank + branch change   -> branch-server role

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
                         + auxiliary roles from the branch
            branch server  branch role for the rank

        Auxiliary roles are main-server-only by design, so a member's rank and
        branch are both visible in the one server everybody is in.
        """
        cfg = cfg or roles_config.current()
        managed = cfg.managed_role_ids(guild_id)
        desired = set()

        rank = cfg.rank(record.rank_key)
        if rank is None:
            # Unknown rank: touch nothing. Stripping every managed role because
            # a rank string is not in the config yet would be destructive.
            return set(), set()

        branch = cfg.branch(record.branch_key)

        if guild_id == cfg.discord.main_guild_id:
            if rank.main_discord_role_id:
                desired.add(rank.main_discord_role_id)
            desired.update(rank.main_aux_discord_role_ids)
            if branch:
                desired.update(branch.main_aux_discord_role_ids)

        if branch and branch.discord_guild_id == guild_id:
            branch_role = branch.discord_role_for(rank.key)
            if branch_role:
                desired.add(branch_role)

        return desired, managed

    def target_guild_ids(self, record, cfg=None, all_guilds=False):
        """Guilds this record has a presence in: main plus its branch server.

        `all_guilds` widens it to every configured branch server. Used when
        revoking access, where the record's *current* branch is not a reliable
        guide to which servers still carry roles from an earlier branch.
        """
        cfg = cfg or roles_config.current()
        guilds = []
        if cfg.discord.main_guild_id:
            guilds.append((SyncTarget.DISCORD_MAIN, cfg.discord.main_guild_id))

        if all_guilds:
            for branch in cfg.branches.values():
                if branch.discord_guild_id and branch.discord_guild_id != cfg.discord.main_guild_id:
                    guilds.append((SyncTarget.DISCORD_BRANCH, branch.discord_guild_id))
            return guilds

        branch = cfg.branch(record.branch_key)
        if branch and branch.discord_guild_id and branch.discord_guild_id != cfg.discord.main_guild_id:
            guilds.append((SyncTarget.DISCORD_BRANCH, branch.discord_guild_id))

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

        for target, guild_id in self.target_guild_ids(record, cfg, all_guilds=strip):
            outcomes.append(
                await self._sync_guild(record, target, guild_id, cfg, dry_run, strip)
            )

        if not outcomes:
            outcomes.append(SyncOutcome(
                SyncTarget.DISCORD_MAIN, True,
                "No Discord servers configured yet; nothing to do.",
            ))

        return outcomes

    async def _sync_guild(self, record, target, guild_id, cfg, dry_run, strip=False):
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return SyncOutcome(
                target, False,
                f"Bot is not in guild {guild_id} (or the guild is unavailable).",
                retryable=True,
            )

        member = guild.get_member(record.discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(record.discord_id)
            except discord.NotFound:
                # Not an error: the person simply is not in this server.
                return SyncOutcome(
                    target, True,
                    f"{record.discord_id} is not a member of {guild.name}; skipped.",
                )
            except discord.Forbidden:
                return SyncOutcome(
                    target, False,
                    f"Missing permission to read members in {guild.name}.",
                )
            except discord.HTTPException as exc:
                return SyncOutcome(
                    target, False, f"Member lookup failed: {exc}", retryable=True,
                )

        if strip:
            # Revocation: remove every managed role, add nothing. Unmanaged
            # roles are still left alone — this revokes personnel access, it
            # does not wipe the member.
            desired = set()
            managed = cfg.managed_role_ids(guild_id)
        else:
            desired, managed = self.desired_roles(record, guild_id, cfg)

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
            for rank_key, role_id in branch.discord_roles_by_rank.items():
                if guild.get_role(role_id) is None:
                    problems.append(
                        f"Branch {branch.key} / rank {rank_key}: role {role_id} not "
                        f"found in {guild.name}."
                    )

        return problems


#: Shared instance. main.py / bot.py calls set_client() once the bot is ready.
synchronizer = DiscordSynchronizer()
