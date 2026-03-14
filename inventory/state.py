import discord
from datetime import datetime, timedelta
import asyncio

from inventory.sheets import (
    calculate_cart_points, get_cached_quantity,
    refresh_cache,
)

# ── Settings ─────────────────────────────────────────────────────────────────
PENDING_REQUEST_TIMEOUT_DAYS = 7
CART_TIMEOUT_HOURS = 2
CLEANUP_INTERVAL_MINUTES = 30

# ── Per-user cart storage ────────────────────────────────────────────────────
# { user_id: { "cart": [...], "last_updated": datetime } }
user_carts = {}

# ── Pending approval requests ────────────────────────────────────────────────
# { request_id: { "view", "message", "cart", "requester", "requester_name", "requester_avatar", "note", "created_at" } }
pending_requests = {}


# ── Cart helper functions ────────────────────────────────────────────────────

def get_user_cart(user_id):
    """Get a user's cart items, or empty list."""
    data = user_carts.get(user_id)
    if data is None:
        return []
    return data.get("cart", [])


def set_user_cart(user_id, cart):
    """Set a user's cart and update the timestamp."""
    user_carts[user_id] = {
        "cart": cart,
        "last_updated": datetime.utcnow(),
    }


def clear_user_cart(user_id):
    """Remove a user's cart entirely."""
    user_carts.pop(user_id, None)


def append_to_user_cart(user_id, entry):
    """Add an item to a user's cart."""
    data = user_carts.get(user_id)
    if data is None:
        user_carts[user_id] = {
            "cart": [entry],
            "last_updated": datetime.utcnow(),
        }
    else:
        data["cart"].append(entry)
        data["last_updated"] = datetime.utcnow()


# ── Pending embed helpers ────────────────────────────────────────────────────

async def update_pending_embeds(exclude_request_id=None):
    from inventory.sheets import build_approval_embeds

    to_remove = []

    for req_id, req_data in pending_requests.items():
        if req_id == exclude_request_id:
            continue

        try:
            message = req_data["message"]  # this is always the LAST message (with buttons)
            cart = req_data["cart"]
            requester_name = req_data["requester_name"]
            requester_avatar = req_data["requester_avatar"]
            note = req_data.get("note", "")

            embeds = build_approval_embeds(cart, requester_name, requester_avatar, note)

            await message.edit(embed=embeds[-1])

        except (discord.NotFound, discord.HTTPException):
            to_remove.append(req_id)

    for req_id in to_remove:
        pending_requests.pop(req_id, None)


# ── Cleanup task ─────────────────────────────────────────────────────────────

async def cleanup_loop(bot):
    """Periodically clean up expired pending requests and stale carts."""
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            now = datetime.utcnow()

            # ── Expire old pending requests ──
            expired_requests = []
            for req_id, req_data in list(pending_requests.items()):
                created_at = req_data.get("created_at", now)
                if now - created_at > timedelta(days=PENDING_REQUEST_TIMEOUT_DAYS):
                    expired_requests.append(req_id)

            for req_id in expired_requests:
                req_data = pending_requests.pop(req_id, None)
                if not req_data:
                    continue

                try:
                    message = req_data["message"]
                    embed = message.embeds[0] if message.embeds else None

                    if embed:
                        embed.color = discord.Color.dark_grey()
                        embed.add_field(
                            name="⏰ Timed Out",
                            value=f"This request expired after {PENDING_REQUEST_TIMEOUT_DAYS} days without a response.\n{now.strftime('%d/%m/%Y %H:%M UTC')}",
                            inline=False
                        )
                        await message.edit(embed=embed, view=None)

                except (discord.NotFound, discord.HTTPException):
                    pass

                try:
                    requester = req_data.get("requester")
                    if requester:
                        await requester.send(
                            f"⏰ Your log request has **timed out** after {PENDING_REQUEST_TIMEOUT_DAYS} days without approval or rejection.\n"
                            f"Please submit a new request if still needed."
                        )
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if expired_requests:
                print(f"Cleanup: expired {len(expired_requests)} pending request(s)")

            # ── Clear stale user carts ──
            stale_carts = []
            for user_id, cart_data in list(user_carts.items()):
                last_updated = cart_data.get("last_updated", now)
                if now - last_updated > timedelta(hours=CART_TIMEOUT_HOURS):
                    stale_carts.append(user_id)

            for user_id in stale_carts:
                user_carts.pop(user_id, None)

            if stale_carts:
                print(f"Cleanup: cleared {len(stale_carts)} stale cart(s)")

        except Exception as e:
            print(f"Cleanup error: {e}")

        await asyncio.sleep(CLEANUP_INTERVAL_MINUTES * 60)