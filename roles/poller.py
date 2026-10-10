"""Background synchronization: detect sheet changes, push them outward.

A change made directly in Google Sheets is authoritative in exactly the same way
as one made through a Discord command. This loop is what makes that true — it
notices the manual edit and drives the same synchronization.

What it does NOT do is rewrite the sheet every cycle. Each pass compares a fresh
read against the previous snapshot and acts only on real differences, so a quiet
sheet costs one read per interval and nothing else.

Follows the background-loop convention already used by inventory/state.py:
`while not bot.is_closed()` with a sleep at the end and every exception caught,
so one bad cycle can never kill the loop.
"""

import asyncio
import time
from datetime import datetime

from roles import columns
from roles import config as roles_config
from roles import layout
from roles.models import ChangeSource, format_identification, split_identification
from roles.repository import RecordChange


class SheetPoller:
    """Periodic change detection over the PERSONNEL sheet."""

    def __init__(self, service):
        self.service = service
        self.repository = service.repository
        self.sync_queue = service.sync_queue

        self._running = False
        self._last_poll_at = None
        self._last_error = ""
        #: Monotonic counter. A poll result carries the generation it was read
        #: at; anything computed from an older generation is discarded rather
        #: than applied on top of newer state.
        self._generation = 0
        self._stats = {"cycles": 0, "changes": 0, "queued": 0, "errors": 0, "removals": 0}

        #: The layout a restructure was last asked to fix, and whether that
        #: attempt ran to completion. See _maybe_restructure.
        self._restructure_signature = None
        self._restructure_completed = False
        self._restructure_warned = False

        #: Column D texts worked out in the background, by record UID, waiting
        #: to be written at the next poll. See _start_username_lookup.
        self._pending_usernames = None
        self._username_task = None
        self._username_lookup_at = None

        #: Record UIDs of applicants found to be in the Roblox group, waiting
        #: to be promoted at the next poll. See _start_applicant_check.
        self._pending_promotions = []
        self._applicant_task = None
        self._applicant_check_at = None

    # ── Loop ──

    async def run(self, bot=None):
        """Poll until the bot closes. Interval comes from configuration."""
        if bot is not None:
            await bot.wait_until_ready()

        self._running = True
        print(
            f"[Roles:Poll] Started "
            f"(interval {roles_config.current().sync.poll_interval_seconds}s)."
        )

        while self._running and not (bot is not None and bot.is_closed()):
            interval = roles_config.current().sync.poll_interval_seconds
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stats["errors"] += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                print(f"[Roles:Poll] Cycle failed: {self._last_error}")

            await asyncio.sleep(max(5, interval))

        print("[Roles:Poll] Stopped.")

    def stop(self):
        self._running = False

    # ── One cycle ──

    async def poll_once(self):
        """Read the sheet, diff it, queue what changed. Returns the changes."""
        cfg = roles_config.current()
        self._stats["cycles"] += 1

        # Skip a cycle if a mutation is mid-flight. Reading a half-written layout
        # would produce phantom differences and queue pointless work.
        if self.service.gateway.write_lock.locked():
            return []

        async with self.service.gateway.write_lock:
            changes = await self.repository.refresh_and_diff()
            self._generation = self.repository.generation
            # The snapshot is fresh and the lock is held: the one moment a
            # looked-up name can be written to a row number known to be current.
            await self._apply_usernames()
            await self._apply_promotions()

        self._last_poll_at = datetime.utcnow()
        self._start_username_lookup(cfg)
        self._start_applicant_check(cfg)

        if not changes:
            await self._maybe_resync_drift(cfg)
            # Checked on quiet cycles too. A layout that is wrong stays wrong
            # until something fixes it, and waiting for an unrelated edit to
            # come along and trigger the check is not a repair strategy.
            if cfg.sync.auto_restructure:
                await self._maybe_restructure(cfg)
            return []

        self._stats["changes"] += len(changes)
        queued = 0

        for change in changes:
            if change.kind is RecordChange.REMOVED:
                # A record does not vanish on its own. Its removal means the
                # person is no longer personnel, so their managed roles go with
                # it — re-entry requires /register again.
                print(f"[Roles:Poll] Record removed from sheet: {change.record.label()} "
                      f"— revoking managed roles.")
                self.sync_queue.enqueue_removal(
                    change.record, reason="row removed from PERSONNEL"
                )
                self._stats["removals"] += 1
                continue

            record = change.record
            source = "manual edit" if change.kind is RecordChange.MODIFIED else "new row"

            if change.kind is RecordChange.MODIFIED and record.source is not ChangeSource.MANUAL:
                # The system's own write shows up here too; only mark provenance
                # when the change did not come from us.
                if not self._looks_system_originated(change):
                    try:
                        record = await self.repository.note_manual_change(record)
                    except Exception as exc:
                        print(f"[Roles:Poll] Could not mark manual provenance: {exc}")

            if change.affects_sync():
                if self.sync_queue.enqueue(record, reason=f"{source}: {', '.join(change.fields) or change.kind}"):
                    queued += 1
                    print(f"[Roles:Poll] {record.label()} changed ({', '.join(change.fields) or change.kind}) "
                          f"— synchronization queued.")

        self._stats["queued"] += queued

        if cfg.sync.auto_restructure:
            await self._maybe_restructure(cfg)

        return changes

    async def _maybe_resync_drift(self, cfg):
        """Catch records whose stored sync hash no longer matches their state.

        Covers the case where a sync failed permanently earlier: nothing changed
        this cycle, but the record is still out of step with Discord/Roblox.
        """
        drifted = [r for r in self.repository.live_records() if r.needs_sync()]
        if not drifted:
            return

        queued = len(self.sync_queue.enqueue_many(drifted, reason="sync drift"))
        if queued:
            self._stats["queued"] += queued
            print(f"[Roles:Poll] Re-queued {queued} record(s) with unsynchronized state.")

    async def _maybe_restructure(self, cfg):
        """Re-lay-out the sheet when records are not where they belong.

        A manual rank edit puts someone in the right rank but the wrong place;
        this is what moves them into their category section, and into the right
        position within it.

        "Where they belong" is decided by planning the layout and seeing whether
        anyone would move — the same computation the repository then executes,
        so the check and the fix cannot disagree. It does not read column C:
        that column is merged per section, so most rows carry no category value
        to compare against.
        """
        records = self.repository.live_records()
        plan = layout.plan_layout(records, cfg, self.repository.footer_row)
        # Every EMPTY row beyond the configured slack is one the sheet should
        # have been resized around.
        surplus = max(0, len(layout.find_empty_rows(self.repository.all_records()))
                         - columns.SLACK_ROWS)

        if not plan.moves and not surplus:
            self._restructure_signature = None
            return

        # This now runs every cycle, so it must not be able to rewrite the sheet
        # every cycle. If a restructure already ran to completion for exactly
        # this layout and the sheet still reads the same, another pass will not
        # change the outcome — say so once and leave it for a human.
        signature = (
            tuple((r.record_uid or f"row{r.row}", r.row, target) for r, target in plan.moves),
            surplus,
        )
        if signature == self._restructure_signature and self._restructure_completed:
            if not self._restructure_warned:
                self._restructure_warned = True
                print("[Roles:Poll] Restructure did not settle the layout; not "
                      "repeating it. Run /aria restructure after checking the sheet.")
            return
        self._restructure_signature = signature
        self._restructure_completed = False
        self._restructure_warned = False

        reasons = []
        if plan.moves:
            reasons.append(f"{len(plan.moves)} record(s) out of place")
        if surplus:
            reasons.append(f"{surplus} EMPTY row(s)")
        print(f"[Roles:Poll] Restructuring: {', '.join(reasons)}.")

        try:
            async with self.service.gateway.write_lock:
                # Never purges: automatic deletion of rows is not something a
                # background loop should decide. Only the explicit command does.
                await self.repository.restructure(purge_incomplete=False)
            self._restructure_completed = True
        except Exception as exc:
            # Left as not-completed, so the next cycle tries again.
            print(f"[Roles:Poll] Restructure failed: {type(exc).__name__}: {exc}")

    # ── Applicants who have joined the Roblox group ──

    def _start_applicant_check(self, cfg):
        """Kick off a background group-membership check when one is due.

        Same two-step shape as the username pass, for the same reasons: the
        check is one Roblox call per applicant, so it runs as its own task, and
        what it finds is only acted on by _apply_promotions at a later poll.
        """
        minutes = cfg.registration.applicant_check_minutes
        if not minutes or minutes <= 0 or cfg.applicant_promotion() is None:
            return
        if self._applicant_task is not None and not self._applicant_task.done():
            return

        now = time.monotonic()
        if self._applicant_check_at is not None and now - self._applicant_check_at < minutes * 60:
            return

        self._applicant_check_at = now
        self._applicant_task = asyncio.get_event_loop().create_task(
            self._check_applicants(cfg)
        )

    async def _check_applicants(self, cfg):
        from roles.roblox_sync import synchronizer as roblox_synchronizer

        pair = cfg.applicant_promotion()
        if pair is None:
            return
        applicant, _joined = pair

        waiting = [
            r for r in self.repository.live_records()
            if r.rank_key == applicant.key and r.roblox_id and not r.is_incomplete()
        ]

        joined = []
        for record in waiting:
            try:
                in_group = await roblox_synchronizer.is_group_member(record.roblox_id, cfg)
            except Exception as exc:
                print(f"[Roles:Poll] Group check failed: {type(exc).__name__}: {exc}")
                in_group = None

            if in_group is None:
                # Could not be checked — no key, or Roblox is unhappy. Unknown is
                # not "joined", and asking about everyone else right now would
                # get the same non-answer. The next pass starts over.
                break
            if in_group:
                joined.append(record.record_uid)

            if cfg.roblox.request_delay:
                await asyncio.sleep(cfg.roblox.request_delay)

        if joined:
            self._pending_promotions = joined

    async def _apply_promotions(self):
        """Promote applicants found in the group. Caller holds the write lock,
        post-refresh."""
        uids, self._pending_promotions = self._pending_promotions, []
        if not uids:
            return

        try:
            promoted = await self.service.promote_group_joiners(uids)
        except Exception as exc:
            # Nothing is lost: they are still applicants, so the next check
            # finds them again.
            print(f"[Roles:Poll] Could not promote applicants: {type(exc).__name__}: {exc}")
            return

        self._stats["promotions"] = self._stats.get("promotions", 0) + len(promoted)

    # ── Usernames (column D) ──

    def _start_username_lookup(self, cfg):
        """Kick off the background username pass when one is due.

        Two steps on purpose. The pass can involve API calls, so it runs as its
        own task and never holds up a poll or the sheet lock. Its result is only *written* by _apply_usernames, at the
        start of a later poll, where the snapshot is known to be current.
        """
        minutes = cfg.sync.username_refresh_minutes
        if not minutes or minutes <= 0:
            return
        if self._username_task is not None and not self._username_task.done():
            return

        now = time.monotonic()
        if self._username_lookup_at is not None and now - self._username_lookup_at < minutes * 60:
            return

        self._username_lookup_at = now
        self._username_task = asyncio.get_event_loop().create_task(
            self._lookup_usernames(cfg)
        )

    async def _lookup_usernames(self, cfg):
        """Work out what column D should read for every record.

        Column D is "RobloxName (discordname)". The two halves are treated
        differently, because they change at very different rates:

          * The Discord half is re-read from Discord on every pass. Reading it
            is nearly free — the names are already in the bot's member cache.

          * The Roblox half is left alone. It is only looked up for rows that
            are not in the combined format yet (written before it existed, or
            typed by hand), which need it once to be converted. After that the
            row never costs a Roblox call again.
        """
        from roles.discord_sync import synchronizer as discord_synchronizer
        from roles.roblox_sync import synchronizer as roblox_synchronizer

        if not discord_synchronizer.is_ready():
            self._retry_username_lookup_soon(cfg)
            return

        records = [r for r in self.repository.live_records()
                   if not r.is_incomplete() and r.discord_id]
        if not records:
            return

        try:
            discord_names = await discord_synchronizer.fetch_usernames(
                [r.discord_id for r in records]
            )
        except Exception as exc:
            print(f"[Roles:Poll] Discord username lookup failed: {type(exc).__name__}: {exc}")
            discord_names = {}

        unconverted = [r for r in records if not split_identification(r.username)[1]]
        roblox_names = {}
        if unconverted:
            try:
                roblox_names = await roblox_synchronizer.fetch_usernames(
                    sorted({r.roblox_id for r in unconverted if r.roblox_id}), cfg
                )
            except Exception as exc:
                print(f"[Roles:Poll] Roblox username lookup failed: {type(exc).__name__}: {exc}")

        texts, waiting = {}, 0
        for record in records:
            roblox_name, discord_name = split_identification(record.username)

            if not discord_name:
                # Old single-name row. What it holds could be either username,
                # so it is only rewritten once Roblox has said which is which.
                roblox_name = roblox_names.get(record.roblox_id)
                if not roblox_name:
                    waiting += 1
                    continue

            text = format_identification(
                roblox_name, discord_names.get(record.discord_id) or discord_name
            )
            if text and text != record.username:
                texts[record.record_uid] = text

        if texts:
            self._pending_usernames = texts
        if waiting:
            # Rows still to convert should not sit for a whole day because one
            # Roblox call failed.
            print(f"[Roles:Poll] {waiting} row(s) still need a Roblox username to be "
                  f"converted to the combined format; retrying shortly.")
            self._retry_username_lookup_soon(cfg)

    def _retry_username_lookup_soon(self, cfg, seconds=600):
        """Make the next pass due in ten minutes instead of a full interval."""
        interval = cfg.sync.username_refresh_minutes * 60
        self._username_lookup_at = time.monotonic() - max(0, interval - seconds)

    async def _apply_usernames(self):
        """Write looked-up usernames. Caller holds the write lock, post-refresh."""
        names, self._pending_usernames = self._pending_usernames, None
        if not names:
            return

        try:
            changed = await self.repository.update_usernames(names)
        except Exception as exc:
            print(f"[Roles:Poll] Could not write usernames: {type(exc).__name__}: {exc}")
            return

        if changed:
            preview = ", ".join(f"{old or '(blank)'} -> {new}" for old, new in changed[:10])
            more = f" (+{len(changed) - 10} more)" if len(changed) > 10 else ""
            print(f"[Roles:Poll] Column D updated for {len(changed)} record(s): "
                  f"{preview}{more}.")

    @staticmethod
    def _looks_system_originated(change):
        """Heuristic: did this diff come from our own write?

        Only bookkeeping fields moving, with the revision advancing, means the
        system wrote it. Anything touching a human-facing field is treated as a
        manual edit — the safer assumption, since a manual edit misread as a
        system write would lose its provenance marker.
        """
        if change.previous is None:
            return False
        if change.record.revision <= change.previous.revision:
            return False
        human_fields = {"category", "username", "timezone", "rank_key",
                        "branch_key", "status", "entry_date"}
        return not (human_fields & set(change.fields))

    # ── Status ──

    def status(self):
        return {
            "running": self._running,
            "last_poll_at": self._last_poll_at.isoformat(timespec="seconds")
                            if self._last_poll_at else None,
            "generation": self._generation,
            "last_error": self._last_error,
            "interval": roles_config.current().sync.poll_interval_seconds,
            **self._stats,
        }
