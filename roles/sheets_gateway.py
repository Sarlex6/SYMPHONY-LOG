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

    if footer < columns.FIRST_MANAGED_ROW:
        raise SheetStructureError(
            f"PERSONNEL has only {footer} row(s); the managed area starts at row "
            f"{columns.FIRST_MANAGED_ROW}. The sheet is missing its header rows - "
            f"restore it from a backup."
        )

    if footer == columns.FIRST_MANAGED_ROW:
        # Zero managed rows: degraded, but readable. Tolerated rather than
        # raised so the system can repair itself on the next write instead of
        # locking up until someone edits the sheet by hand.
        print(
            f"[Roles] PERSONNEL has no managed rows (footer at row {footer}). "
            f"Rows will be re-inserted on the next write."
        )
        return footer

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

    # Defence in depth: even if a caller asks for more, never take the managed
    # area below its floor.
    available = columns.managed_row_count(footer_row)
    count = min(count, max(0, available - columns.MIN_MANAGED_ROWS))
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


def _column_range(sheet_id, col, first_row, last_row):
    """A1-free grid range covering one column across a row span."""
    return {
        "sheetId": sheet_id,
        "startRowIndex": first_row - 1,
        "endRowIndex": last_row,
        "startColumnIndex": col - 1,
        "endColumnIndex": col,
    }


async def normalize_managed_merges(sections, footer_row):
    """Rebuild the merges the managed area depends on.

    Two things happen here, in one batch:

      * Column J is UNMERGED across the managed area. Status is a per-row value,
        and the Sheets API silently discards a write aimed at any cell of a merged
        range other than its top-left anchor - so a leftover design merge makes
        a row's STATUS text vanish while its neighbour's sticks.

      * Column C is unmerged and then re-merged once per category block, so the
        section label reads "HIGH-RANK" a single time down the whole block
        instead of being repeated on every row. The label is centred both ways,
        so it sits in the middle of the merged block rather than clinging to its
        top-left corner.

    `sections` is [(first_row, last_row), ...] in sheet coordinates. Blocks of a
    single row are left unmerged - a one-cell merge is meaningless.
    """
    worksheet = await get_worksheet()
    last_row = columns.last_managed_row(footer_row)
    if last_row < columns.FIRST_MANAGED_ROW:
        return

    requests = [
        {"unmergeCells": {"range": _column_range(
            worksheet.id, columns.COL_STATUS, columns.FIRST_MANAGED_ROW, last_row)}},
        {"unmergeCells": {"range": _column_range(
            worksheet.id, columns.COL_CATEGORY, columns.FIRST_MANAGED_ROW, last_row)}},
    ]

    merged = 0
    if sections:
        # Centre the whole column, not just the merged blocks, so a single-row
        # section - which is never merged - still matches the rest. Only the two
        # alignment fields are in `fields`, so the design's colours, borders and
        # font are left exactly as they are.
        requests.append({"repeatCell": {
            "range": _column_range(
                worksheet.id, columns.COL_CATEGORY,
                columns.FIRST_MANAGED_ROW, last_row),
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat.horizontalAlignment,"
                      "userEnteredFormat.verticalAlignment",
        }})

        for first, last in sections:
            if last > first:
                requests.append({"mergeCells": {
                    "range": _column_range(
                        worksheet.id, columns.COL_CATEGORY, first, last),
                    "mergeType": "MERGE_ALL",
                }})
                merged += 1

    try:
        await asyncio.to_thread(spreadsheet.batch_update, {"requests": requests})
    except gspread.exceptions.APIError as exc:
        # Never fatal: the values are already written and correct. A failed merge
        # is cosmetic, and retrying it on the next layout pass costs nothing.
        print(f"[Roles] Could not rebuild category merges: {exc}")
        return

    if merged:
        print(f"[Roles] Category column merged into {merged} block(s).")


#: Column J background colours, exactly as specified.
STATUS_COLORS = {
    "ACTIVE":      {"red": 0.72, "green": 0.88, "blue": 0.72},  # light green
    "SEMI-ACTIVE": {"red": 0.99, "green": 0.85, "blue": 0.66},  # light orange
    "IN-ACTIVE":   {"red": 1.00, "green": 0.20, "blue": 0.20},  # bright red
    "EMPTY":       {"red": 1.00, "green": 0.20, "blue": 0.20},  # bright red
}


async def ensure_status_formatting(footer_row):
    """Install conditional-format rules so column J colours follow its text.

    Idempotent: any rule this system previously installed on column J is removed
    first, so repeated calls do not stack duplicates and the range always matches
    the current managed area.

    Conditional formatting takes precedence over a cell's static fill, so this
    also corrects rows whose background was painted by hand in the original
    design and would otherwise never change colour.
    """
    worksheet = await get_worksheet()
    last_row = columns.last_managed_row(footer_row)
    if last_row < columns.FIRST_MANAGED_ROW:
        return

    try:
        meta = await asyncio.to_thread(spreadsheet.fetch_sheet_metadata)
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(f"Could not read sheet metadata: {exc}") from exc

    # Identify rules we own: a TEXT_EQ on one of our status words, over column J.
    stale = []
    for sheet in meta.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != worksheet.id:
            continue
        for index, rule in enumerate(sheet.get("conditionalFormats") or []):
            condition = rule.get("booleanRule", {}).get("condition", {})
            if condition.get("type") != "TEXT_EQ":
                continue
            values = [v.get("userEnteredValue") for v in condition.get("values", [])]
            if not values or values[0] not in STATUS_COLORS:
                continue
            if any(r.get("startColumnIndex") == columns.COL_STATUS - 1
                   for r in rule.get("ranges", [])):
                stale.append(index)

    requests = []
    # Descending, so each deletion does not shift the indices still to come.
    for index in sorted(stale, reverse=True):
        requests.append({"deleteConditionalFormatRule": {
            "sheetId": worksheet.id, "index": index,
        }})

    for position, (status_text, color) in enumerate(STATUS_COLORS.items()):
        requests.append({
            "addConditionalFormatRule": {
                "index": position,
                "rule": {
                    "ranges": [_column_range(
                        worksheet.id, columns.COL_STATUS,
                        columns.FIRST_MANAGED_ROW, last_row,
                    )],
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

    print(f"[Roles] Status colours applied to column J "
          f"(replaced {len(stale)} previous rule(s)).")


async def _read_validation_rule(worksheet, col, row):
    """The data validation rule currently on one cell, or None.

    Reads a single cell with a tight field mask, so this costs almost nothing
    even though it asks for grid data.
    """
    a1 = f"{worksheet.title}!{columns.col_letter(col)}{row}"
    params = {
        "includeGridData": "true",
        "ranges": [a1],
        "fields": "sheets.data.rowData.values.dataValidation",
    }

    try:
        meta = await asyncio.to_thread(spreadsheet.fetch_sheet_metadata, params)
    except Exception as exc:
        # Not fatal: without the current rule we simply fall back to writing one.
        print(f"[Roles] Could not read existing validation on "
              f"{columns.col_letter(col)}{row}: {exc}")
        return None

    try:
        sheets = meta.get("sheets") or []
        data = (sheets[0].get("data") or [])[0]
        row_data = (data.get("rowData") or [])[0]
        cell = (row_data.get("values") or [])[0]
        return cell.get("dataValidation")
    except (IndexError, KeyError, TypeError):
        return None


def _rule_values(rule):
    """The dropdown's value list, in order."""
    if not rule:
        return None
    condition = rule.get("condition") or {}
    if condition.get("type") != "ONE_OF_LIST":
        return None
    return [v.get("userEnteredValue") for v in (condition.get("values") or [])]


async def set_dropdown_values(col, values, footer_row):
    """Bring the column F (RANK) / G (BRANCH) dropdown in line with configuration.

    Deliberately conservative about *how* it does that.

    A wholesale setDataValidation replaces the entire rule, and a dropdown's
    appearance - the chip styling applied in the Sheets UI - lives on that rule.
    Rewriting it on every start is what silently reverted hand-styled dropdowns
    to plain white after a redeploy.

    So this:

      * reads the rule that is already there;
      * does nothing at all when the value list already matches, which is the
        normal case on every restart;
      * when the list genuinely changed, edits ONLY `condition.values` on the
        existing rule and writes that back, leaving every other property of the
        rule exactly as it was found.

    Refuses any column other than F and G.
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
    if last_row < columns.FIRST_MANAGED_ROW:
        return

    values = list(values)
    letter = columns.col_letter(col)

    # Check both ends of the managed range. Inserted rows inherit validation from
    # the row above, so if the first and last agree the column is consistent.
    first_rule = await _read_validation_rule(worksheet, col, columns.FIRST_MANAGED_ROW)
    last_rule = (
        first_rule if last_row == columns.FIRST_MANAGED_ROW
        else await _read_validation_rule(worksheet, col, last_row)
    )

    if _rule_values(first_rule) == values and _rule_values(last_rule) == values:
        print(f"[Roles] Column {letter} dropdown already matches configuration "
              f"({len(values)} value(s)); styling left untouched.")
        return

    # Start from whatever is on the sheet so styling and options survive.
    rule = dict(first_rule) if first_rule else {"showCustomUi": True, "strict": False}
    condition = dict(rule.get("condition") or {})
    condition["type"] = "ONE_OF_LIST"
    condition["values"] = [{"userEnteredValue": v} for v in values]
    rule["condition"] = condition
    # Never reject a manual edit: the sheet is the source of truth, so a value
    # typed by hand must be allowed to stand even if it is off-list.
    rule["strict"] = False

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
                "rule": rule,
            }
        }]
    }

    try:
        await asyncio.to_thread(spreadsheet.batch_update, body)
    except gspread.exceptions.APIError as exc:
        raise TransientSyncError(
            f"Failed to update column {letter} dropdown: {exc}"
        ) from exc

    previous = _rule_values(first_rule)
    print(f"[Roles] Column {letter} dropdown updated: "
          f"{len(previous) if previous else 0} -> {len(values)} value(s).")
