"""Canonical timezone handling for column E.

CANONICAL FORMAT: a fixed UTC offset, stored verbatim.

    UTC        UTC+1        UTC-5        UTC+5:30

Chosen over place names (IANA identifiers) because a member states how far they
are from UTC, not where they live. The cost is that an offset does not follow
daylight saving: a member whose clock changes has to set it again.

`normalize()` accepts the common ways of writing an offset ("+2", "utc +02:00",
"GMT-5") and resolves them to the canonical form.
"""

import re
from datetime import datetime, timedelta, timezone

#: Real-world offsets run from UTC-12 to UTC+14.
_MIN_MINUTES = -12 * 60
_MAX_MINUTES = 14 * 60

_ZERO_ALIASES = {"UTC", "GMT", "Z", "ZULU"}

_OFFSET_PATTERN = re.compile(
    r"^(?:UTC|GMT)?\s*([+-])?\s*(\d{1,2})(?:\s*:?\s*(\d{2}))?$"
)

#: Offsets that are not whole hours but are in real use, for autocomplete.
_FRACTIONAL_SUGGESTIONS = (
    "UTC-9:30", "UTC-3:30", "UTC+3:30", "UTC+4:30", "UTC+5:30", "UTC+5:45",
    "UTC+6:30", "UTC+8:45", "UTC+9:30", "UTC+10:30", "UTC+12:45",
)


class TimezoneResult:
    """Outcome of normalizing user input.

    `canonical` is set when the input resolved; otherwise `error` explains why
    it was rejected.
    """

    def __init__(self, canonical=None, error=""):
        self.canonical = canonical
        self.error = error

    @property
    def ok(self):
        return bool(self.canonical)


def _format(total_minutes):
    """Canonical string for an offset in minutes: 0 -> 'UTC', 330 -> 'UTC+5:30'."""
    if total_minutes == 0:
        return "UTC"
    sign = "+" if total_minutes > 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours}:{minutes:02d}" if minutes else f"UTC{sign}{hours}"


def _parse_minutes(raw):
    """Offset in minutes for any accepted spelling, or None if it is not one."""
    if raw is None:
        return None

    # Sheets and phone keyboards produce a typographic minus.
    text = str(raw).strip().upper().replace("−", "-").replace("–", "-")
    if not text:
        return None
    if text in _ZERO_ALIASES:
        return 0

    match = _OFFSET_PATTERN.match(text)
    if not match:
        return None

    sign, hours, minutes = match.groups()
    minutes = int(minutes or 0)
    if minutes not in (0, 15, 30, 45):
        return None

    total = int(hours) * 60 + minutes
    if sign == "-":
        total = -total
    if not _MIN_MINUTES <= total <= _MAX_MINUTES:
        return None
    return total


def is_valid(identifier):
    """True if `identifier` is a usable UTC offset in any accepted spelling."""
    return _parse_minutes(identifier) is not None


def normalize(raw):
    """Resolve user input to a canonical UTC offset.

    Returns a TimezoneResult. Never raises on bad input.
    """
    if not raw or not str(raw).strip():
        return TimezoneResult(error="No timezone provided.")

    total = _parse_minutes(raw)
    if total is None:
        return TimezoneResult(
            error=f"'{str(raw).strip()}' is not a recognized timezone. Use a UTC "
                  f"offset between UTC-12 and UTC+14, such as UTC+1, UTC-5 or UTC+5:30."
        )
    return TimezoneResult(canonical=_format(total))


def suggestions(text=""):
    """Canonical offsets matching partial input, for autocomplete."""
    whole_hours = [_format(hours * 60) for hours in range(-12, 15)]

    needle = "".join(str(text or "").upper().split())
    if not needle:
        # Closest to UTC first, so the cut-off at Discord's limit drops the extremes.
        return sorted(whole_hours, key=lambda o: abs(_parse_minutes(o)))

    # What the input already resolves to comes first; "UTC+1" still offers UTC+10.
    exact = normalize(text)
    options = [exact.canonical] if exact.ok else []
    for option in whole_hours + list(_FRACTIONAL_SUGGESTIONS):
        if needle in option and option not in options:
            options.append(option)
    return options


def describe(identifier):
    """'UTC+1 (14:32)' — for command responses, not storage.

    A value that is not an offset (one entered by hand, or an IANA name stored
    before the format changed) is shown as it is.
    """
    now = local_time(identifier)
    if now is None:
        return identifier or "unset"
    return f"{_format(_parse_minutes(identifier))} ({now.strftime('%H:%M')})"


def local_time(identifier, moment=None):
    """The record holder's local time. Returns None if the offset is unusable."""
    total = _parse_minutes(identifier)
    if total is None:
        return None
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone(timedelta(minutes=total)))
