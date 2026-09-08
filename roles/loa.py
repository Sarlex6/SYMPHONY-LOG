"""LOA (leave of absence) handling — DELIBERATELY INCOMPLETE.

The `#/#/#` LOA value format has not been defined yet. What the three fields
mean, whether they are dates, durations, counts or something else entirely, is
unknown — and inventing a meaning here would bake a wrong assumption into the
authoritative record.

So this module does exactly two things today:

  1. Validates the *shape* of the input (three slash-separated fields) without
     assigning any semantics to it.
  2. Stores the raw string opaquely in technical column W, round-tripping it
     unchanged.

Everything that requires knowing what the fields MEAN is marked TODO below and
raises / returns "not implemented" rather than guessing.

Isolated in its own module precisely so that defining the format later is a
change to this one file plus the storage column, not a change across the system.
"""

import re

from roles.models import Status

# ── Shape validation only ────────────────────────────────────────────────────

#: Three slash-separated fields. Each field is digits, optionally with a sign or
#: a decimal point, because we do not yet know whether they are dates or counts.
_SHAPE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*$")

#: How the raw value is persisted. Currently the raw user string, normalized
#: only for surrounding whitespace. No reordering, no reinterpretation.
STORAGE_COLUMN_NOTE = "technical column W (COL_TECH_LOA)"


class LOAValue:
    """An opaque LOA payload.

    Holds the three fields as strings. It intentionally exposes no `start_date`,
    `end_date` or `duration` accessor — adding one requires knowing the format.
    """

    __slots__ = ("raw", "fields")

    def __init__(self, raw, fields=None):
        self.raw = (raw or "").strip()
        self.fields = tuple(fields) if fields else ()

    def __bool__(self):
        return bool(self.raw)

    def __repr__(self):
        return f"LOAValue({self.raw!r})"

    def to_storage(self):
        """Exact string written to the sheet."""
        return self.raw

    @classmethod
    def from_storage(cls, raw):
        """Read back whatever is in the cell. Never rejects — the sheet is
        authoritative, so an unparseable value is preserved as-is."""
        raw = (raw or "").strip()
        if not raw:
            return cls("")
        match = _SHAPE.match(raw)
        return cls(raw, match.groups() if match else ())

    def is_well_formed(self):
        return bool(self.fields)


class LOAParseResult:
    def __init__(self, value=None, error=""):
        self.value = value
        self.error = error

    @property
    def ok(self):
        return self.value is not None and not self.error


def parse(raw):
    """Validate the shape of a user-supplied LOA value.

    Accepts the `#/#/#` shape and stores it verbatim. Rejects everything else
    with a message that says plainly that the format is still being defined.
    """
    if raw is None or not str(raw).strip():
        return LOAParseResult(error="No LOA value provided.")

    text = str(raw).strip()

    # Clearing an LOA is unambiguous regardless of the pending format.
    if text.upper() in ("NONE", "CLEAR", "OFF", "-", "0"):
        return LOAParseResult(value=LOAValue(""))

    match = _SHAPE.match(text)
    if not match:
        return LOAParseResult(
            error=(
                f"'{text}' does not match the expected `#/#/#` shape. "
                "The meaning of the three values has not been defined yet, so only "
                "that shape is accepted for now."
            )
        )

    return LOAParseResult(value=LOAValue(text, match.groups()))


def describe(value):
    """Render an LOA value for a command response.

    Shows the raw value only. It cannot say 'returns on X' without knowing the
    format, and saying it wrongly would be worse than not saying it.
    """
    if not value or not value.raw:
        return "no LOA on record"
    return f"`{value.raw}` (raw — LOA field semantics not yet defined)"


# ── Deferred behavior ────────────────────────────────────────────────────────

def implied_status(value):
    """What STATUS (column J) an active LOA should imply, if any.

    TODO: requires the LOA format. Deciding whether an LOA forces SEMI-ACTIVE or
    IN-ACTIVE depends on what the fields mean. Returns None = leave status alone.
    """
    return None


def is_expired(value, now=None):
    """Whether an LOA has elapsed.

    TODO: requires the LOA format. Returns None ("unknown") rather than False, so
    callers cannot mistake "cannot tell" for "still active".
    """
    return None


def sweep_expired(records, now=None):
    """Find records whose LOA has elapsed, for automatic status restoration.

    TODO: requires the LOA format and a policy decision on what expiry should do.
    Returns an empty list until then; the background poller calls it but has
    nothing to act on yet.
    """
    return []


#: Statuses an LOA may legitimately coexist with, once the format is known.
#: TODO: confirm — EMPTY is excluded because it is a sheet-maintenance state,
#: not a user state.
LOA_COMPATIBLE_STATUSES = (Status.ACTIVE, Status.SEMI_ACTIVE, Status.INACTIVE)
