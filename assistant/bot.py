import discord
import asyncio
from config import config
from assistant.gemini import generate_response, generate_gc_response
from assistant.memory import (
    add_message, get_history, get_memory_summary, cleanup_memories,
    load_from_disk, save_to_disk, SAVE_INTERVAL_MINUTES,
    should_update_profile, get_profile_context, generate_profile,
    user_profiles,
)
from assistant.knowledge import load_knowledge
from assistant.moderation import check_message, MONITORED_USER_IDS, _next_response
import assistant.gc as gc_mod
from assistant.gc import ALLOWED_GUILD_IDS

# ── Discord Bot setup ───────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents, max_messages=200)

# Memory cleanup interval (minutes)
MEMORY_CLEANUP_INTERVAL = 60

# How many recent channel messages to fetch for context
CHANNEL_CONTEXT_LIMIT = 25

OWNER_ID = 624559006072963082


def _should_respond(message):
    """Check if the bot should respond because it was mentioned or replied to."""
    if bot.user in message.mentions:
        return True
    if message.reference and message.reference.resolved:
        if message.reference.resolved.author == bot.user:
            return True
    return False


def _clean_mention(text):
    """Remove the bot's mention from the message text."""
    if bot.user:
        text = text.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    return text.strip()


async def _get_replied_message(message):
    """Fetch the message being replied to, if any."""
    if not message.reference:
        return None

    try:
        if message.reference.resolved:
            ref = message.reference.resolved
            return {
                "author": ref.author.display_name,
                "content": ref.content[:500] if ref.content else "[no text content]",
            }

        ref = await message.channel.fetch_message(message.reference.message_id)
        return {
            "author": ref.author.display_name,
            "content": ref.content[:500] if ref.content else "[no text content]",
        }
    except (discord.NotFound, discord.HTTPException):
        return None


async def _get_channel_context(message, include_current=False):
    """Fetch recent channel messages for conversational context."""
    context_messages = []
    try:
        async for msg in message.channel.history(limit=CHANNEL_CONTEXT_LIMIT + 1, before=message):
            if not msg.content:
                continue

            author_name = msg.author.display_name
            is_bot = msg.author == bot.user

            content = msg.content
            for user_mention in msg.mentions:
                content = content.replace(f"<@{user_mention.id}>", f"@{user_mention.display_name}")
                content = content.replace(f"<@!{user_mention.id}>", f"@{user_mention.display_name}")

            entry = {
                "author": author_name,
                "content": content[:300],
                "is_angela": is_bot,
            }

            if msg.reference and msg.reference.resolved:
                ref = msg.reference.resolved
                entry["replying_to"] = {
                    "author": ref.author.display_name,
                    "content": ref.content[:150] if ref.content else "[no text]",
                }

            context_messages.append(entry)

    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[Assistant] Failed to fetch channel history: {e}")

    context_messages.reverse()  # oldest first

    if include_current and message.content:
        content = message.content
        for user_mention in message.mentions:
            content = content.replace(f"<@{user_mention.id}>", f"@{user_mention.display_name}")
            content = content.replace(f"<@!{user_mention.id}>", f"@{user_mention.display_name}")
        context_messages.append({
            "author": message.author.display_name,
            "content": content[:300],
            "is_angela": False,
        })

    return context_messages


_initialized = False

@bot.event
async def on_ready():
    global _initialized
    print(f"[Assistant] Logged in as {bot.user} (ID: {bot.user.id})")

    if not _initialized:
        load_from_disk()
        load_knowledge()
        bot.loop.create_task(_memory_cleanup_loop())
        bot.loop.create_task(_memory_save_loop())
        _initialized = True
    else:
        print("[Assistant] Reconnected (skipping re-initialization).")

    print("[Assistant] Bot is ready!")


@bot.event
async def on_disconnect():
    print("[Assistant] Disconnected from Discord gateway.")


@bot.event
async def on_resumed():
    print("[Assistant] Resumed Discord gateway session.")


@bot.event
async def on_message(message):
    # Owner-only commands
    if message.author.id == OWNER_ID and "!profile" in message.content and bot.user in message.mentions:
        await _handle_profile_command(message)
        return

    # Ignore all bots (including self)
    if message.author.bot or message.author == bot.user:
        return

    # Block DMs — Angela does not respond to direct messages
    if isinstance(message.channel, discord.DMChannel):
        return

    # ── Content moderation for monitored users ──────────────────────────────
    if message.author.id in MONITORED_USER_IDS and message.content:
        flagged = await check_message(message.content)
        if flagged:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[Moderation] Could not delete message from {message.author}: {e}")
                return

            try:
                await message.channel.send(_next_response(message.author.mention))
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[Moderation] Could not send warning: {e}")
            return

    # ── Track messages for GC auto-respond ──────────────────────────────────
    in_allowed_guild = message.guild is not None and message.guild.id in ALLOWED_GUILD_IDS
    if in_allowed_guild:
        gc_mod.record_message(message.channel.id)

    # ── Path 1: Directly mentioned or replied to → targeted response ─────────
    if _should_respond(message):
        await _handle_direct_response(message)
        return

    # ── Path 2: GC auto-respond (unprompted) ────────────────────────────────
    if in_allowed_guild:
        channel_id = message.channel.id

        if gc_mod.is_active(channel_id):
            # Active — participate naturally within cooldown limits
            if (not gc_mod.is_on_cooldown(channel_id)
                    and gc_mod.get_new_message_count(channel_id) >= gc_mod.GC_MIN_NEW_MESSAGES):
                await _handle_gc_response(message)
        elif gc_mod.is_activation_trigger(message.content):
            # Inactive but keyword detected — wake up and respond immediately
            gc_mod.activate(channel_id)
            await _handle_gc_response(message, force=True)


async def _handle_direct_response(message):
    """Respond to a message that directly mentioned or replied to Angela."""
    user_text = _clean_mention(message.content)
    user_name = message.author.display_name
    user_id = message.author.id

    replied_message = await _get_replied_message(message)
    channel_context = await _get_channel_context(message)

    if not user_text:
        user_text = "[This user mentioned you without a specific message. Read the recent channel messages and respond to what's being discussed.]"

    memory_summary = get_memory_summary(user_id)
    history = get_history(user_id)

    async with message.channel.typing():
        response = await generate_response(
            user_name=user_name,
            user_message=user_text,
            conversation_history=history,
            memory_summary=memory_summary,
            replied_message=replied_message,
            channel_context=channel_context,
        )

    add_message(user_id, "user", f"{user_name}: {user_text}")
    add_message(user_id, "model", response)
    save_to_disk()

    if len(response) <= 2000:
        await message.reply(response, mention_author=False)
    else:
        chunks = _split_response(response)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

    if should_update_profile(user_id):
        profile_messages = get_profile_context(user_id, user_name)
        bot.loop.create_task(_run_profile_update(user_id, user_name, profile_messages))


_gc_processing: set[int] = set()  # channels currently generating a GC response


async def _handle_gc_response(message, force=False):
    """Decide with Flash Lite, then generate an unprompted GC response if warranted.

    force=True skips the gate (used on activation trigger).
    """
    channel_id = message.channel.id

    # Prevent concurrent GC responses in the same channel
    if channel_id in _gc_processing:
        return
    _gc_processing.add(channel_id)

    try:
        channel_context = await _get_channel_context(message, include_current=True)

        if not force:
            should = await gc_mod.should_respond(channel_context)
            if not should:
                return

        # Lock the cooldown immediately so messages arriving during generation don't retrigger
        gc_mod.record_response(channel_id)

        async with message.channel.typing():
            response = await generate_gc_response(channel_context)

        if not response:
            return

        if len(response) <= 2000:
            await message.channel.send(response)
        else:
            for chunk in _split_response(response):
                await message.channel.send(chunk)

    finally:
        _gc_processing.discard(channel_id)


async def _run_profile_update(user_id, user_name, messages):
    """Background task to generate/update a user profile."""
    try:
        await generate_profile(user_id, user_name, messages)
    except Exception as e:
        print(f"[Assistant] Profile update failed for {user_name}: {e}")


async def _handle_profile_command(message):
    """Handle !profile @user command. Owner only."""
    target = None
    for user in message.mentions:
        if user != bot.user:
            target = user
            break

    if not target:
        await message.reply("Usage: `@Angela !profile @user`", mention_author=False)
        return

    profile = user_profiles.get(target.id)

    if profile:
        embed = discord.Embed(
            title=f"Profile: {target.display_name}",
            description=profile["profile"],
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Last updated: {profile['last_updated'][:19]} UTC")
        embed.set_thumbnail(url=target.display_avatar.url)
        await message.reply(embed=embed, mention_author=False)
    else:
        from assistant.memory import conversation_history
        msg_count = len(conversation_history.get(target.id, []))
        if msg_count > 0:
            await message.reply(
                f"No profile for **{target.display_name}** yet ({msg_count} messages in history). Generating now...",
                mention_author=False,
            )
            profile_messages = get_profile_context(target.id, target.display_name)
            await generate_profile(target.id, target.display_name, profile_messages)

            new_profile = user_profiles.get(target.id)
            if new_profile:
                embed = discord.Embed(
                    title=f"Profile: {target.display_name}",
                    description=new_profile["profile"],
                    color=discord.Color.green(),
                )
                embed.set_footer(text="Freshly generated")
                embed.set_thumbnail(url=target.display_avatar.url)
                await message.reply(embed=embed, mention_author=False)
            else:
                await message.reply("Profile generation failed. Try again later.", mention_author=False)
        else:
            await message.reply(
                f"No data on **{target.display_name}** — they haven't talked to me yet.",
                mention_author=False,
            )


def _split_response(text, max_len=2000):
    """Split a long response into chunks, preferring sentence boundaries."""
    chunks = []
    while len(text) > max_len:
        split_at = max_len
        for sep in [". ", ".\n", "! ", "!\n", "? ", "?\n", "\n"]:
            idx = text[:max_len].rfind(sep)
            if idx > max_len // 2:
                split_at = idx + len(sep)
                break

        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()

    if text:
        chunks.append(text)

    return chunks


async def _memory_cleanup_loop():
    """Periodically clean up expired memories."""
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            cleanup_memories()
        except Exception as e:
            print(f"[Assistant] Memory cleanup error: {e}")

        await asyncio.sleep(MEMORY_CLEANUP_INTERVAL * 60)


async def _memory_save_loop():
    """Periodically save memory to disk as a safety net."""
    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(SAVE_INTERVAL_MINUTES * 60)
        try:
            save_to_disk()
        except Exception as e:
            print(f"[Assistant] Memory save error: {e}")
