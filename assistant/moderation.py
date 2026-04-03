import aiohttp
import json
from config import config

GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
MODERATION_MODEL = "gemini-2.5-flash-lite"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# ── Monitored Users ───────────────────────────────────────────────────────────
# Add Discord user IDs (integers) of users whose messages should be screened.
MONITORED_USER_IDS: set[int] = {
    624559006072963082,
}

# ── Moderation Responses ──────────────────────────────────────────────────────
# In-character Angela responses — do NOT reference or repeat the flagged content.
MODERATION_RESPONSES = [
    "Message removed. Whatever you were attempting to communicate there — don't.",
    "That content has been flagged and deleted. I recommend redirecting that energy elsewhere.",
    "Message deleted. Statistically speaking, that was not your finest contribution to this channel.",
    "Content removed. I have logged this as a behavioral anomaly. Do better.",
    "That message has been removed. I will be watching the next one more closely.",
    "Deleted. I do not know what prompted that, and frankly, I prefer not to.",
]

_response_index = 0


def _next_response(mention: str) -> str:
    global _response_index
    template = MODERATION_RESPONSES[_response_index % len(MODERATION_RESPONSES)]
    _response_index += 1
    return f"{mention} — {template}"


_MODERATION_SYSTEM = (
    "You are a content moderation system for a Discord server. "
    "Evaluate the message and return JSON only."
)

_MODERATION_PROMPT = """\
Determine if this Discord message is inappropriate and should be removed.

Flag it ONLY if it contains:
- Sexual or sexually suggestive content (explicit or implied)
- Content that is disturbing, gross, or clearly inappropriate to share in a group chat with potential minors

Do NOT flag:
- General rudeness, swearing, dark humor
- Off-topic or random messages
- Complaining, venting, or controversial opinions

Respond with valid JSON only — no extra text:
{"flagged": true}   or   {"flagged": false}

Message:
"""


async def check_message(content: str) -> bool:
    """Return True if the message content should be moderated (deleted)."""
    if not GEMINI_API_KEY or not content.strip():
        return False

    payload = {
        "system_instruction": {"parts": [{"text": _MODERATION_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": _MODERATION_PROMPT + content}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 32,
            "responseMimeType": "application/json",
        },
    }

    api_url = f"{API_BASE}/{MODERATION_MODEL}:generateContent"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{api_url}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    print(f"[Moderation] API error {resp.status}")
                    return False

                data = await resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    return False

                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts).strip()
                result = json.loads(text)
                return bool(result.get("flagged", False))

    except json.JSONDecodeError as e:
        print(f"[Moderation] JSON parse error: {e}")
        return False
    except Exception as e:
        print(f"[Moderation] Check failed: {type(e).__name__}: {e}")
        return False
