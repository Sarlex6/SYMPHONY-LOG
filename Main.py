import discord
from discord import app_commands
from discord.ui import View, Select, Button, Modal, TextInput
import gspread
import json
from datetime import datetime
import os

# ── Load config — supports both local config.json and Railway env vars ────
def load_config():
    """Load from environment variables (Railway) or fall back to config.json (local)."""
    if os.environ.get("DISCORD_TOKEN"):
        # Running on Railway — read from environment variables
        return {
            "DISCORD_TOKEN": os.environ["DISCORD_TOKEN"],
            "SPREADSHEET_ID": os.environ["SPREADSHEET_ID"],
            "LOG_CHANNEL_ID": int(os.environ["LOG_CHANNEL_ID"]),
            "APPROVAL_CHANNEL_ID": int(os.environ["APPROVAL_CHANNEL_ID"]),
        }
    else:
        # Running locally — read from config.json
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            return json.load(f)

config = load_config()

DISCORD_TOKEN = config["DISCORD_TOKEN"]
SPREADSHEET_ID = config["SPREADSHEET_ID"]
LOG_CHANNEL_ID = config["LOG_CHANNEL_ID"]
APPROVAL_CHANNEL_ID = config["APPROVAL_CHANNEL_ID"]

# ── Google Sheets setup ─────────────────────────────────────────────────────
def get_gspread_client():
    """Connect to Google Sheets via service account — supports env var or local file."""
    if os.environ.get("GOOGLE_CREDENTIALS"):
        # Railway — credentials stored as env var
        creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        return gspread.service_account_from_dict(creds_dict)
    else:
        # Local — credentials.json file
        credentials_path = os.path.join(os.path.dirname(__file__), "credentials.json")
        return gspread.service_account(filename=credentials_path)

gc = get_gspread_client()
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

# The four sheets we care about (skip LOTUS LOGISTICS)
SHEET_NAMES = ["MATERIALS&SUPPLIES", "EQUIPMENT", "FIREARMS", "MELEES"]

# ── Points system ────────────────────────────────────────────────────────────
POINTS_PER_UNIT = 2  # Only counted for additions, not subtractions

def calculate_cart_points(cart):
    """Calculate total points for a cart. Only additions count."""
    total = 0
    for entry in cart:
        if entry["operation"] == "add":
            total += entry["amount"] * POINTS_PER_UNIT
    return total

# ── Cache for spreadsheet data ───────────────────────────────────────────────
item_cache = {}  # { sheet_name: [ {row, category, name, type, quantity, ...}, ... ] }

def refresh_cache():
    """Pull all item data from the four sheets and cache it."""
    global item_cache
    item_cache = {}

    for sheet_name in SHEET_NAMES:
        worksheet = spreadsheet.worksheet(sheet_name)
        all_values = worksheet.get_all_values()

        items = []
        current_category = ""

        # Rows 1-9 are headers (index 0-8), data starts at row 10 (index 9)
        for row_idx in range(9, len(all_values)):
            row = all_values[row_idx]

            # Column C (index 2) = category - might be empty if merged
            if row[2].strip():
                current_category = row[2].strip()

            # Column D (index 3) = item name
            item_name = row[3].strip()
            if not item_name:
                continue  # Skip empty rows

            # Column E (index 4) = item type
            item_type = row[4].strip()

            # Column H+I merged (index 7) = quantity
            try:
                quantity = int(row[7]) if row[7].strip() else 0
            except ValueError:
                quantity = 0

            items.append({
                "row": row_idx + 1,  # 1-indexed for Google Sheets API
                "category": current_category,
                "name": item_name,
                "type": item_type,
                "quantity": quantity,
            })

        item_cache[sheet_name] = items

    print(f"Cache refreshed: {sum(len(v) for v in item_cache.values())} items loaded")


def get_categories(sheet_name):
    """Get unique categories for a sheet."""
    if sheet_name not in item_cache:
        return []
    seen = []
    for item in item_cache[sheet_name]:
        if item["category"] not in seen:
            seen.append(item["category"])
    return seen


def get_items_in_category(sheet_name, category):
    """Get items within a specific category."""
    if sheet_name not in item_cache:
        return []
    return [item for item in item_cache[sheet_name] if item["category"] == category]


# ── Per-user cart storage ────────────────────────────────────────────────────
user_carts = {}


# ── Supply status helper ────────────────────────────────────────────────────
def get_supply_status(quantity):
    if quantity <= 0:
        return "❌ NONE"
    elif quantity <= 199:
        return "⚠️ LOW"
    else:
        return "✅ HIGH"


# ── Discord Bot setup ───────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ════════════════════════════════════════════════════════════════════════════
#  STEP 1: /log command — starts the flow with a page (sheet) selector
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="log", description="Inventory Adjustment Interface")
async def log_command(interaction: discord.Interaction):
    if interaction.channel_id != LOG_CHANNEL_ID:
        await interaction.response.send_message(
            f"Please use this command in <#{LOG_CHANNEL_ID}>.",
            ephemeral=True
        )
        return

    view = PageSelectView(interaction.user)
    await interaction.response.send_message(
        "Inventory Adjustment Interface — Select Inventory Register:",
        view=view,
        ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════════════
#  STEP 2: Page selection dropdown
# ════════════════════════════════════════════════════════════════════════════

class PageSelectView(View):
    def __init__(self, user):
        super().__init__(timeout=120)
        self.user = user

        select = Select(
            placeholder="Select Inventory Register...",
            options=[
                discord.SelectOption(label=name, value=name)
                for name in SHEET_NAMES
            ],
            custom_id="page_select"
        )
        select.callback = self.page_selected
        self.add_item(select)

        if user.id in user_carts and user_carts[user.id]:
            cart = user_carts[user.id]
            points = calculate_cart_points(cart)
            cart_btn = Button(
                label=f"📋 View Entries ({len(cart)} items • {points} pts)",
                style=discord.ButtonStyle.secondary,
                custom_id="view_cart_from_page"
            )
            cart_btn.callback = self.view_cart
            self.add_item(cart_btn)

    async def page_selected(self, interaction: discord.Interaction):
        selected_page = interaction.data["values"][0]
        categories = get_categories(selected_page)

        if not categories:
            await interaction.response.edit_message(
                content=f"⚠️ No categories found in **{selected_page}**.",
                view=None
            )
            return

        view = CategorySelectView(self.user, selected_page, categories)
        await interaction.response.edit_message(
            content=f"**{selected_page}** — Select a category:",
            view=view
        )

    async def view_cart(self, interaction: discord.Interaction):
        view = CartView(self.user)
        await interaction.response.edit_message(
            content=view.get_cart_display(),
            view=view
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


# ════════════════════════════════════════════════════════════════════════════
#  STEP 3: Category selection dropdown
# ════════════════════════════════════════════════════════════════════════════

class CategorySelectView(View):
    def __init__(self, user, sheet_name, categories):
        super().__init__(timeout=120)
        self.user = user
        self.sheet_name = sheet_name

        select = Select(
            placeholder="Select a category...",
            options=[
                discord.SelectOption(label=cat[:100], value=cat[:100])
                for cat in categories[:25]
            ],
            custom_id="category_select"
        )
        select.callback = self.category_selected
        self.add_item(select)

        back_btn = Button(label="⬅ Back", style=discord.ButtonStyle.secondary, custom_id="back_to_page")
        back_btn.callback = self.go_back
        self.add_item(back_btn)

    async def category_selected(self, interaction: discord.Interaction):
        selected_category = interaction.data["values"][0]
        items = get_items_in_category(self.sheet_name, selected_category)

        if not items:
            await interaction.response.edit_message(
                content=f"⚠️ No items found in **{selected_category}**.",
                view=None
            )
            return

        view = ItemSelectView(self.user, self.sheet_name, selected_category, items)
        await interaction.response.edit_message(
            content=f"**{self.sheet_name}** > **{selected_category}** — Select an entry:",
            view=view
        )

    async def go_back(self, interaction: discord.Interaction):
        view = PageSelectView(self.user)
        await interaction.response.edit_message(
            content="Inventory Adjustment Interface — Select Inventory Register:",
            view=view
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


# ════════════════════════════════════════════════════════════════════════════
#  STEP 4: Item selection dropdown
# ════════════════════════════════════════════════════════════════════════════

class ItemSelectView(View):
    def __init__(self, user, sheet_name, category, items):
        super().__init__(timeout=120)
        self.user = user
        self.sheet_name = sheet_name
        self.category = category
        self.items = items

        select = Select(
            placeholder="Select an item...",
            options=[
                discord.SelectOption(
                    label=item["name"][:100],
                    description=f"Type: {item['type']} | Qty: {item['quantity']}" if item["type"] else f"Qty: {item['quantity']}",
                    value=str(item["row"])
                )
                for item in items[:25]
            ],
            custom_id="item_select"
        )
        select.callback = self.item_selected
        self.add_item(select)

        back_btn = Button(label="⬅ Back", style=discord.ButtonStyle.secondary, custom_id="back_to_category")
        back_btn.callback = self.go_back
        self.add_item(back_btn)

    async def item_selected(self, interaction: discord.Interaction):
        selected_row = int(interaction.data["values"][0])
        item = next(i for i in self.items if i["row"] == selected_row)

        view = OperationSelectView(self.user, self.sheet_name, self.category, item)
        await interaction.response.edit_message(
            content=(
                f"📦 **{self.sheet_name}** > **{self.category}** > **{item['name']}**\n"
                f"Current quantity: **{item['quantity']}**\n\n"
                f"Select operation:"
            ),
            view=view
        )

    async def go_back(self, interaction: discord.Interaction):
        categories = get_categories(self.sheet_name)
        view = CategorySelectView(self.user, self.sheet_name, categories)
        await interaction.response.edit_message(
            content=f"📦 **{self.sheet_name}** — Select a category:",
            view=view
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


# ════════════════════════════════════════════════════════════════════════════
#  STEP 5: Add / Subtract selection + Amount modal
# ════════════════════════════════════════════════════════════════════════════

class OperationSelectView(View):
    def __init__(self, user, sheet_name, category, item):
        super().__init__(timeout=120)
        self.user = user
        self.sheet_name = sheet_name
        self.category = category
        self.item = item

        add_btn = Button(label="➕ Add", style=discord.ButtonStyle.success, custom_id="op_add")
        add_btn.callback = self.add_clicked
        self.add_item(add_btn)

        sub_btn = Button(label="➖ Subtract", style=discord.ButtonStyle.danger, custom_id="op_subtract")
        sub_btn.callback = self.subtract_clicked
        self.add_item(sub_btn)

        back_btn = Button(label="⬅ Back", style=discord.ButtonStyle.secondary, custom_id="back_to_items")
        back_btn.callback = self.go_back
        self.add_item(back_btn)

    async def add_clicked(self, interaction: discord.Interaction):
        modal = AmountModal(self.user, self.sheet_name, self.category, self.item, "add")
        await interaction.response.send_modal(modal)

    async def subtract_clicked(self, interaction: discord.Interaction):
        modal = AmountModal(self.user, self.sheet_name, self.category, self.item, "subtract")
        await interaction.response.send_modal(modal)

    async def go_back(self, interaction: discord.Interaction):
        items = get_items_in_category(self.sheet_name, self.category)
        view = ItemSelectView(self.user, self.sheet_name, self.category, items)
        await interaction.response.edit_message(
            content=f"📦 **{self.sheet_name}** > **{self.category}** — Select an item:",
            view=view
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


class AmountModal(Modal):
    def __init__(self, user, sheet_name, category, item, operation):
        super().__init__(title=f"{'Add to' if operation == 'add' else 'Subtract from'} {item['name'][:30]}")
        self.user = user
        self.sheet_name = sheet_name
        self.category = category
        self.item = item
        self.operation = operation

        self.amount_input = TextInput(
            label=f"Amount to {'add' if operation == 'add' else 'subtract'}",
            placeholder="Enter a number...",
            required=True,
            max_length=10
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
            if amount <= 0:
                await interaction.response.send_message(
                    "⚠️ Please enter a positive number.", ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Please enter a valid number.", ephemeral=True
            )
            return

        if self.operation == "subtract" and amount > self.item["quantity"]:
            await interaction.response.send_message(
                f"⚠️ Cannot subtract **{amount}** from **{self.item['name']}** "
                f"(current quantity: **{self.item['quantity']}**).",
                ephemeral=True
            )
            return

        if self.user.id not in user_carts:
            user_carts[self.user.id] = []

        user_carts[self.user.id].append({
            "sheet": self.sheet_name,
            "category": self.category,
            "name": self.item["name"],
            "row": self.item["row"],
            "operation": self.operation,
            "amount": amount,
            "current_qty": self.item["quantity"],
        })

        view = CartView(self.user)
        await interaction.response.edit_message(
            content=view.get_cart_display(),
            view=view
        )


# ════════════════════════════════════════════════════════════════════════════
#  STEP 6: Cart view — add more items or submit
# ════════════════════════════════════════════════════════════════════════════

class CartView(View):
    def __init__(self, user):
        super().__init__(timeout=300)
        self.user = user

        add_more_btn = Button(
            label="➕ Add Another Entry",
            style=discord.ButtonStyle.primary,
            custom_id="add_more"
        )
        add_more_btn.callback = self.add_more
        self.add_item(add_more_btn)

        submit_btn = Button(
            label="✅ Submit for Approval",
            style=discord.ButtonStyle.success,
            custom_id="submit_cart"
        )
        submit_btn.callback = self.submit_cart
        self.add_item(submit_btn)

        clear_btn = Button(
            label="🗑️ Clear Entry",
            style=discord.ButtonStyle.danger,
            custom_id="clear_cart"
        )
        clear_btn.callback = self.clear_cart
        self.add_item(clear_btn)

    def get_cart_display(self):
        cart = user_carts.get(self.user.id, [])
        if not cart:
            return "No pending inventory adjustments."

        lines = ["**Pending Adjustment Batch**:\n"]
        for i, entry in enumerate(cart, 1):
            op_symbol = "+" if entry["operation"] == "add" else "-"
            new_qty = entry["current_qty"] + entry["amount"] if entry["operation"] == "add" \
                else entry["current_qty"] - entry["amount"]

            # Show points per entry (only for additions)
            entry_points = entry["amount"] * POINTS_PER_UNIT if entry["operation"] == "add" else 0
            points_str = f" • **{entry_points} pts**" if entry_points > 0 else ""

            lines.append(
                f"**{i}.** {entry['name']} ({entry['sheet']})\n"
                f"   {entry['current_qty']} → **{new_qty}** ({op_symbol}{entry['amount']}){points_str}"
            )

        total_points = calculate_cart_points(cart)
        lines.append(f"\n*{len(cart)} pending adjustment entries* • **Total: {total_points} points**")
        return "\n".join(lines)

    async def add_more(self, interaction: discord.Interaction):
        view = PageSelectView(self.user)
        await interaction.response.edit_message(
            content="Inventory Adjustment Interface — Select Inventory Register:",
            view=view
        )

    async def submit_cart(self, interaction: discord.Interaction):
        cart = user_carts.get(self.user.id, [])
        if not cart:
            await interaction.response.edit_message(
                content="⚠️ No pending inventory adjustments!", view=None
            )
            return

        modal = SubmitDetailsModal(interaction.user, cart.copy())
        await interaction.response.send_modal(modal)

    async def clear_cart(self, interaction: discord.Interaction):
        user_carts[self.user.id] = []
        await interaction.response.edit_message(
            content="Entries cleared.",
            view=None
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


# ════════════════════════════════════════════════════════════════════════════
#  STEP 6b: Submit details modal — optional note and video link
# ════════════════════════════════════════════════════════════════════════════

class SubmitDetailsModal(Modal):
    def __init__(self, user, cart):
        super().__init__(title="Submit Log Request")
        self.user = user
        self.cart = cart

        self.note_input = TextInput(
            label="Note (optional)",
            placeholder="Add any context or notes for the approver...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500
        )
        self.add_item(self.note_input)

        self.video_input = TextInput(
            label="Video link (required)",
            placeholder="Paste a YouTube, Streamable, Medal, etc. link...",
            required=True,
            max_length=500
        )
        self.add_item(self.video_input)

    async def on_submit(self, interaction: discord.Interaction):
        note = self.note_input.value.strip() if self.note_input.value else ""
        video_link = self.video_input.value.strip() if self.video_input.value else ""

        total_points = calculate_cart_points(self.cart)

        # Build the approval embed
        embed = discord.Embed(
            title="Log Request",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url
        )

        description_lines = []
        for i, entry in enumerate(self.cart, 1):
            op_symbol = "+" if entry["operation"] == "add" else "-"
            new_qty = entry["current_qty"] + entry["amount"] if entry["operation"] == "add" \
                else entry["current_qty"] - entry["amount"]

            entry_points = entry["amount"] * POINTS_PER_UNIT if entry["operation"] == "add" else 0
            points_str = f" • **{entry_points} pts**" if entry_points > 0 else ""

            description_lines.append(
                f"**{i}.** {entry['name']}\n"
                f"   📁 {entry['sheet']} > {entry['category']}\n"
                f"   📊 {entry['current_qty']} → **{new_qty}** ({op_symbol}{entry['amount']}){points_str}"
            )

        embed.description = "\n\n".join(description_lines)

        # Add note field if provided
        if note:
            embed.add_field(name="📝 Note", value=note, inline=False)

        # Add points to footer
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name} • {len(self.cart)} item(s) • {total_points} points"
        )

        # Unique request ID
        request_id = f"{interaction.user.id}_{int(datetime.utcnow().timestamp())}"

        # Send to approval channel
        approval_channel = bot.get_channel(APPROVAL_CHANNEL_ID)
        if not approval_channel:
            await interaction.response.send_message(
                "⚠️ Approval channel not found. Contact an admin.",
                ephemeral=True
            )
            return

        approval_view = ApprovalView(
            requester=interaction.user,
            cart=self.cart,
            request_id=request_id
        )

        approval_msg = await approval_channel.send(embed=embed, view=approval_view)
        approval_view.message_id = approval_msg.id

        # Send video link as a follow-up so Discord renders the preview
        if video_link:
            await approval_channel.send(
                f"🎥 **Evidence video for the request above:**\n{video_link}"
            )

        # Clear the user's cart
        user_carts[self.user.id] = []

        await interaction.response.edit_message(
            content=(
                "✅ **Request submitted!**\n"
                "Your log request has been sent to the approval channel.\n"
                "You'll be notified when it's approved or rejected."
            ),
            view=None
        )


# ════════════════════════════════════════════════════════════════════════════
#  STEP 7: Approval view — HR approves or rejects
# ════════════════════════════════════════════════════════════════════════════

class ApprovalView(View):
    def __init__(self, requester, cart, request_id):
        super().__init__(timeout=None)
        self.requester = requester
        self.cart = cart
        self.request_id = request_id
        self.message_id = None

        approve_btn = Button(
            label="✅ Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"approve_{request_id}"
        )
        approve_btn.callback = self.approve
        self.add_item(approve_btn)

        reject_btn = Button(
            label="❌ Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"reject_{request_id}"
        )
        reject_btn.callback = self.reject
        self.add_item(reject_btn)

    async def approve(self, interaction: discord.Interaction):
        approver = interaction.user

        # Defer immediately — sheet updates take too long for Discord's 3s timeout
        await interaction.response.defer()

        highest_role = approver.top_role.name if approver.top_role else "Unknown"

        try:
            today = datetime.utcnow().strftime("%d/%m/%Y")

            # Group cart entries by sheet so we can batch update per sheet
            entries_by_sheet = {}
            for entry in self.cart:
                if entry["sheet"] not in entries_by_sheet:
                    entries_by_sheet[entry["sheet"]] = []
                entries_by_sheet[entry["sheet"]].append(entry)

            for sheet_name, entries in entries_by_sheet.items():
                worksheet = spreadsheet.worksheet(sheet_name)

                # Build a single batch of all cell updates for this sheet
                batch_cells = []
                for entry in entries:
                    if entry["operation"] == "add":
                        new_qty = entry["current_qty"] + entry["amount"]
                    else:
                        new_qty = entry["current_qty"] - entry["amount"]

                    new_qty = max(0, new_qty)
                    status = get_supply_status(new_qty)
                    row = entry["row"]

                    # F = supply status, H = quantity, J = date, K = approver
                    batch_cells.append(gspread.Cell(row=row, col=6, value=status))
                    batch_cells.append(gspread.Cell(row=row, col=8, value=new_qty))
                    batch_cells.append(gspread.Cell(row=row, col=10, value=today))
                    batch_cells.append(gspread.Cell(row=row, col=11, value=highest_role))

                # One API call per sheet instead of 4 per item
                worksheet.update_cells(batch_cells)

            # Update the embed to show it's approved
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.add_field(
                name="✅ Approved",
                value=f"By {approver.mention} ({highest_role})\n{datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}",
                inline=False
            )

            await interaction.edit_original_response(embed=embed, view=None)

            # Refresh cache after update
            refresh_cache()

            # Notify the requester
            try:
                await self.requester.send(
                    f"✅ Your entry request has been **approved**!"
                )
            except discord.Forbidden:
                pass

        except Exception as e:
            await interaction.followup.send(
                f"⚠️ Error updating spreadsheet: {str(e)}", ephemeral=True
            )

    async def reject(self, interaction: discord.Interaction):
        modal = RejectReasonModal(self.requester, interaction.message, interaction.user)
        await interaction.response.send_modal(modal)


class RejectReasonModal(Modal):
    def __init__(self, requester, message, rejector):
        super().__init__(title="Rejection Reason")
        self.requester = requester
        self.original_message = message
        self.rejector = rejector

        self.reason_input = TextInput(
            label="Reason for rejection",
            placeholder="Enter reason...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason_input.value or "No reason provided"
        highest_role = self.rejector.top_role.name if self.rejector.top_role else "Unknown"

        embed = self.original_message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(
            name="❌ Rejected",
            value=(
                f"By {self.rejector.mention} ({highest_role})\n"
                f"**Reason:** {reason}\n"
                f"{datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}"
            ),
            inline=False
        )

        await interaction.response.edit_message(embed=embed, view=None)

        try:
            await self.requester.send(
                f"❌ Your log request has been **rejected**.\n"
                f"**Reason:** {reason}"
            )
        except discord.Forbidden:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  STEP 8: /refresh command — manually refresh cache
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="refresh", description="Refresh the item cache from Google Sheets")
async def refresh_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        refresh_cache()
        total = sum(len(v) for v in item_cache.values())
        await interaction.followup.send(
            f"✅ Cache refreshed! Loaded **{total}** items across **{len(SHEET_NAMES)}** sheets.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Error refreshing cache: {str(e)}", ephemeral=True
        )


# ════════════════════════════════════════════════════════════════════════════
#  Bot startup
# ════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await tree.sync()
    print("Slash commands synced!")
    refresh_cache()
    print("Bot is ready!")


# ── Run the bot ─────────────────────────────────────────────────────────────
bot.run(DISCORD_TOKEN)