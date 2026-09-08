"""Canonical timezone handling for column E.

CANONICAL FORMAT: an IANA time zone database identifier, stored verbatim.

    Europe/Prague        America/New_York        Asia/Tokyo

Chosen over free-text and over UTC offsets because:
  - it is unambiguous ("CST" is three different zones; "UTC+1" is not a place)
  - it survives daylight saving transitions, so an offset never goes stale
  - it validates against the standard library (`zoneinfo`), no service required

Users are not expected to type IANA names. `normalize()` accepts common
abbreviations and offset forms and resolves them to a canonical identifier, or
returns a list of candidates for the user to pick from when genuinely ambiguous.
"""

from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo, available_timezones
    _ZONEINFO_AVAILABLE = True
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None
    available_timezones = None
    _ZONEINFO_AVAILABLE = False


class TimezoneResult:
    """Outcome of normalizing user input.

    Exactly one of `canonical` (resolved) or `candidates` (ambiguous) is set;
    if neither, `error` explains why the input was rejected.
    """

    def __init__(self, canonical=None, candidates=None, error=""):
        self.canonical = canonical
        self.candidates = candidates or []
        self.error = error

    @property
    def ok(self):
        return bool(self.canonical)

    @property
    def ambiguous(self):
        return not self.canonical and bool(self.candidates)


# ── Convenience aliases ──────────────────────────────────────────────────────
# Unambiguous shorthands only. Deliberately excludes genuinely ambiguous
# abbreviations (CST, IST, BST, ...) — those go through the candidate path so
# the user picks rather than the system guessing wrong.

_ALIASES = {
    "UTC": "UTC",
    "GMT": "Etc/GMT",
    "Z": "UTC",
    "ZULU": "UTC",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MT": "America/Denver",
    "CDT": "America/Chicago",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "EET": "Europe/Athens",
    "EEST": "Europe/Athens",
    "WET": "Europe/Lisbon",
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "NZST": "Pacific/Auckland",
    "NZDT": "Pacific/Auckland",
    "MSK": "Europe/Moscow",
}

#: Abbreviations that map to more than one real zone. Offering a short candidate
#: list beats silently picking one.
_AMBIGUOUS = {
    "CST": ["America/Chicago", "Asia/Shanghai", "America/Havana"],
    "IST": ["Asia/Kolkata", "Europe/Dublin", "Asia/Jerusalem"],
    "BST": ["Europe/London", "Pacific/Bougainville"],
    "AST": ["America/Halifax", "Asia/Riyadh"],
    "WST": ["Pacific/Apia", "Australia/Perth"],
}


def is_valid(identifier):
    """True if `identifier` is a loadable IANA zone."""
    if not identifier or not _ZONEINFO_AVAILABLE:
        return False
    try:
        ZoneInfo(str(identifier).strip())
        return True
    except Exception:
        return False


def _offset_to_zone(text):
    """Map 'UTC+2' / '+02:00' / 'GMT-5' onto an Etc/GMT zone.

    Etc/GMT signs are inverted by POSIX convention: UTC+2 is 'Etc/GMT-2'.
    """
    cleaned = text.upper().replace("UTC", "").replace("GMT", "").strip()
    if not cleaned or cleaned[0] not in "+-":
        return None

    sign = 1 if cleaned[0] == "+" else -1
    body = cleaned[1:].strip()
    hours_part = body.split(":")[0].strip() if body else ""
    if not hours_part.isdigit():
        return None

    hours = int(hours_part)
    if hours > 14:
        return None

    # Etc/GMT+0 does not exist; UTC is the canonical zero.
    if hours == 0:
        return "UTC"
    return f"Etc/GMT{'-' if sign > 0 else '+'}{hours}"


def _search(needle):
    """Fuzzy-match against the tz database, e.g. 'prague' -> 'Europe/Prague'."""
    if not _ZONEINFO_AVAILABLE or available_timezones is None:
        return []

    needle = needle.strip().casefold().replace(" ", "_")
    if not needle:
        return []

    zones = available_timezones()

    exact_city = [z for z in zones if z.rsplit("/", 1)[-1].casefold() == needle]
    if exact_city:
        return sorted(exact_city)[:10]

    partial = [z for z in zones if needle in z.casefold()]
    return sorted(partial)[:10]


def normalize(raw):
    """Resolve user input to a canonical IANA identifier.

    Returns a TimezoneResult. Never raises on bad input.
    """
    if not raw or not str(raw).strip():
        return TimezoneResult(error="No timezone provided.")

    if not _ZONEINFO_AVAILABLE:
        return TimezoneResult(
            error="Timezone support unavailable: the 'zoneinfo' module is missing. "
                  "Install the 'tzdata' package."
        )

    text = str(raw).strip()
    upper = text.upper()

    if upper in _AMBIGUOUS:
        return TimezoneResult(candidates=_AMBIGUOUS[upper])

    if upper in _ALIASES:
        candidate = _ALIASES[upper]
        return TimezoneResult(canonical=candidate) if is_valid(candidate) else TimezoneResult(
            error=f"Alias '{text}' maps to '{candidate}', which is not in this system's tz database."
        )

    # Already canonical (case-corrected against the database).
    if is_valid(text):
        return TimezoneResult(canonical=text)
    for zone in _search(text) if "/" in text else []:
        if zone.casefold() == text.casefold():
            return TimezoneResult(canonical=zone)

    offset_zone = _offset_to_zone(text)
    if offset_zone and is_valid(offset_zone):
        return TimezoneResult(canonical=offset_zone)

    matches = _search(text)
    if len(matches) == 1:
        return TimezoneResult(canonical=matches[0])
    if matches:
        return TimezoneResult(candidates=matches)

    return TimezoneResult(
        error=f"'{text}' is not a recognized timezone. Use an IANA identifier "
              f"such as Europe/Prague, America/New_York or Asia/Tokyo."
    )


def current_offset(identifier):
    """Current UTC offset string for display, e.g. '+01:00'. Empty on failure."""
    if not is_valid(identifier):
        return ""
    now = datetime.now(ZoneInfo(identifier))
    offset = now.utcoffset()
    if offset is None:
        return ""
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def describe(identifier):
    """'Europe/Prague (UTC+01:00, 14:32)' — for command responses, not storage."""
    if not is_valid(identifier):
        return identifier or "unset"
    now = datetime.now(ZoneInfo(identifier))
    return f"{identifier} (UTC{current_offset(identifier)}, {now.strftime('%H:%M')})"


def local_time(identifier, moment=None):
    """The record holder's local time. Returns None if the zone is unusable."""
    if not is_valid(identifier):
        return None
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(ZoneInfo(identifier))
