"""Raw I/O against the PERSONNEL worksheet.

Reuses the already-authenticated gspread client from `inventory.sheets` — same
spreadsheet, same service account, one API client.

Two things this layer adds over calling gspread directly:

  * gspread is blocking. Every call here runs in a worker thread via
    ``asyncio.to_thread`` so a poll cycle or a slow write never stalls the bot's
    event loop.
  * A single write lock serializes all mutations, so read-modify-write cycles
    from commands and from the background poller cannot interleave.

This module knows about cells and ranges. It does not know what a UserRecord is.
"""

import asyncio

import gspread

from roles import columns
from roles.errors import SheetStructureError, TransientSyncError

# Reuse the existing authenticated client and open spreadsheet.
from inventory.sheets import spreadsheet

# ── Concurrency ──────────────────────────────────────────────────────────────

#: Held for the whole read-modify-write cycle of any mutation. Everything that
#: changes the sheet must acquire it — that is what makes "the sheet is
#: authoritative" safe under concurrent commands and polling.
write_lock = asyncio.Lock()

_worksheet = None


# ── Worksheet resolution ─────────────────────────────────────────────────────

def _resolve_worksheet_sync():
    """Find PERSONNEL by title, falling back to its documented position."""
    try:
        return spreadsheet.worksheet(columns.SHEET_NAME)
    except gspread.WorksheetNotFound:
        pass

    worksheets = spreadsheet.worksheets()
    if len(worksheets) > columns.SHEET_INDEX:
        found = worksheets[columns.SHEET_INDEX]
        print(
            f"[Roles] Worksheet '{columns.SHEET_NAME}' not found by name; "
            f"using position {columns.SHEET_INDEX} ('{found.title}')."
        )
        return found

    raise SheetStructureError(
        f"Worksheet '{columns.SHEET_NAME}' not found, and the spreadsheet has "
        f"only {len(worksheets)} sheet(s)."
    )


async def get_worksheet(force_refresh=False):
    """Cached PERSONNEL worksheet handle."""
    global _worksheet
    if _worksheet is None or force_refresh:
        _worksheet = await asyncio.to_thread(_resolve_worksheet_sync)
    return _worksheet


async def get_footer_row():
    """Row number of the purely visual bottom-edge row.

    Read live rather than hardcoded: inserting managed rows pushes the footer
    down, so the sheet's own row count tracks it automatically.
    """
    worksheet = await get_worksheet()
    footer = await asyncio.to_thread(lambda: worksheet.row_count)

    if footer < columns.FIRST_MANAGED_ROW + 1:
        raise SheetStructureError(
            f"PERSONNEL has {footer} rows; the managed area starts at row "
            f"{columns.FIRST_MANAGED_ROW} and needs a footer row below it."
        )

    if footer != columns.EXPECTED_FOOTER_ROW:
        # Expected after the first resize. Informational, never fatal.
        print(
            f"[Roles] Footer row is {footer} "
            f"(initial layout had {columns.EXPECTED_FOOTER_ROW})."
        )
    return footer


# ── Reads ────────────────────────────────────────────────────────────────────

async def read_managed_block():
    """Read A10:X<footer-1> as a padded 2D list.

    Returns (rows, footer_row) where rows[i] corresponds to sheet row
    FIRST_MANAGED_ROW + i and is always READ_COL_END wide.
    """
    worksheet = await get_worksheet()
    footer_row = await get_footer_row()
    last_row = columns.last_managed_row(footer_row)

    if last_row < columns.FIRST_MANAGED_ROW:
        return [], footer_row

    a1 = columns.a1_range(columns.FIRST_MANAGED_ROW, last_row)

    try:
        raw = await asyncio.to_thread(
            worksheet.get, a1, value_render_option="UNFORMATTED_VALUE"
        )
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Failed to read PERSONNEL block: {exc}") from exc

    width = columns.READ_COL_END
    expected = last_row - columns.FIRST_MANAGED_ROW + 1

    rows = []
    for index in range(expected):
        row = list(raw[index]) if index < len(raw) else []
        row = [("" if cell is None else str(cell)) for cell in row]
        row.extend([""] * (width - len(row)))
        rows.append(row[:width])

    return rows, footer_row


# ── Writes ───────────────────────────────────────────────────────────────────

async def write_cells(cells, value_input_option="USER_ENTERED"):
    """Write a list of gspread.Cell. Design columns are refused, not silently dropped.

    The caller must already hold `write_lock`.
    """
    if not cells:
        return

    allowed = columns.WRITABLE_COLUMNS
    for cell in cells:
        writable = cell.col in allowed or columns.TECH_COL_START <= cell.col <= columns.TECH_COL_END
        if columns.MOVE_NOTES_WITH_RECORD and cell.col == columns.COL_NOTES:
            writable = True
        if not writable:
            raise SheetStructureError(
                f"Refusing to write column {columns.col_letter(cell.col)} "
                f"(row {cell.row}): not a managed column."
            )
        if cell.row < columns.FIRST_MANAGED_ROW:
            raise SheetStructureError(
                f"Refusing to write row {cell.row}: above the managed area "
                f"(row {columns.FIRST_MANAGED_ROW})."
            )

    worksheet = await get_worksheet()
    try:
        await asyncio.to_thread(
            worksheet.update_cells, cells, value_input_option=value_input_option
        )
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Failed to write PERSONNEL cells: {exc}") from exc


async def insert_managed_rows(count, footer_row):
    """Insert `count` blank managed rows immediately above the footer row.

    Uses insertDimension with inheritFromBefore so the new rows inherit the
    formatting and data validation of the managed row above them — that is what
    keeps the column F / G dropdowns and the column J colours intact.
    """
    if count <= 0:
        return footer_row

    worksheet = await get_worksheet()
    sheet_id = worksheet.id
    insert_at = footer_row - 1  # 0-based index of the footer row

    body = {
        "requests": [{
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": insert_at,
                    "endIndex": insert_at + count,
                },
                "inheritFromBefore": True,
            }
        }]
    }

    try:
        await asyncio.to_thread(spreadsheet.batch_update, body)
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Failed to insert {count} row(s): {exc}") from exc

    await get_worksheet(force_refresh=True)
    print(f"[Roles] Inserted {count} managed row(s) above footer row {footer_row}.")
    return footer_row + count


async def delete_managed_rows(count, footer_row):
    """Delete `count` managed rows immediately above the footer row.

    Only ever removes rows from the bottom of the managed area, which is where
    the layout pass parks surplus/EMPTY rows before calling this.
    """
    if count <= 0:
        return footer_row

    available = columns.managed_row_count(footer_row)
    count = min(count, max(0, available))
    if count <= 0:
        return footer_row

    worksheet = await get_worksheet()
    start_index = footer_row - 1 - count  # 0-based, exclusive of the footer

    if start_index < columns.FIRST_MANAGED_ROW - 1:
        raise SheetStructureError(
            "Refusing to delete rows: the range would reach above the managed area."
        )

    body = {
        "requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": worksheet.id,
                    "dimension": "ROWS",
                    "startIndex": start_index,
                    "endIndex": start_index + count,
                }
            }
        }]
    }

    try:
        await asyncio.to_thread(spreadsheet.batch_update, body)
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Failed to delete {count} row(s): {exc}") from exc

    await get_worksheet(force_refresh=True)
    print(f"[Roles] Deleted {count} surplus managed row(s).")
    return footer_row - count


async def clear_managed_range(first_row, last_row):
    """Blank the managed + technical columns across a row range.

    Leaves design columns A, B, I, M, N, O untouched — this clears values, not
    formatting, so the row stays visually intact and reusable.
    """
    if last_row < first_row:
        return

    worksheet = await get_worksheet()
    ranges = []
    for col in sorted(columns.WRITABLE_COLUMNS):
        letter = columns.col_letter(col)
        ranges.append(f"{letter}{first_row}:{letter}{last_row}")
    ranges.append(
        f"{columns.col_letter(columns.TECH_COL_START)}{first_row}:"
        f"{columns.col_letter(columns.TECH_COL_END)}{last_row}"
    )

    try:
        await asyncio.to_thread(worksheet.batch_clear, ranges)
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Failed to clear rows {first_row}-{last_row}: {exc}") from exc


# ── One-off structural maintenance ──────────────────────────────────────────

async def ensure_technical_columns():
    """Hide and narrow the technical columns so they stay out of the design.

    Safe to call repeatedly. Writes headers only if columns.WRITE_TECH_HEADERS
    is enabled, since the header row sits above the managed area.
    """
    worksheet = await get_worksheet()

    requests = [{
        "updateDimensionProperties": {
            "range": {
                "sheetId": worksheet.id,
                "dimension": "COLUMNS",
                "startIndex": columns.TECH_COL_START - 1,
                "endIndex": columns.TECH_COL_END,
            },
            "properties": {"pixelSize": 40, "hiddenByUser": True},
            "fields": "pixelSize,hiddenByUser",
        }
    }]

    try:
        await asyncio.to_thread(spreadsheet.batch_update, {"requests": requests})
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Failed to configure technical columns: {exc}") from exc

    if columns.WRITE_TECH_HEADERS:
        header_cells = [
            gspread.Cell(row=columns.TECH_HEADER_ROW, col=col, value=title)
            for col, title in columns.TECH_HEADERS.items()
        ]
        await asyncio.to_thread(worksheet.update_cells, header_cells)

    print("[Roles] Technical columns configured (hidden, minimal width).")


#: Column J background colours, exactly as specified.
STATUS_COLORS = {
    "ACTIVE":      {"red": 0.72, "green": 0.88, "blue": 0.72},  # light green
    "SEMI-ACTIVE": {"red": 0.99, "green": 0.85, "blue": 0.66},  # light orange
    "IN-ACTIVE":   {"red": 1.00, "green": 0.20, "blue": 0.20},  # bright red
    "EMPTY":       {"red": 1.00, "green": 0.20, "blue": 0.20},  # bright red
}


async def ensure_status_formatting(footer_row):
    """Install conditional-format rules for column J.

    Off by default (sync.manage_status_formatting) because adding rules to a
    sheet that already has hand-made ones is the kind of change that should be
    deliberate. Appends rules; it does not clear existing ones.
    """
    worksheet = await get_worksheet()
    last_row = columns.last_managed_row(footer_row)

    requests = []
    for index, (status_text, color) in enumerate(STATUS_COLORS.items()):
        requests.append({
            "addConditionalFormatRule": {
                "index": index,
                "rule": {
                    "ranges": [{
                        "sheetId": worksheet.id,
                        "startRowIndex": columns.FIRST_MANAGED_ROW - 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": columns.COL_STATUS - 1,
                        "endColumnIndex": columns.COL_STATUS,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": status_text}],
                        },
                        "format": {"backgroundColor": color},
                    },
                },
            }
        })

    try:
        await asyncio.to_thread(spreadsheet.batch_update, {"requests": requests})
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Failed to install status formatting: {exc}") from exc

    print("[Roles] Status conditional formatting installed for column J.")


async def set_dropdown_values(col, values, footer_row):
    """Rebuild the data validation dropdown for column F (RANK) or G (BRANCH).

    Called after rank/branch configuration changes so the sheet's dropdowns match
    the configuration exactly. Refuses any other column.
    """
    if col not in (columns.COL_RANK, columns.COL_BRANCH):
        raise SheetStructureError(
            f"Dropdowns are only managed for columns F and G, not "
            f"{columns.col_letter(col)}."
        )
    if not values:
        return

    worksheet = await get_worksheet()
    last_row = columns.last_managed_row(footer_row)

    body = {
        "requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": columns.FIRST_MANAGED_ROW - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": col - 1,
                    "endColumnIndex": col,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in values],
                    },
                    "showCustomUi": True,
                    "strict": False,  # never reject a manual edit; the sheet is authoritative
                },
            }
        }]
    }

    try:
        await asyncio.to_thread(spreadsheet.batch_update, body)
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(
            f"Failed to update column {columns.col_letter(col)} dropdown: {exc}"
        ) from exc

    print(f"[Roles] Column {columns.col_letter(col)} dropdown updated ({len(values)} value(s)).")
