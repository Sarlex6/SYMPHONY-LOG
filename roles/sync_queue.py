"""Retryable outbound synchronization queue.

Separates the authoritative write from the external pushes it implies. A command
writes the sheet, enqueues a job, and returns success — because it *did* succeed:
the authoritative record is updated. Discord and Roblox catch up afterwards, with
retries, and their failure is reported rather than reverted.

    sheet says Rank X  ->  Discord push fails  ->  sheet still says Rank X
                                                   job retried with backoff
                                                   failure surfaced, not swallowed

Staleness: each job carries the record revision it was built from. Before it
runs, the job re-reads the record from the repository snapshot; if the snapshot
has a higher revision the job is dropped, because a newer job already covers it.
That is what stops an old queued state from overwriting a newer one.

Dead-lettered jobs persist to data/roles_sync_failures.json, following the
project's existing JSON-in-data/ convention.
"""

import asyncio
import json
import os
from datetime import datetime

from roles import config as roles_config
from roles.models import SyncStatus, UserRecord

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FAILURES_FILE = os.path.join(DATA_DIR, "roles_sync_failures.json")
#: Removal jobs are persisted the moment they are created. The sheet row is
#: already gone, so a restart before the strip completes would otherwise lose
#: the revocation entirely — the next poll has nothing left to diff against.
REMOVALS_FILE = os.path.join(DATA_DIR, "roles_pending_removals.json")


class SyncJob:
    """One record's pending synchronization."""

    def __init__(self, record_uid, revision, reason="", attempt=0, targets=None,
                 removal=False, snapshot=None):
        self.record_uid = record_uid
        self.revision = revision
        self.reason = reason
        self.attempt = attempt
        #: None = all applicable targets. A list narrows it (e.g. Discord only).
        self.targets = targets
        #: True for a revocation job: the record is gone from the sheet, so the
        #: person's managed roles are stripped rather than reconciled.
        self.removal = removal
        #: Last known record. Required for removal jobs, since the row no longer
        #: exists to look up.
        self.snapshot = snapshot
        self.created_at = datetime.utcnow()
        self.last_error = ""

    def __repr__(self):
        kind = "removal" if self.removal else "sync"
        return f"SyncJob({kind}, {self.record_uid}, rev={self.revision}, attempt={self.attempt})"

    def to_dict(self):
        data = {
            "record_uid": self.record_uid,
            "revision": self.revision,
            "reason": self.reason,
            "attempt": self.attempt,
            "targets": self.targets,
            "removal": self.removal,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "last_error": self.last_error,
        }
        if self.snapshot is not None:
            # Only the fields a revocation actually needs.
            data["snapshot"] = {
                "record_uid": self.snapshot.record_uid,
                "discord_id": self.snapshot.discord_id,
                "roblox_id": self.snapshot.roblox_id,
                "discord_username": self.snapshot.discord_username,
                "rank_key": self.snapshot.rank_key,
                "branch_key": self.snapshot.branch_key,
            }
        return data

    @classmethod
    def from_dict(cls, data):
        """Rebuild a persisted job. Returns None if it is unusable."""
        snapshot = None
        raw = data.get("snapshot")
        if raw:
            snapshot = UserRecord(
                record_uid=raw.get("record_uid", ""),
                discord_id=int(raw.get("discord_id") or 0),
                roblox_id=int(raw.get("roblox_id") or 0),
                discord_username=raw.get("discord_username", ""),
                rank_key=raw.get("rank_key", ""),
                branch_key=raw.get("branch_key", ""),
            )

        if data.get("removal") and snapshot is None:
            return None  # a removal job without its snapshot cannot be replayed

        return cls(
            record_uid=data.get("record_uid", ""),
            revision=int(data.get("revision") or 0),
            reason=data.get("reason", ""),
            attempt=int(data.get("attempt") or 0),
            targets=data.get("targets"),
            removal=bool(data.get("removal")),
            snapshot=snapshot,
        )


class SyncQueue:
    """Serialized worker over pending sync jobs.

    One job at a time on purpose: it keeps Discord and Roblox rate limits
    manageable and makes the log readable. Throughput has never been the
    constraint here — correctness is.
    """

    def __init__(self, repository, discord_sync=None, roblox_sync=None):
        self.repository = repository

        if discord_sync is None:
            from roles.discord_sync import synchronizer as discord_sync
        if roblox_sync is None:
            from roles.roblox_sync import synchronizer as roblox_sync

        self.discord_sync = discord_sync
        self.roblox_sync = roblox_sync

        self._queue = asyncio.Queue()
        self._pending_uids = set()
        self._dead_letters = []
        self._pending_removals = {}   # record_uid -> serialized removal job
        self._worker_task = None
        self._stats = {
            "processed": 0, "succeeded": 0, "failed": 0,
            "dropped_stale": 0, "removals": 0,
        }

    # ── Enqueueing ──

    def enqueue(self, record, reason="", targets=None):
        """Queue a record for synchronization.

        Collapses duplicates: a record already queued is not queued twice, since
        the job re-reads current state when it runs anyway.
        """
        if not record.record_uid:
            print("[Roles:Sync] Refusing to queue a record with no UID.")
            return None

        if record.record_uid in self._pending_uids:
            return None

        job = SyncJob(record.record_uid, record.revision, reason, targets=targets)
        self._pending_uids.add(record.record_uid)
        self._queue.put_nowait(job)
        return job

    def enqueue_many(self, records, reason=""):
        return [job for job in (self.enqueue(r, reason) for r in records) if job]

    def enqueue_removal(self, record, reason="removed from sheet"):
        """Queue a revocation for a record that no longer exists on the sheet.

        The record's own snapshot travels with the job, because there is no row
        left to read it back from. The job is persisted immediately so a restart
        mid-revocation does not silently drop it.
        """
        if not record.record_uid:
            print("[Roles:Sync] Refusing to queue a removal with no record UID.")
            return None

        job = SyncJob(
            record.record_uid, record.revision, reason,
            removal=True, snapshot=record,
        )

        self._pending_removals[record.record_uid] = job.to_dict()
        self._save_removals()

        self._pending_uids.add(record.record_uid)
        self._queue.put_nowait(job)
        self._stats["removals"] += 1

        print(f"[Roles:Sync] Revocation queued for {record.label()} ({reason}).")
        return job

    def qsize(self):
        return self._queue.qsize()

    # ── Worker ──

    def start(self, loop=None):
        """Start the background worker. Idempotent."""
        if self._worker_task and not self._worker_task.done():
            return self._worker_task
        loop = loop or asyncio.get_event_loop()
        self._worker_task = loop.create_task(self._worker())
        print("[Roles:Sync] Worker started.")
        return self._worker_task

    def stop(self):
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None

    async def _worker(self):
        while True:
            try:
                job = await self._queue.get()
                self._pending_uids.discard(job.record_uid)
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A crashing worker would silently stop all synchronization.
                print(f"[Roles:Sync] Worker error: {type(exc).__name__}: {exc}")
                await asyncio.sleep(5)

    async def _process(self, job):
        cfg = roles_config.current()

        if job.removal:
            record = job.snapshot
            # A record that came back (re-added, or the row was restored) means
            # the revocation is obsolete — do not strip a current member.
            if self.repository.by_uid(job.record_uid) is not None:
                print(f"[Roles:Sync] {job.record_uid} is back on the sheet; "
                      f"cancelling revocation.")
                self._clear_removal(job.record_uid)
                return
        else:
            record = self.repository.by_uid(job.record_uid)

            if record is None:
                print(f"[Roles:Sync] {job.record_uid} no longer exists; dropping job.")
                return

            # Staleness guard: a newer revision means a newer job covers this.
            if record.revision > job.revision:
                self._stats["dropped_stale"] += 1
                return

        self._stats["processed"] += 1
        outcomes = []
        dry_run = not cfg.sync.enabled
        strip = job.removal

        wants_discord = job.targets is None or any("DISCORD" in t for t in job.targets)
        wants_roblox = job.targets is None or any("ROBLOX" in t for t in job.targets)

        if strip and not cfg.sync.on_removal_strip_discord:
            wants_discord = False

        if wants_discord:
            try:
                outcomes.extend(
                    await self.discord_sync.sync_record(record, cfg, dry_run, strip=strip)
                )
            except Exception as exc:
                print(f"[Roles:Sync] Discord sync raised for {record.label()}: "
                      f"{type(exc).__name__}: {exc}")
                outcomes.append(_exception_outcome("DISCORD_MAIN", exc))

        if wants_roblox:
            try:
                outcomes.extend(
                    await self.roblox_sync.sync_record(record, cfg, dry_run, strip=strip)
                )
            except Exception as exc:
                print(f"[Roles:Sync] Roblox sync raised for {record.label()}: "
                      f"{type(exc).__name__}: {exc}")
                outcomes.append(_exception_outcome("ROBLOX", exc))

        failed = [o for o in outcomes if not o.ok]
        retryable = [o for o in failed if o.retryable]
        note = "; ".join(o.describe() for o in outcomes)

        if not failed:
            self._stats["succeeded"] += 1
            if strip:
                # Nothing to write back: the row is gone. Clearing the persisted
                # removal is what marks the revocation complete.
                self._clear_removal(job.record_uid)
                print(f"[Roles:Sync] Revocation complete for {record.label()}: {note}")
            else:
                await self._record_result(record, SyncStatus.OK, note)
            return

        self._stats["failed"] += 1
        job.last_error = "; ".join(o.message for o in failed)

        if retryable and job.attempt < cfg.sync.max_attempts:
            delay = _backoff_delay(cfg, job.attempt)
            job.attempt += 1
            print(
                f"[Roles:Sync] {record.label()} failed ({job.last_error}); "
                f"retry {job.attempt}/{cfg.sync.max_attempts} in {delay}s."
            )
            if strip:
                self._pending_removals[job.record_uid] = job.to_dict()
                self._save_removals()
            else:
                await self._record_result(record, SyncStatus.PENDING, note)
            asyncio.get_event_loop().create_task(self._requeue_after(job, delay))
            return

        # Out of retries, or a failure retrying cannot fix. The sheet keeps its
        # authoritative value; the failure is recorded for a human.
        print(f"[Roles:Sync] {record.label()} permanently failed: {job.last_error}")
        if strip:
            # Keep it in pending_removals as well as the dead-letter list: an
            # incomplete revocation is an access-control problem, so it stays
            # visible and replayable until it succeeds or is cleared by hand.
            self._pending_removals[job.record_uid] = job.to_dict()
            self._save_removals()
        else:
            await self._record_result(record, SyncStatus.FAILED, note)
        self._dead_letter(job, record)

    async def _requeue_after(self, job, delay):
        try:
            await asyncio.sleep(delay)
            self._pending_uids.add(job.record_uid)
            await self._queue.put(job)
        except asyncio.CancelledError:
            pass

    async def _record_result(self, record, status, note):
        """Write the sync outcome to the technical columns only."""
        try:
            await self.repository.mark_synced(record, status, note)
        except Exception as exc:
            # Failing to record the outcome must not escalate into losing the
            # authoritative data. Log and move on; the next poll re-detects it.
            print(f"[Roles:Sync] Could not record sync result for "
                  f"{record.record_uid}: {type(exc).__name__}: {exc}")

    # ── Dead letters ──

    def _dead_letter(self, job, record):
        entry = job.to_dict()
        entry["label"] = record.label()
        entry["discord_id"] = record.discord_id
        entry["failed_at"] = datetime.utcnow().isoformat(timespec="seconds")
        self._dead_letters.append(entry)
        self._save_failures()

    def dead_letters(self):
        return list(self._dead_letters)

    def clear_dead_letters(self):
        count = len(self._dead_letters)
        self._dead_letters = []
        self._save_failures()
        return count

    def retry_dead_letters(self):
        """Requeue everything in the dead-letter list. Returns how many."""
        entries = self._dead_letters
        self._dead_letters = []
        requeued = 0

        for entry in entries:
            record = self.repository.by_uid(entry["record_uid"])
            if record is None:
                continue
            if self.enqueue(record, reason="manual dead-letter retry"):
                requeued += 1

        self._save_failures()
        return requeued

    # ── Pending removals ──

    def _clear_removal(self, record_uid):
        if self._pending_removals.pop(record_uid, None) is not None:
            self._save_removals()

    def pending_removals(self):
        return list(self._pending_removals.values())

    def _save_removals(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(REMOVALS_FILE, "w", encoding="utf-8") as handle:
                json.dump(list(self._pending_removals.values()), handle,
                          indent=2, default=str)
        except IOError as exc:
            print(f"[Roles:Sync] Could not persist pending removals: {exc}")

    def load_removals(self):
        """Re-queue revocations that were still pending at shutdown."""
        if not os.path.exists(REMOVALS_FILE):
            return

        try:
            with open(REMOVALS_FILE, "r", encoding="utf-8") as handle:
                entries = json.load(handle)
        except (IOError, json.JSONDecodeError) as exc:
            print(f"[Roles:Sync] Could not load pending removals: {exc}")
            return

        replayed = 0
        for entry in entries:
            job = SyncJob.from_dict(entry)
            if job is None:
                print(f"[Roles:Sync] Discarding unusable removal entry: "
                      f"{entry.get('record_uid', '?')}")
                continue
            # Reset the attempt counter: a fresh process gets a fresh budget.
            job.attempt = 0
            self._pending_removals[job.record_uid] = job.to_dict()
            self._pending_uids.add(job.record_uid)
            self._queue.put_nowait(job)
            replayed += 1

        if replayed:
            self._save_removals()
            print(f"[Roles:Sync] Replaying {replayed} pending revocation(s) from disk.")

    def _save_failures(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(FAILURES_FILE, "w", encoding="utf-8") as handle:
                json.dump(self._dead_letters, handle, indent=2, default=str)
        except IOError as exc:
            print(f"[Roles:Sync] Could not persist failures: {exc}")

    def load_failures(self):
        if not os.path.exists(FAILURES_FILE):
            return
        try:
            with open(FAILURES_FILE, "r", encoding="utf-8") as handle:
                self._dead_letters = json.load(handle)
            if self._dead_letters:
                print(f"[Roles:Sync] Loaded {len(self._dead_letters)} unresolved "
                      f"sync failure(s) from disk.")
        except (IOError, json.JSONDecodeError) as exc:
            print(f"[Roles:Sync] Could not load failures: {exc}")

    # ── Status ──

    def stats(self):
        return {
            **self._stats,
            "queued": self._queue.qsize(),
            "dead_letters": len(self._dead_letters),
            "pending_removals": len(self._pending_removals),
            "worker_running": bool(self._worker_task and not self._worker_task.done()),
        }


def _backoff_delay(cfg, attempt):
    backoff = cfg.sync.retry_backoff_seconds or [30]
    return backoff[min(attempt, len(backoff) - 1)]


def _exception_outcome(target, exc):
    from roles.discord_sync import SyncOutcome
    return SyncOutcome(
        target, False, f"{type(exc).__name__}: {exc}", retryable=True,
    )
