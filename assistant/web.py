from aiohttp import web
import os

# Secret token for authentication — set CONSOLE_SECRET env var
CONSOLE_SECRET = os.environ.get("CONSOLE_SECRET", "")

# Reference to the assistant bot — set by main.py after import
_bot = None


def set_bot(bot):
    global _bot
    _bot = bot


async def _check_auth(request):
    """Verify the authorization token."""
    if not CONSOLE_SECRET:
        return web.json_response({"error": "CONSOLE_SECRET not configured"}, status=503)

    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()

    if token != CONSOLE_SECRET:
        return web.json_response({"error": "Unauthorized"}, status=401)

    return None  # Auth OK


async def handle_say(request):
    """Send a message to a channel as Angela."""
    auth_err = await _check_auth(request)
    if auth_err:
        return auth_err

    if not _bot or not _bot.is_ready():
        return web.json_response({"error": "Bot not ready"}, status=503)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    channel_id = data.get("channel_id")
    message = data.get("message", "").strip()
    reply_to = data.get("reply_to")  # optional message ID to reply to

    if not channel_id or not message:
        return web.json_response({"error": "channel_id and message required"}, status=400)

    try:
        channel = _bot.get_channel(int(channel_id))
        if not channel:
            channel = await _bot.fetch_channel(int(channel_id))
    except Exception as e:
        return web.json_response({"error": f"Channel not found: {e}"}, status=404)

    try:
        if reply_to:
            try:
                ref_msg = await channel.fetch_message(int(reply_to))
                sent = await ref_msg.reply(message, mention_author=False)
            except Exception:
                sent = await channel.send(message)
        else:
            sent = await channel.send(message)

        return web.json_response({
            "ok": True,
            "message_id": sent.id,
            "channel": channel.name,
        })
    except Exception as e:
        return web.json_response({"error": f"Failed to send: {e}"}, status=500)


async def handle_channels(request):
    """List available channels."""
    auth_err = await _check_auth(request)
    if auth_err:
        return auth_err

    if not _bot or not _bot.is_ready():
        return web.json_response({"error": "Bot not ready"}, status=503)

    channels = []
    for guild in _bot.guilds:
        for ch in guild.text_channels:
            channels.append({
                "id": str(ch.id),
                "name": ch.name,
                "guild": guild.name,
            })

    return web.json_response({"channels": channels})


async def handle_health(request):
    """Health check endpoint for Koyeb."""
    ready = _bot is not None and _bot.is_ready()
    return web.json_response({"status": "ok" if ready else "starting", "bot_ready": ready})


def create_app():
    """Create the aiohttp web application."""
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/channels", handle_channels)
    app.router.add_post("/say", handle_say)
    return app


async def start_web_server(host="0.0.0.0", port=8000):
    """Start the web server."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[Web] HTTP server running on {host}:{port}")
    print(f"[Web] Endpoints: GET /health, GET /channels, POST /say")
    if CONSOLE_SECRET:
        print(f"[Web] Auth configured (CONSOLE_SECRET set)")
    else:
        print(f"[Web] WARNING: CONSOLE_SECRET not set, /say and /channels will be disabled")