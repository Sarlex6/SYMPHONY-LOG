"""Domain models for the role management system.

A UserRecord is the authoritative representation of one person. It belongs to
the *user*, not to a spreadsheet row: `row` is transient placement information
that changes freely as the sheet is re-laid-out, while `record_uid`,
`entry_date`, `discord_id` and `roblox_id` follow the person.
"""

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


# ── Enumerations ─────────────────────────────────────────────────────────────

class Category(str, Enum):
    """Column C. Section boundaries are dynamic; membership is derived from rank."""

    HIGH_RANK = "HIGH-RANK"
    MID_RANK = "MID-RANK"
    LOW_RANK = "LOW-RANK"

    @classmethod
    def parse(cls, raw):
        if not raw:
            return None
        cleaned = str(raw).strip().upper().replace("_", "-").replace(" ", "-")
        for member in cls:
            if member.value == cleaned:
                return member
        return None


class Status(str, Enum):
    """Column J."""

    ACTIVE = "ACTIVE"
    SEMI_ACTIVE = "SEMI-ACTIVE"
    INACTIVE = "IN-ACTIVE"
    #: Not a user state. Marks a managed row that exists only because the sheet
    #: has not been resized around a missing record yet. The layout pass removes
    #: these rather than leaving them in place.
    EMPTY = "EMPTY"

    @classmethod
    def parse(cls, raw):
        if not raw:
            return None
        cleaned = str(raw).strip().upper().replace("_", "-").replace(" ", "-")
        aliases = {"INACTIVE": cls.INACTIVE, "SEMIACTIVE": cls.SEMI_ACTIVE}
        for member in cls:
            if member.value == cleaned:
                return member
        return aliases.get(cleaned.replace("-", ""))


class SyncStatus(str, Enum):
    """Technical column T — outcome of the last outbound synchronization."""

    NEVER = "NEVER"
    PENDING = "PENDING"
    OK = "OK"
    FAILED = "FAILED"
    #: Synced as far as possible; some targets reported the user as absent.
    PARTIAL = "PARTIAL"


class ChangeSource(str, Enum):
    """Technical column X — provenance of the last observed change."""

    SYSTEM = "SYSTEM"
    MANUAL = "MANUAL"


class ActionOrigin(str, Enum):
    """Where a mutation request entered the system.

    Purely informational for logging and messaging. It carries NO authority:
    authorization always resolves the acting Discord user's record from the
    sheet, regardless of origin.
    """

    DISCORD_COMMAND = "DISCORD_COMMAND"
    ANGELA = "ANGELA"
    SYSTEM = "SYSTEM"


class ActionStatus(str, Enum):
    OK = "OK"
    DENIED = "DENIED"
    INVALID = "INVALID"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"


class SyncTarget(str, Enum):
    DISCORD_MAIN = "DISCORD_MAIN"
    DISCORD_BRANCH = "DISCORD_BRANCH"
    ROBLOX = "ROBLOX"


#: Verification marker written to column L for system-performed changes.
VERIFICATION_SYSTEM_MARKER = "AUTOMATIC EXECUTIVE SYSTEM"

#: Column K format. Two digits for month and day, e.g. 02.07.2026.
ENTRY_DATE_FORMAT = "%m.%d.%Y"


def format_entry_date(dt=None):
    return (dt or datetime.utcnow()).strftime(ENTRY_DATE_FORMAT)


def parse_entry_date(raw):
    """Parse MM.DD.YYYY. Returns None rather than raising on malformed input —
    a bad date must never block reading an authoritative record."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), ENTRY_DATE_FORMAT)
    except ValueError:
        return None


# ── The record ───────────────────────────────────────────────────────────────

@dataclass
class UserRecord:
    """One managed person. Mirrors one row of the PERSONNEL sheet."""

    # ── Identity (canonical, never derived from usernames) ──
    discord_id: int = 0
    roblox_id: int = 0
    record_uid: str = ""

    # ── Human-facing columns ──
    category: "Category | None" = None
    discord_username: str = ""          # column D, display only
    timezone: str = ""                  # column E, canonical IANA identifier
    rank_key: str = ""                  # column F, key into RankRegistry
    branch_key: str = ""                # column G, key into BranchRegistry
    notes: str = ""                     # column H, carried but never authored
    status: "Status | None" = None      # column J
    entry_date: str = ""                # column K, MM.DD.YYYY, write-once
    verification: str = ""              # column L, provenance marker

    # ── Technical columns ──
    loa_raw: str = ""
    sync_hash: str = ""
    sync_status: SyncStatus = SyncStatus.NEVER
    last_sync_at: str = ""
    revision: int = 0
    source: ChangeSource = ChangeSource.SYSTEM

    # ── Transient placement (not part of the user's identity) ──
    row: int = 0

    def is_empty_row(self):
        """True for a placeholder row rather than a real person."""
        if self.status is Status.EMPTY:
            return True
        return not (self.discord_id or self.roblox_id or self.discord_username.strip())

    def is_incomplete(self):
        """A row with human-facing content but no bot identity.

        These are rows entered by hand before this system existed: they have a
        name, a rank and so on in the visible columns, but no Discord ID and no
        record UID in the technical columns. The system cannot look them up,
        authorize them, or synchronize them to anything — the person has to
        /register before the row becomes a real record.

        Reported by /roles status and cleared by /roles restructure with
        purge_incomplete enabled.
        """
        if self.is_empty_row():
            return False
        return not (self.record_uid and self.discord_id)

    def sync_fingerprint(self):
        """Hash of the fields that outbound synchronization depends on.

        Deliberately excludes notes, timezone, entry date and verification:
        editing those must not trigger a Discord/Roblox role write.
        """
        payload = "|".join(str(part) for part in (
            self.discord_id,
            self.roblox_id,
            self.rank_key.strip().upper(),
            self.branch_key.strip().upper(),
            self.category.value if self.category else "",
            self.status.value if self.status else "",
        ))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def needs_sync(self):
        return self.sync_fingerprint() != self.sync_hash

    def with_changes(self, **changes):
        """Return a copy with fields replaced. Records are treated as immutable
        snapshots outside the repository so a stale copy can never be written back."""
        return replace(self, **changes)

    def label(self):
        """Short human label for logs and command responses."""
        name = self.discord_username.strip() or f"<@{self.discord_id}>"
        return f"{name} ({self.rank_key or 'no rank'})"


def new_record_uid():
    return uuid.uuid4().hex[:12]


# ── Action plumbing (shared by Discord commands and Angela) ─────────────────

@dataclass
class ActionContext:
    """Who is asking, and through what interface.

    `actor_discord_id` is the ONLY identity input the authorization layer trusts.
    Callers cannot supply a rank, a category or a permission claim — those are
    resolved from the authoritative sheet record for this Discord ID.
    """

    actor_discord_id: int
    origin: ActionOrigin = ActionOrigin.DISCORD_COMMAND
    guild_id: "int | None" = None
    channel_id: "int | None" = None
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def log_prefix(self):
        return f"[Roles:{self.correlation_id}]"


@dataclass
class ActionResult:
    """Outcome of a role-management action.

    Both the Discord command handler and Angela render from this same object, so
    there is exactly one description of what happened.
    """

    status: ActionStatus
    message: str
    record: "UserRecord | None" = None
    #: Machine-readable detail for Angela to phrase a dynamic response around.
    detail: dict = field(default_factory=dict)
    #: Targets queued for synchronization, if the authoritative write succeeded.
    queued_targets: list = field(default_factory=list)

    @property
    def ok(self):
        return self.status is ActionStatus.OK

    @classmethod
    def denied(cls, message, **detail):
        return cls(ActionStatus.DENIED, message, detail=detail)

    @classmethod
    def invalid(cls, message, **detail):
        return cls(ActionStatus.INVALID, message, detail=detail)

    @classmethod
    def not_found(cls, message, **detail):
        return cls(ActionStatus.NOT_FOUND, message, detail=detail)

    @classmethod
    def error(cls, message, **detail):
        return cls(ActionStatus.ERROR, message, detail=detail)

    @classmethod
    def success(cls, message, record=None, queued_targets=None, **detail):
        return cls(
            ActionStatus.OK,
            message,
            record=record,
            detail=detail,
            queued_targets=list(queued_targets or []),
        )
