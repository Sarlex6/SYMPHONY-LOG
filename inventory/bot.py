import discord
from discord import app_commands

from config import config
from inventory.sheets import refresh_cache_async, item_cache, SHEET_NAMES
from inventory.views import PageSelectView
from inventory.state import cleanup_loop, clear_user_cart, pending_requests

# ── Discord Bot setup ───────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents, max_messages=100)
tree = app_commands.CommandTree(bot)

LOG_CHANNEL_ID = config["LOG_CHANNEL_ID"]
APPROVAL_CHANNEL_ID = config["APPROVAL_CHANNEL_ID"]


# ════════════════════════════════════════════════════════════════════════════
#  /log command
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
#  /refresh command
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="refresh", description="Refresh the item cache from Google Sheets")
async def refresh_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await refresh_cache_async()
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
#  /reset command
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="reset", description="Clear your current log cart and start fresh")
async def reset_command(interaction: discord.Interaction):
    clear_user_cart(interaction.user.id)
    await interaction.response.send_message(
        "Your log cart has been cleared. Use `/log` to start fresh.",
        ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════════════
#  Bot startup
# ════════════════════════════════════════════════════════════════════════════

_initialized = False

@bot.event
async def on_ready():
    global _initialized
    print(f"[Inventory] Logged in as {bot.user} (ID: {bot.user.id})")

    try:
        if not _initialized:
            await tree.sync()
            print("[Inventory] Slash commands synced!")
            await refresh_cache_async()
            bot.loop.create_task(cleanup_loop(bot))
            _initialized = True
        else:
            print("[Inventory] Reconnected (skipping re-initialization).")
            await refresh_cache_async()  # data may have changed while disconnected
    except Exception as e:
        # A failed refresh leaves the previous cache in place; /refresh retries.
        print(f"[Inventory] Startup refresh failed: {type(e).__name__}: {e}")

    print("[Inventory] Bot is ready!")


@tree.error
async def on_command_error(interaction: discord.Interaction, error):
    """Answer instead of leaving the member on "The application did not respond"."""
    original = getattr(error, "original", error)
    print(f"[Inventory] Command failed: {type(original).__name__}: {original}")
    message = f"⚠️ Something went wrong: {type(original).__name__}. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Answer approve/reject clicks on requests this process no longer knows.

    Pending requests live in memory, so after a restart the buttons on older
    approval messages have nothing behind them and Discord shows "This
    interaction failed". Say what happened instead. Requests that ARE known
    are left alone here; their ApprovalView handles the click.
    """
    if interaction.type is not discord.InteractionType.component:
        return
    custom_id = str((interaction.data or {}).get("custom_id", ""))
    for prefix in ("approve_", "reject_"):
        if custom_id.startswith(prefix) and custom_id[len(prefix):] not in pending_requests:
            try:
                await interaction.response.send_message(
                    "⚠️ This request was submitted before the bot last restarted, so "
                    "it can no longer be approved or rejected here. Ask the requester "
                    "to submit it again with `/log`.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return


@bot.event
async def on_disconnect():
    print("[Inventory] Disconnected from Discord gateway.")


@bot.event
async def on_resumed():
    print("[Inventory] Resumed Discord gateway session.")