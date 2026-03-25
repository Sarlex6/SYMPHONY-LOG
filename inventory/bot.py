import discord
from discord import app_commands

from config import config
from inventory.sheets import refresh_cache, item_cache, SHEET_NAMES
from inventory.views import PageSelectView
from inventory.state import cleanup_loop, clear_user_cart

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

    if not _initialized:
        await tree.sync()
        print("[Inventory] Slash commands synced!")
        refresh_cache()
        bot.loop.create_task(cleanup_loop(bot))
        _initialized = True
    else:
        print("[Inventory] Reconnected (skipping re-initialization).")
        refresh_cache()  # Still refresh cache on reconnect since data may have changed

    print("[Inventory] Bot is ready!")


@bot.event
async def on_disconnect():
    print("[Inventory] Disconnected from Discord gateway.")


@bot.event
async def on_resumed():
    print("[Inventory] Resumed Discord gateway session.")