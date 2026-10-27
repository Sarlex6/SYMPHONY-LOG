import asyncio
import gspread
import json
import os
from config import config

# ── Google Sheets setup ─────────────────────────────────────────────────────
def get_gspread_client():
    """Connect to Google Sheets via service account — supports env var or local file."""
    if os.environ.get("GOOGLE_CREDENTIALS"):
        creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        return gspread.service_account_from_dict(creds_dict)
    else:
        credentials_path = os.path.join(os.path.dirname(__file__), "credentials.json")
        return gspread.service_account(filename=credentials_path)

gc = get_gspread_client()

#: (connect, read) timeouts in seconds for every Sheets call, from this bot and
#: from the role manager, which shares this client. gspread sets none by
#: default, so a connection that stalls would hang its caller forever — and
#: the caller holding the role manager's sheet lock blocks every role command
#: behind it.
SHEETS_TIMEOUT = (10, 45)
gc.set_timeout(SHEETS_TIMEOUT)

spreadsheet = gc.open_by_key(config["SPREADSHEET_ID"])

SHEET_NAMES = ["MATERIALS&SUPPLIES", "EQUIPMENT", "FIREARMS", "MELEES"]

# ── Points system ────────────────────────────────────────────────────────────
POINTS_BY_TYPE = {
    "MATERIALS-C": 0.1,
    "MATERIALS-B": 0.5,
    "MATERIALS-A": 2,
    "SUPPLIES-C": 0.2,
    "SUPPLIES-A": 4,
    "FOOD-C": 0.1,
    "MISC-BIO-C": 1,
    "MISC-BIO-A": 2,
    "MISC-KEY-A": 2,
    "WPN-CQC-C": 1,
    "WPN-CQC-B": 2,
    "WPN-CQC-A": 3,
    "WPN-CQC-EXOTIC": 5,
    "WPN-FRM-C": 2,
    "WPN-FRM-B": 3,
    "WPN-FRM-A": 4,
    "WPN-FRM-EXOTIC": 6,
    "MED-C-B": 1,
    "MED-C-ADVANCED": 2,
    "MED-A-ADVANCED": 4,
    "COM-DEV-B": 1,
    "EQUIP-GEN-C": 1,
    "EQUIP-EXP-C": 2,
}

def get_points_for_type(item_type):
    """Get points per unit for an item type. Returns 0 if type not found."""
    return POINTS_BY_TYPE.get(item_type.strip().upper(), 0)

def calculate_cart_points(cart):
    """Calculate total points for a cart. Only additions count."""
    total = 0
    for entry in cart:
        if entry["operation"] == "add":
            total += entry["amount"] * get_points_for_type(entry.get("type", ""))
    return total

# ── Cache for spreadsheet data ───────────────────────────────────────────────
# Filled in place, never rebound: other modules hold a reference to this dict
# (`from inventory.sheets import item_cache`), and rebinding the name left
# them looking at the old, empty one.
item_cache = {}

#: Columns read per row: C (category), D (name), E (type), H (quantity).
_MIN_ROW_WIDTH = 8


def _fetch_items():
    """Read all four registers and return {sheet_name: [items]}.

    Blocking. One batched API call for all four sheets, where reading them one
    by one cost eight (a metadata lookup plus a read, per sheet).
    """
    response = spreadsheet.values_batch_get([f"'{name}'" for name in SHEET_NAMES])
    value_ranges = response.get("valueRanges", [])

    fresh = {}
    for sheet_name, value_range in zip(SHEET_NAMES, value_ranges):
        all_values = value_range.get("values", [])

        items = []
        current_category = ""

        for row_idx in range(9, len(all_values)):
            # The batch read drops trailing empty cells, so pad each row out to
            # the columns used below.
            row = [str(cell) for cell in all_values[row_idx]]
            row.extend([""] * (_MIN_ROW_WIDTH - len(row)))

            if row[2].strip():
                current_category = row[2].strip()

            item_name = row[3].strip()
            if not item_name:
                continue

            item_type = row[4].strip()

            try:
                quantity = int(row[7]) if row[7].strip() else 0
            except ValueError:
                quantity = 0

            items.append({
                "row": row_idx + 1,
                "category": current_category,
                "name": item_name,
                "type": item_type,
                "quantity": quantity,
            })

        fresh[sheet_name] = items

    return fresh


def _apply_cache(fresh):
    item_cache.clear()
    item_cache.update(fresh)
    print(f"Cache refreshed: {sum(len(v) for v in item_cache.values())} items loaded")


def refresh_cache():
    """Pull all item data from the four sheets and cache it. BLOCKING — from
    async code use refresh_cache_async(), or the bot stalls while it runs."""
    _apply_cache(_fetch_items())


async def refresh_cache_async():
    """refresh_cache() without blocking the event loop.

    The inventory, assistant and roles bots share one event loop. A Sheets read
    run directly on it freezes all three for as long as it takes, and any
    button press or command arriving in that window misses Discord's 3-second
    deadline and shows as "This interaction failed".
    """
    fresh = await asyncio.to_thread(_fetch_items)
    # Swapped in on the loop thread, so nothing ever sees a half-filled cache.
    _apply_cache(fresh)


def get_cached_quantity(sheet_name, row):
    """Get the current cached quantity for a specific item by sheet and row."""
    if sheet_name not in item_cache:
        return 0
    for item in item_cache[sheet_name]:
        if item["row"] == row:
            return item["quantity"]
    return 0


def get_categories(sheet_name):
    if sheet_name not in item_cache:
        return []
    seen = []
    for item in item_cache[sheet_name]:
        if item["category"] not in seen:
            seen.append(item["category"])
    return seen


def get_items_in_category(sheet_name, category):
    if sheet_name not in item_cache:
        return []
    return [item for item in item_cache[sheet_name] if item["category"] == category]


def get_supply_status(quantity):
    if quantity <= 0:
        return "❌ NONE"
    elif quantity <= 199:
        return "⚠️ LOW"
    else:
        return "✅ HIGH"


def write_cells_by_sheet(cells_by_sheet):
    """Write {sheet_name: [gspread.Cell]} to the registers. Blocking.

    Uses one cached metadata read for all the worksheet handles rather than one
    per sheet.
    """
    worksheets = {ws.title: ws for ws in spreadsheet.worksheets()}
    for sheet_name, cells in cells_by_sheet.items():
        if cells:
            worksheets[sheet_name].update_cells(cells)


def build_approval_embeds(cart, requester_name, requester_avatar_url, note=""):
    """Build one or more approval embeds, paginated if the cart is large."""
    from datetime import datetime
    import discord

    total_points = calculate_cart_points(cart)
    total_pages = 1
    pages = []

    # Build all entry lines first, then chunk them
    entry_lines = []
    for i, entry in enumerate(cart, 1):
        current_qty = get_cached_quantity(entry["sheet"], entry["row"])
        op_symbol = "+" if entry["operation"] == "add" else "-"
        new_qty = current_qty + entry["amount"] if entry["operation"] == "add" else current_qty - entry["amount"]
        entry_points = entry["amount"] * get_points_for_type(entry.get("type", "")) if entry["operation"] == "add" else 0
        points_str = f" • **{entry_points:.1f} pts**" if entry_points > 0 else ""

        entry_lines.append((
            f"**{i}.** {entry['name']}\n"
            f"   📁 {entry['sheet']} > {entry['category']}\n"
            f"   📊 {current_qty} → **{new_qty}** ({op_symbol}{entry['amount']}){points_str}"
        ))

    # Chunk lines into pages staying under 3800 chars (safe margin under 4096)
    DESCRIPTION_LIMIT = 3800
    chunks = []
    current_chunk = []
    current_len = 0

    for line in entry_lines:
        line_len = len(line) + 2  # +2 for the \n\n separator
        if current_chunk and current_len + line_len > DESCRIPTION_LIMIT:
            chunks.append(current_chunk)
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append(current_chunk)

    total_pages = len(chunks)

    for page_idx, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=f"Log Request" + (f" (page {page_idx + 1}/{total_pages})" if total_pages > 1 else ""),
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=requester_name, icon_url=requester_avatar_url)
        embed.description = "\n\n".join(chunk)

        # Only add note + footer to last page so they don't repeat
        if page_idx == total_pages - 1:
            if note:
                embed.add_field(name="📝 Note", value=note, inline=False)
            embed.set_footer(
                text=f"Requested by {requester_name} • {len(cart)} item(s) • {total_points:.1f} points"
            )
        else:
            embed.set_footer(text=f"Continued on next embed...")

        pages.append(embed)

    return pages


# Keep old single-embed function as a shim if anything still calls it
def build_approval_embed(cart, requester_name, requester_avatar_url, note=""):
    return build_approval_embeds(cart, requester_name, requester_avatar_url, note)[0]
