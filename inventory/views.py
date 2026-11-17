import asyncio
import discord
from discord.ui import View, Select, Button, Modal, TextInput
from inventory.sheets import build_approval_embeds
from datetime import datetime
import gspread
import math

from inventory import text_import
from inventory.sheets import (
    SHEET_NAMES, get_points_for_type, calculate_cart_points,
    get_categories, get_items_in_category, get_cached_quantity,
    get_supply_status, refresh_cache_async, write_cells_by_sheet, item_cache,
)
from inventory.state import (
    get_user_cart, set_user_cart, clear_user_cart, append_to_user_cart,
    pending_requests, update_pending_embeds, CART_TIMEOUT_HOURS,
)

#: How long a menu keeps working without being touched. It used to be 15
#: minutes, so anyone who opened /log, went to count stock and came back found
#: every button answering "This interaction failed". The cart behind it lives
#: for CART_TIMEOUT_HOURS, so the menu does too.
MENU_TIMEOUT = CART_TIMEOUT_HOURS * 3600

#: Strong references to fire-and-forget tasks, which asyncio otherwise only
#: holds weakly and can collect mid-run.
_background_tasks = set()


def _in_background(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ════════════════════════════════════════════════════════════════════════════
#  Page selection dropdown
# ════════════════════════════════════════════════════════════════════════════

class PageSelectView(View):
    def __init__(self, user):
        super().__init__(timeout=MENU_TIMEOUT)
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

        cart = get_user_cart(user.id)
        if cart:
            points = calculate_cart_points(cart)
            cart_btn = Button(
                label=f"📋 View Log ({len(cart)} entries • {points:.1f} pts)",
                style=discord.ButtonStyle.secondary,
                custom_id="view_cart_from_page"
            )
            cart_btn.callback = self.view_cart
            self.add_item(cart_btn)

        import_btn = Button(
            label="📝 Import Text Log (beta)",
            style=discord.ButtonStyle.secondary,
            custom_id="import_text_log"
        )
        import_btn.callback = self.import_text_log
        self.add_item(import_btn)

    async def import_text_log(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ImportLogModal(self.user))

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
#  Category selection dropdown
# ════════════════════════════════════════════════════════════════════════════

class CategorySelectView(View):
    def __init__(self, user, sheet_name, categories):
        super().__init__(timeout=MENU_TIMEOUT)
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
#  Item selection dropdown
# ════════════════════════════════════════════════════════════════════════════

ITEMS_PER_PAGE = 20  # leave room for nav options in the select

class ItemSelectView(View):
    def __init__(self, user, sheet_name, category, items, page=0):
        super().__init__(timeout=MENU_TIMEOUT)
        self.user = user
        self.sheet_name = sheet_name
        self.category = category
        self.items = items
        self.page = page

        total_pages = math.ceil(len(items) / ITEMS_PER_PAGE)
        start = page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        select = Select(
            placeholder=f"Select an item... (page {page + 1}/{total_pages})" if total_pages > 1 else "Select an item...",
            options=[
                discord.SelectOption(
                    label=item["name"][:100],
                    description=f"Type: {item['type']} | Qty: {item['quantity']}" if item["type"] else f"Qty: {item['quantity']}",
                    value=str(item["row"])
                )
                for item in page_items
            ],
            custom_id="item_select"
        )
        select.callback = self.item_selected
        self.add_item(select)

        # Prev / Next buttons (only shown when needed)
        if page > 0:
            prev_btn = Button(label="⬅ Prev", style=discord.ButtonStyle.secondary, custom_id="prev_page")
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)

        if end < len(items):
            next_btn = Button(label="Next ➡", style=discord.ButtonStyle.secondary, custom_id="next_page")
            next_btn.callback = self.next_page
            self.add_item(next_btn)

        back_btn = Button(label="⬅ Back to Categories", style=discord.ButtonStyle.secondary, custom_id="back_to_category")
        back_btn.callback = self.go_back
        self.add_item(back_btn)

    async def item_selected(self, interaction: discord.Interaction):
        selected_row = int(interaction.data["values"][0])
        item = next(i for i in self.items if i["row"] == selected_row)

        view = OperationSelectView(self.user, self.sheet_name, self.category, item)
        await interaction.response.edit_message(
            content=(
                f"**{self.sheet_name}** > **{self.category}** > **{item['name']}**\n"
                f"Current quantity: **{item['quantity']}**\n\n"
                f"Select operation:"
            ),
            view=view
        )

    async def prev_page(self, interaction: discord.Interaction):
        view = ItemSelectView(self.user, self.sheet_name, self.category, self.items, self.page - 1)
        await interaction.response.edit_message(
            content=self._header(),
            view=view
        )

    async def next_page(self, interaction: discord.Interaction):
        view = ItemSelectView(self.user, self.sheet_name, self.category, self.items, self.page + 1)
        await interaction.response.edit_message(
            content=self._header(),
            view=view
        )

    async def go_back(self, interaction: discord.Interaction):
        categories = get_categories(self.sheet_name)
        view = CategorySelectView(self.user, self.sheet_name, categories)
        await interaction.response.edit_message(
            content=f"**{self.sheet_name}** — Select a category:",
            view=view
        )

    def _header(self):
        total_pages = math.ceil(len(self.items) / ITEMS_PER_PAGE)
        start = self.page * ITEMS_PER_PAGE + 1
        end = min((self.page + 1) * ITEMS_PER_PAGE, len(self.items))
        return (
            f"**{self.sheet_name}** > **{self.category}** — "
            f"Items {start}–{end} of {len(self.items)} (page {self.page + 1}/{total_pages}):"
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


# ════════════════════════════════════════════════════════════════════════════
#  Add / Subtract selection + Amount modal
# ════════════════════════════════════════════════════════════════════════════

class OperationSelectView(View):
    def __init__(self, user, sheet_name, category, item):
        super().__init__(timeout=MENU_TIMEOUT)
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
        modal = AmountModal(self.user, self.sheet_name, self.category, self.item, "add", interaction)
        await interaction.response.send_modal(modal)

    async def subtract_clicked(self, interaction: discord.Interaction):
        modal = AmountModal(self.user, self.sheet_name, self.category, self.item, "subtract", interaction)
        await interaction.response.send_modal(modal)

    async def go_back(self, interaction: discord.Interaction):
        items = get_items_in_category(self.sheet_name, self.category)
        view = ItemSelectView(self.user, self.sheet_name, self.category, items)
        await interaction.response.edit_message(
            content=f"**{self.sheet_name}** > **{self.category}** — Select an item:",
            view=view
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


class AmountModal(Modal):
    def __init__(self, user, sheet_name, category, item, operation, original_interaction):
        super().__init__(title=f"{'Add to' if operation == 'add' else 'Subtract from'} {item['name'][:30]}")
        self.original_interaction = original_interaction
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

        append_to_user_cart(self.user.id, {
            "sheet": self.sheet_name,
            "category": self.category,
            "name": self.item["name"],
            "type": self.item.get("type", ""),
            "row": self.item["row"],
            "operation": self.operation,
            "amount": amount,
            "current_qty": self.item["quantity"],
        })

        # Edit the menu through this modal's own interaction. Going through the
        # button press that opened the modal used that press's token, which
        # Discord expires after 15 minutes.
        view = CartView(self.user)
        await interaction.response.edit_message(
            content=view.get_cart_display(),
            view=view
        )


# ════════════════════════════════════════════════════════════════════════════
#  Cart view — add more items or submit
# ════════════════════════════════════════════════════════════════════════════

CART_DISPLAY_PAGE_SIZE = 15

class CartView(View):
    def __init__(self, user, page=0):
        super().__init__(timeout=MENU_TIMEOUT)
        self.user = user

        cart = get_user_cart(user.id)
        total_pages = max(1, math.ceil(len(cart) / CART_DISPLAY_PAGE_SIZE))
        # Removing the last entry of the last page would otherwise leave the
        # view on a page that no longer exists.
        page = min(max(page, 0), total_pages - 1)
        self.page = page

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

        if page > 0:
            prev_btn = Button(label="⬅ Prev", style=discord.ButtonStyle.secondary, custom_id="cart_prev")
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)

        if (page + 1) * CART_DISPLAY_PAGE_SIZE < len(cart):
            next_btn = Button(label="Next ➡", style=discord.ButtonStyle.secondary, custom_id="cart_next")
            next_btn.callback = self.next_page
            self.add_item(next_btn)

        # Per-entry editing, so an imported log can be corrected entry by entry
        # instead of only cleared as a whole.
        if cart:
            start = page * CART_DISPLAY_PAGE_SIZE
            options = []
            for index, entry in enumerate(cart[start:start + CART_DISPLAY_PAGE_SIZE], start):
                op_symbol = "+" if entry["operation"] == "add" else "-"
                options.append(discord.SelectOption(
                    label=f"{index + 1}. {entry['name']}"[:100],
                    description=f"{op_symbol}{entry['amount']} • {entry['sheet']}"[:100],
                    value=str(index),
                ))
            edit_select = Select(
                placeholder="✏️ Edit or remove an entry…",
                options=options,
                custom_id="edit_entry"
            )
            edit_select.callback = self.edit_entry
            self.add_item(edit_select)

    async def edit_entry(self, interaction: discord.Interaction):
        index = int(interaction.data["values"][0])
        cart = get_user_cart(self.user.id)
        if index >= len(cart):
            view = CartView(self.user, self.page)
            await interaction.response.edit_message(content=view.get_cart_display(), view=view)
            return
        await interaction.response.send_modal(
            EditEntryModal(self.user, index, cart[index], self.page)
        )

    def get_cart_display(self):
        cart = get_user_cart(self.user.id)
        if not cart:
            return "No pending inventory adjustments."

        total_points = calculate_cart_points(cart)
        total_pages = math.ceil(len(cart) / CART_DISPLAY_PAGE_SIZE)
        start = self.page * CART_DISPLAY_PAGE_SIZE
        page_items = cart[start:start + CART_DISPLAY_PAGE_SIZE]

        header = f"**Pending Adjustment Batch** (page {self.page + 1}/{total_pages}):\n\n" if total_pages > 1 else "**Pending Adjustment Batch**:\n\n"
        footer = f"\n\n*{len(cart)} entries total* • **{total_points:.1f} points**"

        lines = []
        for i, entry in enumerate(page_items, start + 1):
            op_symbol = "+" if entry["operation"] == "add" else "-"
            new_qty = entry["current_qty"] + entry["amount"] if entry["operation"] == "add" \
                else entry["current_qty"] - entry["amount"]
            entry_points = entry["amount"] * get_points_for_type(entry.get("type", "")) if entry["operation"] == "add" else 0
            points_str = f" • {entry_points:.1f}pt" if entry_points > 0 else ""
            lines.append(
                f"**{i}.** {entry['name'][:25]} `{entry['current_qty']}→{new_qty}` ({op_symbol}{entry['amount']}){points_str}"
            )

        return header + "\n".join(lines) + footer

    async def prev_page(self, interaction: discord.Interaction):
        view = CartView(self.user, self.page - 1)
        await interaction.response.edit_message(content=view.get_cart_display(), view=view)

    async def next_page(self, interaction: discord.Interaction):
        view = CartView(self.user, self.page + 1)
        await interaction.response.edit_message(content=view.get_cart_display(), view=view)

    async def add_more(self, interaction: discord.Interaction):
        view = PageSelectView(self.user)
        await interaction.response.edit_message(
            content="Inventory Adjustment Interface — Select Inventory Register:",
            view=view
        )

    async def submit_cart(self, interaction: discord.Interaction):
        cart = get_user_cart(self.user.id)
        if not cart:
            await interaction.response.edit_message(
                content="⚠️ No pending inventory adjustments!", view=None
            )
            return

        modal = SubmitDetailsModal(interaction.user, cart.copy())
        await interaction.response.send_modal(modal)

    async def clear_cart(self, interaction: discord.Interaction):
        clear_user_cart(self.user.id)
        await interaction.response.edit_message(
            content="Entries cleared.",
            view=None
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id


class EditEntryModal(Modal):
    """Change one entry's amount; 0 removes it."""

    def __init__(self, user, index, entry, page):
        super().__init__(title=f"Edit: {entry['name']}"[:45])
        self.user = user
        self.index = index
        self.page = page
        # Which entry this was opened for, so an edit never lands on a
        # different one if the log changed while the modal was open.
        self.identity = (entry["sheet"], entry["row"], entry["operation"])
        self.operation = entry["operation"]
        self.current_qty = entry["current_qty"]

        self.amount_input = TextInput(
            label="Amount (0 removes the entry)",
            default=str(entry["amount"]),
            required=True,
            max_length=10
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Please enter a whole number (0 removes the entry).", ephemeral=True
            )
            return
        if amount < 0:
            await interaction.response.send_message(
                "⚠️ The amount cannot be negative.", ephemeral=True
            )
            return

        cart = get_user_cart(self.user.id)
        entry = cart[self.index] if self.index < len(cart) else None
        if entry is None or (entry["sheet"], entry["row"], entry["operation"]) != self.identity:
            view = CartView(self.user, self.page)
            await interaction.response.edit_message(
                content="⚠️ Your log changed while that was open, so nothing was edited.\n\n"
                        + view.get_cart_display(),
                view=view
            )
            return

        if amount == 0:
            cart.pop(self.index)
        elif self.operation == "subtract" and amount > self.current_qty:
            await interaction.response.send_message(
                f"⚠️ Cannot subtract **{amount}** from **{entry['name']}** "
                f"(current quantity: **{self.current_qty}**).",
                ephemeral=True
            )
            return
        else:
            entry["amount"] = amount
        set_user_cart(self.user.id, cart)

        view = CartView(self.user, self.page)
        await interaction.response.edit_message(content=view.get_cart_display(), view=view)


# ════════════════════════════════════════════════════════════════════════════
#  Text log import (experimental)
# ════════════════════════════════════════════════════════════════════════════

#: One import at a time. Matching is CPU work on a small server; a second
#: member waits their turn (and is told so) rather than both slowing down.
_import_slot = asyncio.Semaphore(1)

#: Never wait longer than this for an import, in seconds. The matcher stops
#: itself well before (text_import.DEFAULT_TIME_BUDGET); this is the backstop.
IMPORT_TIMEOUT = 60


def _all_items():
    """Every cached item, labelled with the register it lives in."""
    return [
        dict(item, sheet=sheet_name)
        for sheet_name, items in item_cache.items()
        for item in items
    ]


def _quote(text):
    """Member-typed text, safe inside `code`."""
    return str(text).replace("`", "'")[:60]


def _import_report(result):
    """What happened to each part of the text, for the member to check."""
    sections = [f"**📝 Text log import** — added **{len(result.matched)}** item(s) to your log."]

    def item_label(item, others):
        # The register only matters when the name alone does not say which.
        shared = sum(1 for other in others if other["name"] == item["name"]) > 1
        return f"**{item['name']}**" + (f" ({item['sheet']})" if shared else "")

    guesses = [(item, typed) for item, _qty, typed, score in result.matched if score < 1.0]
    if guesses:
        sections.append("\n🔍 **Double-check these guesses:**")
        sections += [f"• `{_quote(typed)}` → **{item['name']}**" for item, typed in guesses]

    if result.ambiguous:
        sections.append("\n❓ **Could be more than one item — not added, add these manually:**")
        for typed, qty, options in result.ambiguous:
            names = " / ".join(item_label(item, options) for item in options)
            sections.append(f"• `{_quote(typed)} {qty}` → {names}")

    if result.unrecognized:
        sections.append("\n⚠️ **Not recognised — ignored:**")
        sections += [f"• `{_quote(typed)} {qty}`" for typed, qty in result.unrecognized]

    if result.problems:
        sections.append("\n⚠️ **Could not read:**")
        sections += [f"• {problem}" for problem in result.problems]

    if result.unprocessed:
        sections.append(
            f"\n⏱️ **{result.unprocessed}** entr{'y was' if result.unprocessed == 1 else 'ies were'} "
            f"not processed in time — add them manually or import them separately."
        )

    sections.append("\nReview your log and use **✏️ Edit or remove an entry** to fix anything.")

    text = "\n".join(sections)
    if len(text) > 1900:
        text = text[:1880].rsplit("\n", 1)[0] + "\n… (list shortened)"
    return text


class ImportLogModal(Modal):
    def __init__(self, user):
        super().__init__(title="Import Text Log (experimental)")
        self.user = user

        self.log_input = TextInput(
            label="One entry per line: ITEM NAME QUANTITY",
            style=discord.TextStyle.paragraph,
            placeholder="Iron Ore 25\nCopper Wire 10\nMed-Kit 2",
            required=True,
            max_length=4000
        )
        self.add_item(self.log_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Answer straight away; the matching below can take a few seconds on
        # a small server, far past Discord's 3-second deadline.
        busy = _import_slot.locked()
        await interaction.response.edit_message(
            content="⏳ " + (
                "Another log is being read right now — yours is next, hang on…"
                if busy else
                "Reading your log and matching it against the inventory…"
            ),
            view=None
        )

        try:
            async with _import_slot:
                if not any(item_cache.values()):
                    await refresh_cache_async()
                # In a worker thread, so the three bots sharing this event
                # loop keep running while it works.
                result = await asyncio.wait_for(
                    asyncio.to_thread(text_import.import_log, self.log_input.value, _all_items()),
                    timeout=IMPORT_TIMEOUT,
                )
        except Exception as e:
            print(f"[Inventory] Text log import failed: {type(e).__name__}: {e}")
            await interaction.edit_original_response(
                content=f"⚠️ Could not read that log ({type(e).__name__}). Nothing was added.",
                view=PageSelectView(self.user)
            )
            return

        for item, quantity, _typed, _score in result.matched:
            append_to_user_cart(self.user.id, {
                "sheet": item["sheet"],
                "category": item["category"],
                "name": item["name"],
                "type": item.get("type", ""),
                "row": item["row"],
                "operation": "add",
                "amount": quantity,
                "current_qty": item["quantity"],
            })

        view = CartView(self.user)
        await interaction.edit_original_response(content=view.get_cart_display(), view=view)
        await interaction.followup.send(_import_report(result), ephemeral=True)


# ════════════════════════════════════════════════════════════════════════════
#  Submit details modal — optional note and video link
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
            label="Video link (optional)",
            placeholder="Paste a YouTube, Streamable, Medal, etc. link...",
            required=False,
            max_length=500
        )
        self.add_item(self.video_input)

    async def on_submit(self, interaction: discord.Interaction):
        # bot reference is passed through the module-level variable
        from inventory.bot import bot

        note = self.note_input.value.strip() if self.note_input.value else ""
        video_link = self.video_input.value.strip() if self.video_input.value else ""

        requester_name = interaction.user.display_name
        requester_avatar = interaction.user.display_avatar.url

        request_id = f"{interaction.user.id}_{int(datetime.utcnow().timestamp())}"

        approval_channel = bot.get_channel(int(config["APPROVAL_CHANNEL_ID"]))
        if not approval_channel:
            await interaction.response.send_message(
                "⚠️ Approval channel not found. Contact an admin.",
                ephemeral=True
            )
            return

        # Acknowledge first. Posting a large request takes several messages, and
        # the channel's rate limit can stretch that past Discord's 3-second
        # deadline - the member then saw an error and resubmitted, even though
        # the request had already gone through.
        await interaction.response.defer()

        approval_view = ApprovalView(
            requester=interaction.user,
            cart=self.cart,
            request_id=request_id
        )

        embeds = build_approval_embeds(self.cart, requester_name, requester_avatar, note)

        try:
            if len(embeds) == 1:
                approval_msg = await approval_channel.send(embed=embeds[0], view=approval_view)
            else:
                # Send overflow embeds first (no view), then the final one with the buttons
                for overflow_embed in embeds[:-1]:
                    await approval_channel.send(embed=overflow_embed)
                approval_msg = await approval_channel.send(embed=embeds[-1], view=approval_view)
        except discord.HTTPException as e:
            # The cart is still intact, so the member can simply submit again.
            print(f"[Inventory] Could not post log request: {type(e).__name__}: {e}")
            await interaction.edit_original_response(
                content="⚠️ Could not post your request to the approval channel. "
                        "Your entries are kept — please try submitting again.",
                view=CartView(self.user),
            )
            return

        approval_view.message_id = approval_msg.id

        pending_requests[request_id] = {
            "view": approval_view,
            "message": approval_msg,
            "cart": self.cart,
            "requester": interaction.user,
            "requester_name": requester_name,
            "requester_avatar": requester_avatar,
            "note": note,
            "created_at": datetime.utcnow(),
        }

        if video_link:
            await approval_channel.send(
                f"🎥 **Evidence video for the request above:**\n{video_link}"
            )

        clear_user_cart(self.user.id)

        await interaction.edit_original_response(
            content=(
                "✅ **Request submitted!**\n"
                "Your log request has been sent to the approval channel.\n"
                "You'll be notified when it's approved or rejected."
            ),
            view=None
        )


# Need config for APPROVAL_CHANNEL_ID
from config import config


# ════════════════════════════════════════════════════════════════════════════
#  Approval view — HR approves or rejects
# ════════════════════════════════════════════════════════════════════════════

class ApprovalView(View):
    def __init__(self, requester, cart, request_id):
        super().__init__(timeout=None)
        self.requester = requester
        self.cart = cart
        self.request_id = request_id
        self.message_id = None
        #: Set the moment an approver acts, before anything is awaited. Without
        #: it, two approvers clicking together - or one double-clicking while
        #: the sheet write was in progress - applied the same request twice.
        self.handled = False

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
        if self.handled:
            await interaction.response.send_message(
                "⚠️ This request is already being handled.", ephemeral=True
            )
            return
        self.handled = True

        approver = interaction.user

        await interaction.response.defer()

        highest_role = approver.top_role.name if approver.top_role else "Unknown"
        written = False

        try:
            today = datetime.utcnow().strftime("%d/%m/%Y")

            # Fresh quantities first. The cache can be minutes old, and an
            # adjustment applied to a stale quantity silently overwrites
            # whatever changed on the sheet since.
            await refresh_cache_async()

            # Running totals per item, so a cart that touches the same item
            # twice applies both adjustments instead of the second one
            # overwriting the first.
            quantities = {}
            cells_by_sheet = {}
            for entry in self.cart:
                key = (entry["sheet"], entry["row"])
                current_qty = quantities.get(key, get_cached_quantity(*key))

                if entry["operation"] == "add":
                    new_qty = current_qty + entry["amount"]
                else:
                    new_qty = current_qty - entry["amount"]

                new_qty = max(0, new_qty)
                quantities[key] = new_qty

            for (sheet_name, row), new_qty in quantities.items():
                cells_by_sheet.setdefault(sheet_name, []).extend([
                    gspread.Cell(row=row, col=6, value=get_supply_status(new_qty)),
                    gspread.Cell(row=row, col=8, value=new_qty),
                    gspread.Cell(row=row, col=10, value=today),
                    gspread.Cell(row=row, col=11, value=highest_role),
                ])

            # Off the event loop: the three bots share it, and a Sheets write
            # run on it stalls every one of them until it returns.
            await asyncio.to_thread(write_cells_by_sheet, cells_by_sheet)
            written = True

            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.add_field(
                name="✅ Approved",
                value=f"By {approver.mention} ({highest_role})\n{datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}",
                inline=False
            )

            await interaction.edit_original_response(embed=embed, view=None)

            pending_requests.pop(self.request_id, None)

            await refresh_cache_async()

            # One message edit per pending request; not something this click
            # should wait on.
            _in_background(update_pending_embeds())

            try:
                await self.requester.send(
                    f"✅ Your entry request has been **approved**!"
                )
            except discord.Forbidden:
                pass

        except Exception as e:
            print(f"[Inventory] Approval of {self.request_id} failed: {type(e).__name__}: {e}")
            if not written:
                # Nothing reached the sheet, so it is safe to try again.
                self.handled = False
            await interaction.followup.send(
                f"⚠️ Error updating spreadsheet: {type(e).__name__}: {e}"
                + ("" if written else "\nNothing was written; you can try again."),
                ephemeral=True
            )

    async def reject(self, interaction: discord.Interaction):
        if self.handled:
            await interaction.response.send_message(
                "⚠️ This request is already being handled.", ephemeral=True
            )
            return
        modal = RejectReasonModal(self, interaction.message, interaction.user)
        await interaction.response.send_modal(modal)


class RejectReasonModal(Modal):
    def __init__(self, approval_view, message, rejector):
        super().__init__(title="Rejection Reason")
        self.approval_view = approval_view
        self.requester = approval_view.requester
        self.original_message = message
        self.rejector = rejector
        self.request_id = approval_view.request_id

        self.reason_input = TextInput(
            label="Reason for rejection",
            placeholder="Enter reason...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Approved while this modal was open.
        if self.approval_view.handled:
            await interaction.response.send_message(
                "⚠️ This request was already handled.", ephemeral=True
            )
            return
        self.approval_view.handled = True

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

        pending_requests.pop(self.request_id, None)

        try:
            await self.requester.send(
                f"❌ Your log request has been **rejected**.\n"
                f"**Reason:** {reason}"
            )
        except discord.Forbidden:
            pass