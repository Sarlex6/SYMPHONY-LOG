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
from datetime import datetime

from roles import columns
from roles import config as roles_config
from roles import layout
from roles.models import ChangeSource
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

        self._last_poll_at = datetime.utcnow()

        if not changes:
            await self._maybe_resync_drift(cfg)
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
        """Re-lay-out the sheet when records are in the wrong section.

        A manual rank edit puts someone in the right rank but the wrong place;
        this is what moves them into their category section.
        """
        records = self.repository.live_records()
        misplaced = layout.find_misplaced(records, cfg)
        # Every EMPTY row beyond the configured slack is one the sheet should
        # have been resized around.
        surplus = max(0, len(layout.find_empty_rows(self.repository.all_records()))
                         - columns.SLACK_ROWS)

        if not misplaced and not surplus:
            return

        reasons = []
        if misplaced:
            reasons.append(f"{len(misplaced)} misplaced record(s)")
        if surplus:
            reasons.append(f"{surplus} EMPTY row(s)")
        print(f"[Roles:Poll] Restructuring: {', '.join(reasons)}.")

        try:
            async with self.service.gateway.write_lock:
                await self.repository.restructure()
        except Exception as exc:
            print(f"[Roles:Poll] Restructure failed: {type(exc).__name__}: {exc}")

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
        human_fields = {"category", "discord_username", "timezone", "rank_key",
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
