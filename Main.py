import asyncio
from config import config


async def main():
    # ── Start Inventory Bot ──────────────────────────────────────────────────
    from inventory.bot import bot as inventory_bot, tree as inventory_tree

    inventory_task = asyncio.create_task(
        inventory_bot.start(config["DISCORD_TOKEN"])
    )
    print("[Main] Inventory bot starting...")

    # ── Start Assistant Bot (if token is configured) ─────────────────────────
    assistant_task = None
    if config.get("ASSISTANT_TOKEN"):
        try:
            from assistant.bot import bot as assistant_bot
            assistant_task = asyncio.create_task(
                assistant_bot.start(config["ASSISTANT_TOKEN"])
            )
            print("[Main] Assistant bot starting...")
        except ImportError:
            print("[Main] Assistant bot module not ready yet, skipping.")
    else:
        print("[Main] No ASSISTANT_TOKEN configured, running inventory bot only.")

    # ── Wait for all bots ────────────────────────────────────────────────────
    tasks = [inventory_task]
    if assistant_task:
        tasks.append(assistant_task)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())