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
spreadsheet = gc.open_by_key(config["SPREADSHEET_ID"])

SHEET_NAMES = ["MATERIALS&SUPPLIES", "EQUIPMENT", "FIREARMS", "MELEES"]

# ── Points system ────────────────────────────────────────────────────────────
POINTS_PER_UNIT = 2

def calculate_cart_points(cart):
    """Calculate total points for a cart. Only additions count."""
    total = 0
    for entry in cart:
        if entry["operation"] == "add":
            total += entry["amount"] * POINTS_PER_UNIT
    return total

# ── Cache for spreadsheet data ───────────────────────────────────────────────
item_cache = {}

def refresh_cache():
    """Pull all item data from the four sheets and cache it."""
    global item_cache
    item_cache = {}

    for sheet_name in SHEET_NAMES:
        worksheet = spreadsheet.worksheet(sheet_name)
        all_values = worksheet.get_all_values()

        items = []
        current_category = ""

        for row_idx in range(9, len(all_values)):
            row = all_values[row_idx]

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

        item_cache[sheet_name] = items

    print(f"Cache refreshed: {sum(len(v) for v in item_cache.values())} items loaded")


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


def build_approval_embed(cart, requester_name, requester_avatar_url, note=""):
    """Build an approval embed using current cached quantities."""
    import discord
    from datetime import datetime

    total_points = calculate_cart_points(cart)

    embed = discord.Embed(
        title="Log Request",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow()
    )
    embed.set_author(
        name=requester_name,
        icon_url=requester_avatar_url
    )

    description_lines = []
    for i, entry in enumerate(cart, 1):
        current_qty = get_cached_quantity(entry["sheet"], entry["row"])

        op_symbol = "+" if entry["operation"] == "add" else "-"
        if entry["operation"] == "add":
            new_qty = current_qty + entry["amount"]
        else:
            new_qty = current_qty - entry["amount"]

        entry_points = entry["amount"] * POINTS_PER_UNIT if entry["operation"] == "add" else 0
        points_str = f" • **{entry_points} pts**" if entry_points > 0 else ""

        description_lines.append(
            f"**{i}.** {entry['name']}\n"
            f"   📁 {entry['sheet']} > {entry['category']}\n"
            f"   📊 {current_qty} → **{new_qty}** ({op_symbol}{entry['amount']}){points_str}"
        )

    embed.description = "\n\n".join(description_lines)

    if note:
        embed.add_field(name="📝 Note", value=note, inline=False)

    embed.set_footer(
        text=f"Requested by {requester_name} • {len(cart)} item(s) • {total_points} points"
    )

    return embed