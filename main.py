import asyncio
import sys
import traceback
from config import config

RECONNECT_DELAY = 15  # seconds between reconnection attempts


async def _run_forever(name, get_bot, token):
    """Run one bot, restarting it whenever it stops.

    discord.py already reconnects through ordinary network trouble on its own;
    this loop is for what it gives up on. A client that has stopped is closed,
    and a closed client's start() returns immediately without connecting — so
    without clear() a bot that dropped out once stayed offline until the whole
    process was restarted, while this loop printed "reconnecting" forever.
    """
    while True:
        bot = None
        try:
            bot = get_bot()
            if bot.is_closed():
                _reopen(bot)
            print(f"[Main] {name} bot connecting...")
            await bot.start(token)

        except Exception as e:
            print(f"[Main] {name} bot error: {type(e).__name__}: {e}")
            traceback.print_exc()

        finally:
            # start() can also return or raise with the client still open (a
            # failed login, say); close it so the next attempt starts clean.
            if bot is not None and not bot.is_closed():
                try:
                    await bot.close()
                except Exception:
                    pass

        print(f"[Main] {name} bot disconnected. Reconnecting in {RECONNECT_DELAY}s...")
        await asyncio.sleep(RECONNECT_DELAY)


def _reopen(bot):
    """Make a closed client startable again.

    clear() resets the client's own state, but closing it also closed its HTTP
    connector, and discord.py only creates a new one when none is set. Left in
    place, every request of the next login fails with "Connector is closed".
    """
    import discord

    connector = getattr(bot.http, "connector", discord.utils.MISSING)
    if connector is not discord.utils.MISSING and getattr(connector, "closed", False):
        bot.http.connector = discord.utils.MISSING
    bot.clear()


def _inventory_bot():
    from inventory.bot import bot
    return bot


def _assistant_bot():
    from assistant.bot import bot
    return bot


def _roles_bot():
    from roles.bot import bot
    return bot


async def run_inventory_bot():
    """Run the inventory bot with auto-reconnect."""
    await _run_forever("Inventory", _inventory_bot, config["DISCORD_TOKEN"])


async def run_assistant_bot():
    """Run the assistant bot with auto-reconnect."""
    await _run_forever("Assistant", _assistant_bot, config["ASSISTANT_TOKEN"])


async def run_roles_bot():
    """Run the role management bot with auto-reconnect."""
    await _run_forever("Roles", _roles_bot, config["ROLES_TOKEN"])


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

    running = ["inventory"]

    if config.get("ASSISTANT_TOKEN"):
        tasks.append(asyncio.create_task(run_assistant_bot()))
        running.append("assistant")

    if config.get("ROLES_TOKEN"):
        tasks.append(asyncio.create_task(run_roles_bot()))
        running.append("roles")
    else:
        print("[Main] Roles bot skipped (no ROLES_TOKEN).")

    print(f"[Main] Starting with auto-reconnect: {', '.join(running)}")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
