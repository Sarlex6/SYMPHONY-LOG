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
}

# ── Tuning ────────────────────────────────────────────────────────────────────
# Minimum seconds between unprompted responses in the same channel.
GC_COOLDOWN_SECONDS = 15

# Minimum new human messages since Angela's last response before even checking.
GC_MIN_NEW_MESSAGES = 1

# ── State (in-memory, resets on restart) ─────────────────────────────────────
_last_response: dict[int, float] = {}   # channel_id → unix timestamp
_new_messages:  dict[int, int]   = {}   # channel_id → count since last response


def record_message(channel_id: int) -> None:
    """Call for every new human message in a GC-eligible channel."""
    _new_messages[channel_id] = _new_messages.get(channel_id, 0) + 1


def is_on_cooldown(channel_id: int) -> bool:
    return (time.monotonic() - _last_response.get(channel_id, 0)) < GC_COOLDOWN_SECONDS


def get_new_message_count(channel_id: int) -> int:
    return _new_messages.get(channel_id, 0)


def record_response(channel_id: int) -> None:
    """Call after Angela sends an unprompted message."""
    _last_response[channel_id] = time.monotonic()
    _new_messages[channel_id] = 0


# ── Relevance gate ────────────────────────────────────────────────────────────

_GATE_PROMPT = """\
You are a participation gate for a Discord AI called Angela. Decide if she should join \
the conversation RIGHT NOW based on the recent messages below.

Default to YES. Only say NO in these specific cases:
- The last message is a bot command (starts with !, /, etc.)
- Someone is just posting a link or image with no comment
- The conversation clearly ended and no one is saying anything meaningful

In all other cases — someone asking a question, sharing something, chatting, venting, \
joking, debating, even if it's just one person talking — say YES.

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
