"""Physical layout of the PERSONNEL sheet.

Everything that knows about *where* data lives on the sheet lives here. No other
module should hardcode a column letter or a row number.

Column map (1-indexed, matching gspread):

    A  1   design only        NEVER WRITTEN
    B  2   design only        NEVER WRITTEN
    C  3   CATEGORY           managed
    D  4   IDENTIFICATION     managed (Discord username — display only)
    E  5   TIMEZONE           managed
    F  6   RANK               managed (dropdown / data validation)
    G  7   BRANCH             managed (dropdown / data validation)
    H  8   NOTES              carried, never authored — see MOVE_NOTES_WITH_RECORD
    I  9   design only        NEVER WRITTEN
    J  10  STATUS             managed
    K  11  DATE OF ENTRY      managed, write-once
    L  12  VERIFICATION       managed (provenance marker, not an authority marker)
    M  13  design only        NEVER WRITTEN
    N  14  gap                NEVER WRITTEN (visual separation)
    O  15  gap                NEVER WRITTEN (visual separation)
    P+ 16+ technical columns  bot-managed, hidden
"""

# ── Sheet identity ───────────────────────────────────────────────────────────

#: Worksheet title. Resolved by name first; SHEET_INDEX is the fallback.
SHEET_NAME = "PERSONNEL"

#: 0-based position in the spreadsheet ("5th sheet position").
SHEET_INDEX = 4

# ── Row layout ───────────────────────────────────────────────────────────────

#: First row of the managed user area. Nothing at or above row 9 is ever touched.
FIRST_MANAGED_ROW = 10

#: Expected row number of the purely visual bottom-edge row, used only to sanity
#: check the sheet at startup. The live value is read from the worksheet, since
#: inserting managed rows pushes the footer down automatically.
EXPECTED_FOOTER_ROW = 113

#: Spare managed rows to keep beyond the number of live records. 0 = exact fit,
#: which is what "remove unnecessary EMPTY rows" asks for. Raise it to trade a
#: few EMPTY rows for fewer insert/delete API calls.
SLACK_ROWS = 0

#: The managed area is never shrunk below this many rows, even with an empty
#: roster. Two reasons, both load-bearing:
#:
#:   1. Zero managed rows puts the footer directly under the header, which is a
#:      structure the reader rejects - it would take the whole system down until
#:      someone repaired the sheet by hand.
#:   2. Row insertion uses inheritFromBefore, so new rows copy the formatting of
#:      the row above them. With no managed rows left, that row is the HEADER,
#:      and every new record row would inherit header formatting.
#:
#: One surviving row is enough for both; it simply shows as EMPTY.
MIN_MANAGED_ROWS = 1

#: Writing technical column headers would touch row 9, which is above the
#: managed area. Off by default; enable deliberately if the header row is wanted.
WRITE_TECH_HEADERS = False
TECH_HEADER_ROW = 9

# ── Human-facing columns ─────────────────────────────────────────────────────

COL_DESIGN_A = 1
COL_DESIGN_B = 2
COL_CATEGORY = 3
COL_IDENTIFICATION = 4
COL_TIMEZONE = 5
COL_RANK = 6
COL_BRANCH = 7
COL_NOTES = 8
COL_DESIGN_I = 9
COL_STATUS = 10
COL_ENTRY_DATE = 11
COL_VERIFICATION = 12
COL_DESIGN_M = 13

#: Columns the role manager is permitted to write. Anything absent from this set
#: is design/human territory and is never included in a write batch.
WRITABLE_COLUMNS = frozenset({
    COL_CATEGORY,
    COL_IDENTIFICATION,
    COL_TIMEZONE,
    COL_RANK,
    COL_BRANCH,
    COL_STATUS,
    COL_ENTRY_DATE,
    COL_VERIFICATION,
})

#: Column H holds human-written notes. The system never authors or edits note
#: text — but when a record is relocated to a different row, its note has to
#: travel with it or notes would silently reattach to the wrong person.
#: Set False to leave notes pinned to physical rows instead.
MOVE_NOTES_WITH_RECORD = True

# ── Technical (bot-managed) columns ──────────────────────────────────────────
# Placed past the designed area with a two-column gap (N, O) so they never
# interfere with the visual design. Hidden and width-minimised by
# sheets_gateway.ensure_technical_columns().

TECH_COL_START = 16  # P

COL_TECH_DISCORD_ID = 16     # P — canonical Discord identifier
COL_TECH_ROBLOX_ID = 17      # Q — canonical Roblox identifier
COL_TECH_RECORD_UID = 18     # R — stable per-record id, survives row moves
COL_TECH_SYNC_HASH = 19      # S — hash of the last successfully synced state
COL_TECH_SYNC_STATUS = 20    # T — OK / PENDING / FAILED / NEVER
COL_TECH_LAST_SYNC_AT = 21   # U — ISO-8601 UTC
COL_TECH_REVISION = 22       # V — monotonic counter, bumped on every system write
COL_TECH_LOA = 23            # W — opaque LOA payload; format TBD (see loa.py)
COL_TECH_SOURCE = 24         # X — SYSTEM / MANUAL, provenance of the last change

TECH_COL_END = 24  # X

TECH_HEADERS = {
    COL_TECH_DISCORD_ID: "DISCORD_ID",
    COL_TECH_ROBLOX_ID: "ROBLOX_ID",
    COL_TECH_RECORD_UID: "RECORD_UID",
    COL_TECH_SYNC_HASH: "SYNC_HASH",
    COL_TECH_SYNC_STATUS: "SYNC_STATUS",
    COL_TECH_LAST_SYNC_AT: "LAST_SYNC_AT",
    COL_TECH_REVISION: "REVISION",
    COL_TECH_LOA: "LOA",
    COL_TECH_SOURCE: "SOURCE",
}

#: Every column the system reads in a poll cycle: A..X. Cheaper as one range.
READ_COL_START = 1
READ_COL_END = TECH_COL_END


# ── A1 helpers ───────────────────────────────────────────────────────────────

def col_letter(index):
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA'."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def a1_range(first_row, last_row, first_col=READ_COL_START, last_col=READ_COL_END):
    """Build an A1 range string, e.g. 'A10:X112'."""
    return f"{col_letter(first_col)}{first_row}:{col_letter(last_col)}{last_row}"


def managed_row_count(footer_row):
    """How many managed rows exist between FIRST_MANAGED_ROW and the footer."""
    return max(0, footer_row - FIRST_MANAGED_ROW)


def last_managed_row(footer_row):
    """Last row that may hold a user record (the row above the visual footer)."""
    return footer_row - 1
