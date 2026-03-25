import asyncio
import sys
from config import config

RECONNECT_DELAY = 15  # seconds between reconnection attempts


async def run_inventory_bot():
    """Run the inventory bot with auto-reconnect."""
    while True:
        try:
            # Re-import to get fresh module state if needed
            from inventory.bot import bot, tree
            from inventory.sheets import refresh_cache

            print("[Main] Inventory bot connecting...")
            await bot.start(config["DISCORD_TOKEN"])

        except Exception as e:
            print(f"[Main] Inventory bot error: {type(e).__name__}: {e}")

        print(f"[Main] Inventory bot disconnected. Reconnecting in {RECONNECT_DELAY}s...")
        await asyncio.sleep(RECONNECT_DELAY)


async def run_assistant_bot():
    """Run the assistant bot with auto-reconnect."""
    while True:
        try:
            from assistant.bot import bot

            print("[Main] Assistant bot connecting...")
            await bot.start(config["ASSISTANT_TOKEN"])

        except Exception as e:
            print(f"[Main] Assistant bot error: {type(e).__name__}: {e}")

        print(f"[Main] Assistant bot disconnected. Reconnecting in {RECONNECT_DELAY}s...")
        await asyncio.sleep(RECONNECT_DELAY)


async def main():
    # ── Start HTTP server ─────────────────────────────────────────────────
    try:
        from assistant.web import start_web_server, set_bot
        asyncio.create_task(start_web_server())

        # Set bot reference for web server once assistant is available
        if config.get("ASSISTANT_TOKEN"):
            try:
                from assistant.bot import bot as assistant_bot
                set_bot(assistant_bot)
            except ImportError:
                pass
    except ImportError:
        print("[Main] Web server module not available, skipping.")

    # ── Start bots with auto-reconnect ────────────────────────────────────
    tasks = [asyncio.create_task(run_inventory_bot())]

    if config.get("ASSISTANT_TOKEN"):
        tasks.append(asyncio.create_task(run_assistant_bot()))
        print("[Main] Both bots starting with auto-reconnect...")
    else:
        print("[Main] Inventory bot only (no ASSISTANT_TOKEN).")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())