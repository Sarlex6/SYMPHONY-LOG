"""Discord client host for the role management system.

Mirrors inventory/bot.py: a discord.Client plus an app_commands.CommandTree,
with an `_initialized` guard so a gateway reconnect does not re-run startup.

Runs on its own token (ROLES_TOKEN) because the role manager needs to be a
member of the main server *and* every branch server, which is a different
presence from the inventory bot. If a separate bot is not wanted, call
`roles.commands.register_commands(tree)` against an existing tree instead and
`attach_to(existing_client)` from that bot's on_ready.
"""

import discord
from discord import app_commands

from roles import config as roles_config
from roles.commands import register_commands
from roles.discord_sync import synchronizer as discord_synchronizer
from roles.poller import SheetPoller
from roles.service import service

# Members intent is required: role synchronization has to resolve guild members.
intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents, max_messages=100)
tree = app_commands.CommandTree(bot)

poller = SheetPoller(service)

_initialized = False


async def attach_to(client):
    """Wire an already-running Discord client into the role manager.

    Use this when the commands live on another bot's tree instead of on this
    module's own client.
    """
    discord_synchronizer.set_client(client)
    await service.start(discord_client=client)
    client.loop.create_task(poller.run(client))


@bot.event
async def on_ready():
    global _initialized
    print(f"[Roles] Logged in as {bot.user} (ID: {bot.user.id})")

    if not _initialized:
        register_commands(tree)
        await tree.sync()
        print("[Roles] Slash commands synced!")

        discord_synchronizer.set_client(bot)
        await service.start(discord_client=bot)

        # Structural setup is only meaningful once real configuration exists.
        if roles_config.current().is_configured():
            try:
                await service.repository.ensure_structure()
            except Exception as exc:
                print(f"[Roles] Sheet structure setup failed: {type(exc).__name__}: {exc}")
        else:
            print("[Roles] Unconfigured — skipping sheet structure setup. "
                  "Fill in roles_config.json, then run /roles reload.")

        bot.loop.create_task(poller.run(bot))
        _initialized = True
    else:
        print("[Roles] Reconnected (skipping re-initialization).")
        discord_synchronizer.set_client(bot)
        try:
            await service.repository.load()
        except Exception as exc:
            print(f"[Roles] Reload after reconnect failed: {type(exc).__name__}: {exc}")

    print("[Roles] Bot is ready!")


@bot.event
async def on_disconnect():
    print("[Roles] Disconnected from Discord gateway.")


@bot.event
async def on_resumed():
    print("[Roles] Resumed Discord gateway session.")


@bot.event
async def on_member_join(member):
    """Re-apply a joining member's roles from their authoritative record.

    Someone who leaves and rejoins loses their Discord roles but keeps their
    PERSONNEL record — the sheet is still authoritative, so the roles come back.
    """
    record = service.repository.by_discord_id(member.id)
    if record is None:
        return
    service.sync_queue.enqueue(record, reason=f"rejoined {member.guild.name}")
    print(f"[Roles] {record.label()} rejoined {member.guild.name} — sync queued.")
