import discord
import asyncio
from config import config
from assistant.gemini import generate_response
from assistant.memory import (
    add_message, get_history, get_memory_summary, cleanup_memories,
    load_from_disk, save_to_disk, SAVE_INTERVAL_MINUTES,
    should_update_profile, get_profile_context, generate_profile,
    user_profiles,
)
from assistant.knowledge import load_knowledge
from assistant.moderation import check_message, MONITORED_USER_IDS, _next_response

# ── Discord Bot setup ───────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents, max_messages=200)

# Memory cleanup interval (minutes)
MEMORY_CLEANUP_INTERVAL = 60

# How many recent channel messages to fetch for context
CHANNEL_CONTEXT_LIMIT = 25


def _should_respond(message):
    """Check if the bot should respond to this message."""
    # Never respond to self
    if message.author == bot.user:
        return False

    # Never respond to other bots
    if message.author.bot:
        return False

    # Respond if mentioned
    if bot.user in message.mentions:
        return True

    # Respond if replying to one of the bot's messages
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
        # Try the cached resolved message first
        if message.reference.resolved:
            ref = message.reference.resolved
            return {
                "author": ref.author.display_name,
                "content": ref.content[:500] if ref.content else "[no text content]",
            }

        # Fall back to fetching
        ref = await message.channel.fetch_message(message.reference.message_id)
        return {
            "author": ref.author.display_name,
            "content": ref.content[:500] if ref.content else "[no text content]",
        }
    except (discord.NotFound, discord.HTTPException):
        return None


async def _get_channel_context(message):
    """Fetch recent channel messages for conversational context."""
    context_messages = []
    try:
        async for msg in message.channel.history(limit=CHANNEL_CONTEXT_LIMIT + 1, before=message):
            # Skip empty messages
            if not msg.content:
                continue

            author_name = msg.author.display_name
            is_bot = msg.author == bot.user

            # Clean up the message content — replace raw mentions with display names
            content = msg.content
            for user_mention in msg.mentions:
                content = content.replace(f"<@{user_mention.id}>", f"@{user_mention.display_name}")
                content = content.replace(f"<@!{user_mention.id}>", f"@{user_mention.display_name}")

            entry = {
                "author": author_name,
                "content": content[:300],  # Truncate long messages
                "is_angela": is_bot,
            }

            # If this message was a reply, note what it replied to
            if msg.reference and msg.reference.resolved:
                ref = msg.reference.resolved
                entry["replying_to"] = {
                    "author": ref.author.display_name,
                    "content": ref.content[:150] if ref.content else "[no text]",
                }

            context_messages.append(entry)

    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[Assistant] Failed to fetch channel history: {e}")

    # Reverse so oldest is first (history returns newest first)
    context_messages.reverse()
    return context_messages


_initialized = False

@bot.event
async def on_ready():
    global _initialized
    print(f"[Assistant] Logged in as {bot.user} (ID: {bot.user.id})")

    if not _initialized:
        # Only run these once, not on every reconnect
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


OWNER_ID = 624559006072963082

@bot.event
async def on_message(message):
    # Handle !profile command (owner only, must mention the bot)
    if message.author.id == OWNER_ID and "!profile" in message.content and bot.user in message.mentions:
        await _handle_profile_command(message)
        return

    # ── Content moderation for monitored users ──────────────────────────────
    if message.author.id in MONITORED_USER_IDS and message.content:
        flagged = await check_message(message.content)
        if flagged:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[Moderation] Could not delete message from {message.author}: {e}")
                return  # Can't delete, don't respond either

            mention = message.author.mention
            warning = _next_response(mention)
            try:
                await message.channel.send(warning)
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[Moderation] Could not send warning: {e}")
            return

    if not _should_respond(message):
        return

    # Clean the message text
    user_text = _clean_mention(message.content)

    user_name = message.author.display_name
    user_id = message.author.id

    # Get reply context if replying to someone
    replied_message = await _get_replied_message(message)

    # Fetch recent channel messages for context
    channel_context = await _get_channel_context(message)

    # If the message is empty (just a mention), let Angela read the channel context
    # and decide what to respond to
    if not user_text:
        user_text = "[This user mentioned you without a specific message. Read the recent channel messages and respond to what's being discussed.]"

    # Get conversation memory
    memory_summary = get_memory_summary(user_id)
    history = get_history(user_id)

    # Show typing indicator while generating
    async with message.channel.typing():
        response = await generate_response(
            user_name=user_name,
            user_message=user_text,
            conversation_history=history,
            memory_summary=memory_summary,
            replied_message=replied_message,
            channel_context=channel_context,
        )

    # Store the exchange in memory
    add_message(user_id, "user", f"{user_name}: {user_text}")
    add_message(user_id, "model", response)

    # Save to disk after each exchange
    save_to_disk()

    # Discord has a 2000 character limit — split if needed
    if len(response) <= 2000:
        await message.reply(response, mention_author=False)
    else:
        # Split into chunks at sentence boundaries where possible
        chunks = _split_response(response)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

    # Check if we should update this user's profile (runs in background, no delay)
    if should_update_profile(user_id):
        profile_messages = get_profile_context(user_id, user_name)
        bot.loop.create_task(_run_profile_update(user_id, user_name, profile_messages))


async def _run_profile_update(user_id, user_name, messages):
    """Background task to generate/update a user profile."""
    try:
        await generate_profile(user_id, user_name, messages)
    except Exception as e:
        print(f"[Assistant] Profile update failed for {user_name}: {e}")


async def _handle_profile_command(message):
    """Handle !profile @user command. Owner only."""
    # Find the target user (any mentioned user that isn't the bot)
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
        # No profile exists — offer to generate one
        from assistant.memory import conversation_history
        msg_count = len(conversation_history.get(target.id, []))
        if msg_count > 0:
            await message.reply(
                f"No profile for **{target.display_name}** yet ({msg_count} messages in history). Generating now...",
                mention_author=False,
            )
            profile_messages = get_profile_context(target.id, target.display_name)
            await generate_profile(target.id, target.display_name, profile_messages)

            # Check if it was created
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
        # Try to split at the last sentence boundary before max_len
        split_at = max_len
        for sep in [". ", ".\n", "! ", "!\n", "? ", "?\n", "\n"]:
            idx = text[:max_len].rfind(sep)
            if idx > max_len // 2:  # Don't split too early
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