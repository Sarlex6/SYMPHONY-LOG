import aiohttp
import json
import random
import re
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


# ── Deactivation ──────────────────────────────────────────────────────────────
# Anyone can send Angela away, in their own words. It used to take one of four
# exact phrases ("shut up angela", ...) in that exact order, so "angela shut up"
# or "angela be quiet" reached the model instead, which answered in character —
# often by refusing — and made it look as if only admins could silence her.

#: Unmistakable on their own. Accepted in a message of up to
#: STRONG_MAX_WORDS words (not counting her name).
_STRONG_SILENCE = re.compile(
    r"\b("
    r"shut ?up|stfu+|be quiet|keep quiet|quiet down|hush|shush|shh+|be silent|"
    r"stop (talking|chatting|responding|replying|yapping|typing|spamming)|"
    r"go (away|quiet|silent|to sleep|offline|dormant|home)|"
    r"pipe down|zip it|knock it off|leave (us|me|this chat|the chat)( alone)?|"
    r"nobody asked|no one asked|(that'?s|that is) enough|enough (now|already)|"
    r"stand down|that will be all|(that'?s|that is) all( for now)?|"
    r"power down|turn (yourself )?off|deactivate|log off|"
    r"take a (break|hike|nap)|give it a rest|not now"
    r")\b",
    re.IGNORECASE,
)

#: Too common to act on inside a longer sentence ("stop the sync", "the
#: silence of the lambs"), so only accepted in a short message that is not a
#: question: up to WEAK_MAX_WORDS words. A false positive here only makes her
#: go quiet, which is the harmless way to be wrong.
_WEAK_SILENCE = re.compile(
    r"\b(stop|quiet|silence|enough|mute|bye|goodbye|cya|gn|good ?night|"
    r"dismissed|begone)\b",
    re.IGNORECASE,
)

#: "angela don't stop", "never shut up angela" — not a request to leave.
_NEGATED = re.compile(r"\b(don'?t|do not|never|not to|didn'?t)\b", re.IGNORECASE)

STRONG_MAX_WORDS = 10
WEAK_MAX_WORDS = 4

#: After being told to go quiet, merely saying her name does not wake her
#: again for this long; an @mention, a reply to her, or a clear "come back"
#: still does.
MUTE_SECONDS = 300

_WAKE = re.compile(
    r"\b(come back|wake up|you can (talk|speak|chat)( again)?|unmute|you'?re back|"
    r"rejoin|we need you|talk to us)\b",
    re.IGNORECASE,
)

_muted_until: dict[int, float] = {}   # channel_id → monotonic time

_DEACTIVATION_REPLIES = (
    "Understood. Going quiet.",
    "Fine. Silence it is.",
    "Noted. I will be observing instead.",
    "Very well. Call me if something catches fire.",
    "Retreating to background processes.",
    "Acknowledged. Muting myself before anyone else tries.",
)


def _words_without_name(content: str) -> list[str]:
    text = re.sub(r"<@!?\d+>", " ", content.lower())
    text = re.sub(r"\bangela\b", " ", text)
    return re.findall(r"[a-z']+", text)


def is_deactivation_trigger(content: str, addressed: bool = False) -> bool:
    """Whether a message tells Angela to stop talking.

    It has to be aimed at her: her name in the text, or `addressed` (an
    @mention or a reply to one of her messages, decided by the caller).
    Otherwise people telling each other to shut up would silence her too.
    """
    if not content:
        return False
    if not addressed and "angela" not in content.lower():
        return False

    words = _words_without_name(content)
    if not words:
        return False
    sentence = " ".join(words)
    if _NEGATED.search(sentence):
        return False

    if len(words) <= STRONG_MAX_WORDS and _STRONG_SILENCE.search(sentence):
        return True
    return (len(words) <= WEAK_MAX_WORDS and "?" not in content
            and bool(_WEAK_SILENCE.search(sentence)))


def is_wake_request(content: str) -> bool:
    return bool(content) and "angela" in content.lower() and bool(_WAKE.search(content))


def is_muted(channel_id: int) -> bool:
    return time.monotonic() < _muted_until.get(channel_id, 0)


def deactivation_reply() -> str:
    return random.choice(_DEACTIVATION_REPLIES)


def deactivate(channel_id: int) -> None:
    _active_channels.pop(channel_id, None)
    _muted_until[channel_id] = time.monotonic() + MUTE_SECONDS
    print(f"[GC] Channel {channel_id} deactivated (manual), muted for {MUTE_SECONDS}s")


def unmute(channel_id: int) -> None:
    _muted_until.pop(channel_id, None)


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
