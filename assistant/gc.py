import aiohttp
import json
import time
from config import config

GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
LITE_MODEL = "gemini-2.5-flash-lite"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# ── Allowed Guilds ────────────────────────────────────────────────────────────
# Angela will speak unprompted only in these Discord servers.
# Add guild IDs (integers) for each server she should roam freely in.
ALLOWED_GUILD_IDS: set[int] = {
    1498364524552523968,
    1246219094521548810,
}

# ── Tuning ────────────────────────────────────────────────────────────────────
# Minimum seconds between unprompted responses in the same channel.
GC_COOLDOWN_SECONDS = 15

# Minimum new human messages since Angela's last response before even checking.
GC_MIN_NEW_MESSAGES = 1

# Seconds of silence before Angela deactivates in a channel.
ACTIVATION_TIMEOUT_SECONDS = 60

# Case-insensitive substrings that wake Angela up in an inactive channel.
ACTIVATION_KEYWORDS: set[str] = {"angela"}

# ── State (in-memory, resets on restart) ─────────────────────────────────────
_last_response:    dict[int, float] = {}   # channel_id → last response timestamp
_new_messages:     dict[int, int]   = {}   # channel_id → count since last response
_last_activity:    dict[int, float] = {}   # channel_id → last human message timestamp (for timeout)


def record_message(channel_id: int) -> None:
    """Call for every new human message in a GC-eligible channel."""
    _new_messages[channel_id] = _new_messages.get(channel_id, 0) + 1
    _last_activity[channel_id] = time.monotonic()


def is_on_cooldown(channel_id: int) -> bool:
    return (time.monotonic() - _last_response.get(channel_id, 0)) < GC_COOLDOWN_SECONDS


def get_new_message_count(channel_id: int) -> int:
    return _new_messages.get(channel_id, 0)


def record_response(channel_id: int) -> None:
    """Call after Angela sends an unprompted message."""
    _last_response[channel_id] = time.monotonic()
    _new_messages[channel_id] = 0


# ── Activation state ──────────────────────────────────────────────────────────
_active_channels: dict[int, float] = {}   # channel_id → activation timestamp


def is_active(channel_id: int) -> bool:
    """Return True if Angela is currently active in this channel."""
    last = _last_activity.get(channel_id, 0)
    if channel_id not in _active_channels:
        return False
    if time.monotonic() - last > ACTIVATION_TIMEOUT_SECONDS:
        _active_channels.pop(channel_id, None)
        print(f"[GC] Channel {channel_id} deactivated (timeout)")
        return False
    return True


def activate(channel_id: int) -> None:
    _active_channels[channel_id] = time.monotonic()
    print(f"[GC] Channel {channel_id} activated")


def is_activation_trigger(content: str) -> bool:
    """Return True if the message text contains an activation keyword."""
    lower = content.lower()
    return any(kw in lower for kw in ACTIVATION_KEYWORDS)


# ── Relevance gate ────────────────────────────────────────────────────────────

_GATE_PROMPT = """\
You are a participation gate for a Discord AI called Angela. She is already active in \
this group chat. Decide if she should send a message RIGHT NOW.

Say YES if:
- Someone asks a question or shares an opinion worth reacting to
- People are having a real back-and-forth discussion
- Someone says something noteworthy, funny, or surprising
- Angela was addressed or her name was mentioned

Say NO if:
- The last message is a short greeting or acknowledgment aimed at a specific person \
  (e.g. "hi @bob", "lol", "ok", "same")
- It is a bot command (starts with !, /, etc.)
- Someone is just posting a link or image with no comment
- The exchange is clearly between two specific people with nothing for Angela to add

Respond with JSON only — no extra text:
{"respond": true}   or   {"respond": false}

Recent messages (oldest → newest):
"""


async def should_respond(channel_context: list[dict]) -> bool:
    """Use Flash Lite to cheaply decide whether Angela should join the conversation."""
    if not GEMINI_API_KEY or len(channel_context) < GC_MIN_NEW_MESSAGES:
        return False

    lines = []
    for msg in channel_context[-20:]:
        speaker = "Angela" if msg.get("is_angela") else msg["author"]
        lines.append(f"{speaker}: {msg['content']}")

    prompt = _GATE_PROMPT + "\n".join(lines)

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 32,
            "responseMimeType": "application/json",
        },
    }

    api_url = f"{API_BASE}/{LITE_MODEL}:generateContent"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{api_url}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    print(f"[GC] Gate API error {resp.status}")
                    return False

                data = await resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    return False

                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts).strip()
                result = json.loads(text)
                decision = bool(result.get("respond", False))
                print(f"[GC] Gate decision: {'YES' if decision else 'NO'}")
                return decision

    except json.JSONDecodeError as e:
        print(f"[GC] Gate JSON parse error: {e} | raw: {text!r}")
        return False
    except Exception as e:
        print(f"[GC] Gate check failed: {type(e).__name__}: {e}")
        return False
