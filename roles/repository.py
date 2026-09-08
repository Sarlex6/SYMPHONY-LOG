"""UserRecord persistence on top of the PERSONNEL worksheet.

Owns the translation between sheet rows and UserRecords, the in-memory snapshot
of the authoritative state, and change detection between polls.

Concurrency model
-----------------
Every mutation takes ``sheets_gateway.write_lock`` for its whole read-modify-
write cycle, so a command and a poll cycle can never interleave. Each record
carries a monotonic ``revision`` bumped on every system write; a synchronization
job tagged with revision N is dropped once the snapshot holds a higher revision.
That is what stops a slow, stale poll result from overwriting a newer state.
"""

import asyncio
from datetime import datetime

import gspread

from roles import columns, layout
from roles import config as roles_config
from roles.errors import RecordNotFoundError, SheetStructureError
from roles.loa import LOAValue
from roles.models import (
    Category,
    ChangeSource,
    Status,
    SyncStatus,
    UserRecord,
    VERIFICATION_SYSTEM_MARKER,
    new_record_uid,
)


# ── Row <-> record translation ───────────────────────────────────────────────

def _cell(row, col):
    """1-indexed column access into a padded row list."""
    index = col - 1
    return row[index].strip() if 0 <= index < len(row) and row[index] else ""


def _as_int(value):
    if not value:
        return 0
    try:
        return int(str(value).strip().split(".")[0])
    except (TypeError, ValueError):
        return 0


def _resolve_rank_key(raw, cfg):
    """Column F holds the dropdown *display* string; the record holds the key.

    Falls back to the raw uppercased text when the rank is not configured, so an
    unrecognized value is preserved verbatim and reported by
    layout.unknown_ranks() rather than being silently dropped.
    """
    if not raw:
        return ""
    rank = cfg.rank_by_display(raw) or cfg.rank(raw)
    return rank.key if rank else raw.strip().upper()


def _resolve_branch_key(raw, cfg):
    """Same as _resolve_rank_key, for column G."""
    if not raw:
        return ""
    branch = cfg.branch_by_display(raw) or cfg.branch(raw)
    return branch.key if branch else raw.strip().upper()


def row_to_record(row, row_number, cfg=None):
    """Parse one sheet row. Never raises — the sheet is authoritative, so a
    malformed cell degrades to a default rather than blocking the whole read."""
    cfg = cfg or roles_config.current()
    return UserRecord(
        row=row_number,
        category=Category.parse(_cell(row, columns.COL_CATEGORY)),
        discord_username=_cell(row, columns.COL_IDENTIFICATION),
        timezone=_cell(row, columns.COL_TIMEZONE),
        rank_key=_resolve_rank_key(_cell(row, columns.COL_RANK), cfg),
        branch_key=_resolve_branch_key(_cell(row, columns.COL_BRANCH), cfg),
        notes=_cell(row, columns.COL_NOTES),
        status=Status.parse(_cell(row, columns.COL_STATUS)),
        entry_date=_cell(row, columns.COL_ENTRY_DATE),
        verification=_cell(row, columns.COL_VERIFICATION),
        discord_id=_as_int(_cell(row, columns.COL_TECH_DISCORD_ID)),
        roblox_id=_as_int(_cell(row, columns.COL_TECH_ROBLOX_ID)),
        record_uid=_cell(row, columns.COL_TECH_RECORD_UID),
        sync_hash=_cell(row, columns.COL_TECH_SYNC_HASH),
        sync_status=_parse_enum(SyncStatus, _cell(row, columns.COL_TECH_SYNC_STATUS), SyncStatus.NEVER),
        last_sync_at=_cell(row, columns.COL_TECH_LAST_SYNC_AT),
        revision=_as_int(_cell(row, columns.COL_TECH_REVISION)),
        loa_raw=_cell(row, columns.COL_TECH_LOA),
        source=_parse_enum(ChangeSource, _cell(row, columns.COL_TECH_SOURCE), ChangeSource.MANUAL),
    )


def _parse_enum(enum_cls, raw, default):
    if not raw:
        return default
    try:
        return enum_cls(str(raw).strip().upper())
    except ValueError:
        return default


def record_to_cells(record, row_number, cfg, include_notes=None):
    """Every cell for one record at one row.

    Column F and G are written as the configured *display* strings so they match
    the dropdown; the internal key stays in the record.
    """
    rank = cfg.rank(record.rank_key)
    branch = cfg.branch(record.branch_key)

    rank_display = rank.display if rank else record.rank_key
    branch_display = branch.display if branch else record.branch_key
    category = layout.effective_category(record, cfg)

    values = {
        columns.COL_CATEGORY: category.value if category else "",
        columns.COL_IDENTIFICATION: record.discord_username,
        columns.COL_TIMEZONE: record.timezone,
        columns.COL_RANK: rank_display,
        columns.COL_BRANCH: branch_display,
        columns.COL_STATUS: record.status.value if record.status else "",
        columns.COL_ENTRY_DATE: record.entry_date,
        columns.COL_VERIFICATION: record.verification,

        columns.COL_TECH_DISCORD_ID: str(record.discord_id) if record.discord_id else "",
        columns.COL_TECH_ROBLOX_ID: str(record.roblox_id) if record.roblox_id else "",
        columns.COL_TECH_RECORD_UID: record.record_uid,
        columns.COL_TECH_SYNC_HASH: record.sync_hash,
        columns.COL_TECH_SYNC_STATUS: record.sync_status.value,
        columns.COL_TECH_LAST_SYNC_AT: record.last_sync_at,
        columns.COL_TECH_REVISION: str(record.revision),
        columns.COL_TECH_LOA: record.loa_raw,
        columns.COL_TECH_SOURCE: record.source.value,
    }

    # Notes are never authored by the system, but they have to travel with the
    # record when it relocates or they would reattach to the wrong person.
    should_move_notes = columns.MOVE_NOTES_WITH_RECORD if include_notes is None else include_notes
    if should_move_notes:
        values[columns.COL_NOTES] = record.notes

    return [
        gspread.Cell(row=row_number, col=col, value=value)
        for col, value in values.items()
    ]


def blank_row_cells(row_number):
    """Cells that reset a managed row to the EMPTY maintenance state."""
    values = {col: "" for col in columns.WRITABLE_COLUMNS}
    values[columns.COL_STATUS] = Status.EMPTY.value
    for col in range(columns.TECH_COL_START, columns.TECH_COL_END + 1):
        values[col] = ""
    if columns.MOVE_NOTES_WITH_RECORD:
        values[columns.COL_NOTES] = ""
    return [
        gspread.Cell(row=row_number, col=col, value=value)
        for col, value in values.items()
    ]


# ── Change detection ─────────────────────────────────────────────────────────

class RecordChange:
    """One observed difference between two snapshots of a record."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"

    def __init__(self, kind, record, previous=None, fields=None):
        self.kind = kind
        self.record = record
        self.previous = previous
        self.fields = fields or []

    def __repr__(self):
        return f"RecordChange({self.kind}, {self.record.label()}, {self.fields})"

    def affects_sync(self):
        """Whether this change requires touching Discord or Roblox."""
        if self.kind in (RecordChange.ADDED, RecordChange.REMOVED):
            return True
        return bool({"rank_key", "branch_key", "discord_id", "roblox_id"} & set(self.fields))


#: Fields compared when detecting a manual edit. Excludes technical bookkeeping
#: so the system's own writes do not read back as user changes.
_COMPARED_FIELDS = (
    "category", "discord_username", "timezone", "rank_key", "branch_key",
    "status", "entry_date", "discord_id", "roblox_id", "loa_raw",
)


def diff_records(previous_by_uid, current_records):
    """Compare a previous snapshot against a fresh read."""
    changes = []
    seen = set()

    for record in current_records:
        if record.is_empty_row():
            continue

        # Rows entered by hand before this system existed have no identity
        # columns. Reporting them as new on every poll would queue work that can
        # never run and flood the log; they are surfaced by the integrity report
        # instead, and cleared by /roles restructure.
        if record.is_incomplete():
            continue

        uid = record.record_uid
        if not uid:
            changes.append(RecordChange(RecordChange.ADDED, record))
            continue

        seen.add(uid)
        previous = previous_by_uid.get(uid)

        if previous is None:
            changes.append(RecordChange(RecordChange.ADDED, record))
            continue

        changed_fields = [
            field for field in _COMPARED_FIELDS
            if getattr(previous, field) != getattr(record, field)
        ]
        if changed_fields:
            changes.append(
                RecordChange(RecordChange.MODIFIED, record, previous, changed_fields)
            )

    for uid, previous in previous_by_uid.items():
        if uid not in seen:
            changes.append(RecordChange(RecordChange.REMOVED, previous, previous))

    return changes


# ── Repository ───────────────────────────────────────────────────────────────

class PersonnelRepository:
    """Read/write access to PERSONNEL records, plus the authoritative snapshot.

    A single instance is shared by the command handlers, the poller and Angela,
    so they all see the same state and contend on the same lock.
    """

    def __init__(self, gateway=None):
        # Injected so tests can substitute a fake gateway.
        if gateway is None:
            from roles import sheets_gateway
            gateway = sheets_gateway
        self.gateway = gateway

        self._records = []              # last read, in sheet order
        self._by_uid = {}
        self._by_discord_id = {}
        self._footer_row = columns.EXPECTED_FOOTER_ROW
        self._loaded_at = None
        self._generation = 0            # bumped on every successful read

    # ── State access ──

    @property
    def generation(self):
        return self._generation

    @property
    def footer_row(self):
        return self._footer_row

    @property
    def loaded_at(self):
        return self._loaded_at

    def all_records(self):
        return list(self._records)

    def by_uid(self, uid):
        return self._by_uid.get(uid)

    def by_discord_id(self, discord_id):
        """The canonical lookup. Usernames are never used as identifiers."""
        return self._by_discord_id.get(int(discord_id)) if discord_id else None

    def by_roblox_id(self, roblox_id):
        if not roblox_id:
            return None
        roblox_id = int(roblox_id)
        for record in self._records:
            if record.roblox_id == roblox_id:
                return record
        return None

    def snapshot_by_uid(self):
        return dict(self._by_uid)

    def is_stale(self, record):
        """True if the snapshot has moved past this copy of the record.

        Sync workers call this before applying a job, so an older result can
        never overwrite a newer state.
        """
        current = self._by_uid.get(record.record_uid)
        return current is not None and current.revision > record.revision

    # ── Reads ──

    async def load(self):
        """Refresh the snapshot from the sheet. Returns the records read."""
        cfg = roles_config.current()
        rows, footer_row = await self.gateway.read_managed_block()

        records = []
        for offset, row in enumerate(rows):
            record = row_to_record(row, columns.FIRST_MANAGED_ROW + offset, cfg)
            records.append(record)

        self._records = records
        self._footer_row = footer_row
        self._by_uid = {r.record_uid: r for r in records if r.record_uid and not r.is_empty_row()}
        self._by_discord_id = {
            r.discord_id: r for r in records if r.discord_id and not r.is_empty_row()
        }
        self._loaded_at = datetime.utcnow()
        self._generation += 1

        return records

    async def refresh_and_diff(self):
        """Reload and report what changed since the previous snapshot.

        This is how a manual spreadsheet edit becomes an authoritative change:
        it is detected here and treated exactly like a command-originated one.
        """
        previous = self.snapshot_by_uid()
        await self.load()
        return diff_records(previous, self._records)

    def live_records(self):
        """Records that represent real people."""
        return [r for r in self._records if not r.is_empty_row()]

    # ── Writes ──

    async def save_record(self, record, mark_system=True):
        """Persist one record in place and re-lay-out the sheet if it moved.

        Caller must hold `gateway.write_lock`.
        """
        cfg = roles_config.current()

        record = record.with_changes(revision=record.revision + 1)
        if mark_system:
            record = record.with_changes(
                verification=VERIFICATION_SYSTEM_MARKER,
                source=ChangeSource.SYSTEM,
            )
        if not record.record_uid:
            record = record.with_changes(record_uid=new_record_uid())

        # Replace in the working set, then let the layout pass position everyone.
        working = [r for r in self.live_records() if r.record_uid != record.record_uid]
        working.append(record)

        await self._apply_layout(working, cfg)
        return self._by_uid.get(record.record_uid, record)

    async def delete_record(self, record):
        """Remove a record and close the gap it leaves.

        Caller must hold `gateway.write_lock`.
        """
        cfg = roles_config.current()
        working = [r for r in self.live_records() if r.record_uid != record.record_uid]
        await self._apply_layout(working, cfg)

    async def restructure(self, purge_incomplete=False):
        """Re-sort and resize the whole managed area.

        `purge_incomplete=True` also drops rows that were entered by hand and
        carry no bot identity, clearing the sheet for first use. Destructive, so
        it is never the default — the caller has to ask for it.

        Returns (plan, purged_records). Caller must hold `gateway.write_lock`.
        """
        cfg = roles_config.current()
        await self.load()

        records = self.live_records()
        purged = []

        if purge_incomplete:
            purged = [r for r in records if r.is_incomplete()]
            records = [r for r in records if not r.is_incomplete()]
            for record in purged:
                print(f"[Roles] Purging incomplete row {record.row}: "
                      f"{record.discord_username or '(no name)'} "
                      f"({record.rank_key or 'no rank'})")

        plan = await self._apply_layout(records, cfg)
        return plan, purged

    async def _apply_layout(self, records, cfg):
        """Resize, reorder and rewrite the managed area in one pass.

        Values only: design columns and every cell format, dropdown and
        conditional-format rule stay attached to their physical rows.
        """
        plan = layout.plan_layout(records, cfg, self._footer_row)

        if plan.rows_to_insert:
            self._footer_row = await self.gateway.insert_managed_rows(
                plan.rows_to_insert, self._footer_row
            )

        cells = []
        for index, record in enumerate(plan.ordered_records):
            row_number = columns.FIRST_MANAGED_ROW + index
            positioned = record.with_changes(
                row=row_number,
                category=layout.effective_category(record, cfg),
            )
            cells.extend(record_to_cells(positioned, row_number, cfg))

        # Blank the tail before shrinking, so a failed delete leaves EMPTY rows
        # rather than a duplicated copy of somebody's record.
        for row_number in plan.rows_to_clear:
            cells.extend(blank_row_cells(row_number))

        if cells:
            await self.gateway.write_cells(cells)

        if plan.rows_to_delete:
            self._footer_row = await self.gateway.delete_managed_rows(
                plan.rows_to_delete, self._footer_row
            )

        await self.load()

        if plan.changed:
            print(f"[Roles] Layout applied: {plan.summary()}")
        return plan

    async def mark_synced(self, record, sync_status, targets_note=""):
        """Record the outcome of an outbound synchronization.

        Writes ONLY technical columns. A sync result must never be able to alter
        rank, branch, status or any other authoritative field — that is what
        keeps a failed Discord push from corrupting the sheet.
        """
        current = self._by_uid.get(record.record_uid)
        if current is None:
            raise RecordNotFoundError(
                f"Record {record.record_uid} disappeared before its sync result was written."
            )

        updated = current.with_changes(
            sync_status=sync_status,
            last_sync_at=datetime.utcnow().isoformat(timespec="seconds"),
            sync_hash=current.sync_fingerprint() if sync_status is SyncStatus.OK else current.sync_hash,
        )

        cells = [
            gspread.Cell(row=current.row, col=columns.COL_TECH_SYNC_STATUS,
                         value=updated.sync_status.value),
            gspread.Cell(row=current.row, col=columns.COL_TECH_LAST_SYNC_AT,
                         value=updated.last_sync_at),
            gspread.Cell(row=current.row, col=columns.COL_TECH_SYNC_HASH,
                         value=updated.sync_hash),
        ]

        async with self.gateway.write_lock:
            await self.gateway.write_cells(cells)

        self._by_uid[current.record_uid] = updated
        if updated.discord_id:
            self._by_discord_id[updated.discord_id] = updated
        self._records = [
            updated if r.record_uid == updated.record_uid else r for r in self._records
        ]

        if targets_note:
            print(f"[Roles] {updated.label()} sync -> {sync_status.value}: {targets_note}")

        return updated

    async def note_manual_change(self, record):
        """Acknowledge a change the system did not make.

        The data stays authoritative and is synchronized outward. Column L is
        NOT overwritten: it is a provenance marker, and stamping it here would
        erase the evidence that a human made the edit. Only the technical
        SOURCE column is updated.
        """
        cells = [
            gspread.Cell(row=record.row, col=columns.COL_TECH_SOURCE,
                         value=ChangeSource.MANUAL.value)
        ]
        async with self.gateway.write_lock:
            await self.gateway.write_cells(cells)

        updated = record.with_changes(source=ChangeSource.MANUAL)
        self._by_uid[updated.record_uid] = updated
        return updated

    # ── Maintenance ──

    async def ensure_structure(self):
        """One-time setup: hide the technical columns, optionally install the
        status colours and rebuild the rank/branch dropdowns from configuration."""
        cfg = roles_config.current()

        await self.gateway.ensure_technical_columns()

        if cfg.sync.manage_status_formatting:
            await self.gateway.ensure_status_formatting(self._footer_row)

        if cfg.ranks:
            await self.gateway.set_dropdown_values(
                columns.COL_RANK,
                [r.display for r in cfg.normal_ranks()] + [r.display for r in cfg.special_ranks()],
                self._footer_row,
            )
        if cfg.branches:
            await self.gateway.set_dropdown_values(
                columns.COL_BRANCH,
                [b.display for b in cfg.branches.values()],
                self._footer_row,
            )

    def integrity_report(self):
        """Structural problems worth a human's attention. Never auto-resolved."""
        cfg = roles_config.current()
        records = self.live_records()
        incomplete = [r for r in records if r.is_incomplete()]
        return {
            "records": len(records),
            "incomplete": incomplete,
            "sheet_ready": not incomplete,
            "footer_row": self._footer_row,
            "empty_rows": len(layout.find_empty_rows(self._records)),
            "misplaced": layout.find_misplaced(records, cfg),
            "duplicates": layout.find_duplicates(records),
            "unknown_ranks": layout.unknown_ranks(records, cfg),
            "unsynced": [r for r in records if r.needs_sync()],
        }
