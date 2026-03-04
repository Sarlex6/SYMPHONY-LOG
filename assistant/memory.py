import json
import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
from collections import defaultdict
from config import config

# ── Settings ─────────────────────────────────────────────────────────────────
MAX_RECENT_MESSAGES = 20       # Full messages kept per user
MEMORY_EXPIRY_DAYS = 7         # How long profiles persist (longer now since they're useful)
MAX_SUMMARY_LENGTH = 500       # Characters per raw summary fallback
SAVE_INTERVAL_MINUTES = 5      # How often to auto-save to disk
PROFILE_INTERVAL = 10          # Generate/update profile every N messages

# ── API config for profiling calls ───────────────────────────────────────────
GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")
PROFILE_MODEL = "gemini-2.5-flash-lite"  # Use cheap model for profiling
PROFILE_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{PROFILE_MODEL}:generateContent"

# ── Disk storage paths ───────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
HISTORY_FILE = os.path.join(DATA_DIR, "conversation_history.json")
MEMORIES_FILE = os.path.join(DATA_DIR, "user_memories.json")
PROFILES_FILE = os.path.join(DATA_DIR, "user_profiles.json")

# ── In-memory storage ────────────────────────────────────────────────────────
conversation_history = defaultdict(list)
user_memories = {}  # raw summaries (fallback)
user_profiles = {}  # AI-generated profiles: { user_id: { "profile": "...", "last_updated": str, "message_count": int } }
_pending_profiles = []  # list of (user_id, user_name, messages_to_profile) tuples
_dirty = False
_message_counts = defaultdict(int)  # messages since last profile per user


# ── Disk persistence ─────────────────────────────────────────────────────────

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_from_disk():
    global conversation_history, user_memories, user_profiles, _message_counts

    _ensure_data_dir()

    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                raw = json.load(f)
            conversation_history = defaultdict(list)
            for uid_str, messages in raw.items():
                conversation_history[int(uid_str)] = messages
            print(f"[Memory] Loaded conversation history for {len(conversation_history)} users")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Memory] Failed to load history, starting fresh: {e}")
            conversation_history = defaultdict(list)

    if os.path.exists(MEMORIES_FILE):
        try:
            with open(MEMORIES_FILE, "r") as f:
                raw = json.load(f)
            user_memories = {int(uid_str): mem for uid_str, mem in raw.items()}
            print(f"[Memory] Loaded memory summaries for {len(user_memories)} users")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Memory] Failed to load memories, starting fresh: {e}")
            user_memories = {}

    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r") as f:
                raw = json.load(f)
            user_profiles = {int(uid_str): prof for uid_str, prof in raw.items()}
            print(f"[Memory] Loaded profiles for {len(user_profiles)} users")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Memory] Failed to load profiles, starting fresh: {e}")
            user_profiles = {}


def save_to_disk():
    global _dirty

    if not _dirty:
        return

    _ensure_data_dir()

    try:
        history_data = {str(uid): messages for uid, messages in conversation_history.items()}
        with open(HISTORY_FILE, "w") as f:
            json.dump(history_data, f, default=str)

        memories_data = {str(uid): mem for uid, mem in user_memories.items()}
        with open(MEMORIES_FILE, "w") as f:
            json.dump(memories_data, f, default=str)

        profiles_data = {str(uid): prof for uid, prof in user_profiles.items()}
        with open(PROFILES_FILE, "w") as f:
            json.dump(profiles_data, f, default=str)

        _dirty = False
    except IOError as e:
        print(f"[Memory] Failed to save to disk: {e}")


def _mark_dirty():
    global _dirty
    _dirty = True


# ── Core memory functions ────────────────────────────────────────────────────

def add_message(user_id, role, text):
    conversation_history[user_id].append({
        "role": role,
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # Track message count for profiling trigger
    if role == "user":
        _message_counts[user_id] += 1

    # If we exceed the limit, do a basic trim
    if len(conversation_history[user_id]) > MAX_RECENT_MESSAGES:
        _basic_trim(user_id)

    _mark_dirty()


def should_update_profile(user_id):
    """Check if we should generate/update a profile for this user."""
    count = _message_counts.get(user_id, 0)
    if count >= PROFILE_INTERVAL:
        return True
    # Also profile if user has never been profiled and has enough messages
    if user_id not in user_profiles and count >= 5:
        return True
    return False


def get_profile_context(user_id, user_name):
    """Get the messages to use for profiling and reset the counter."""
    messages = []
    for msg in conversation_history[user_id]:
        speaker = user_name if msg["role"] == "user" else "Angela"
        messages.append(f"{speaker}: {msg['text'][:300]}")

    _message_counts[user_id] = 0
    return messages


def get_history(user_id):
    return [
        {"role": msg["role"], "text": msg["text"]}
        for msg in conversation_history[user_id]
    ]


def get_memory_summary(user_id):
    """Get profile (preferred) or raw summary as fallback."""
    # Try profile first
    prof = user_profiles.get(user_id)
    if prof:
        last_updated = _parse_timestamp(prof["last_updated"])
        if datetime.utcnow() - last_updated <= timedelta(days=MEMORY_EXPIRY_DAYS):
            return prof["profile"]
        else:
            user_profiles.pop(user_id, None)
            _mark_dirty()

    # Fall back to raw summary
    mem = user_memories.get(user_id)
    if not mem:
        return None

    last_updated = _parse_timestamp(mem["last_updated"])
    if datetime.utcnow() - last_updated > timedelta(days=MEMORY_EXPIRY_DAYS):
        user_memories.pop(user_id, None)
        _mark_dirty()
        return None

    return mem["summary"]


def update_profile(user_id, profile_text):
    """Store an AI-generated profile for a user."""
    user_profiles[user_id] = {
        "profile": profile_text,
        "last_updated": datetime.utcnow().isoformat(),
    }
    _mark_dirty()
    print(f"[Memory] Updated profile for user {user_id}")


def _parse_timestamp(ts):
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.utcnow()


def _basic_trim(user_id):
    """Trim old messages, keeping a raw text summary as fallback."""
    history = conversation_history[user_id]
    overflow = history[:-MAX_RECENT_MESSAGES]
    kept = history[-MAX_RECENT_MESSAGES:]

    summary_lines = []
    for msg in overflow:
        speaker = "User" if msg["role"] == "user" else "Angela"
        text = msg["text"][:100] + "..." if len(msg["text"]) > 100 else msg["text"]
        summary_lines.append(f"{speaker}: {text}")

    new_summary = "\n".join(summary_lines)

    existing = user_memories.get(user_id, {}).get("summary", "")
    if existing:
        combined = existing + "\n" + new_summary
    else:
        combined = new_summary

    if len(combined) > MAX_SUMMARY_LENGTH:
        combined = "..." + combined[-(MAX_SUMMARY_LENGTH - 3):]

    user_memories[user_id] = {
        "summary": combined,
        "last_updated": datetime.utcnow().isoformat(),
    }

    conversation_history[user_id] = kept
    _mark_dirty()


# ── AI Profile Generation ───────────────────────────────────────────────────

async def generate_profile(user_id, user_name, messages):
    """Call Gemini to generate/update a user profile. Runs in background."""
    if not GEMINI_API_KEY or not messages:
        return

    existing_profile = user_profiles.get(user_id, {}).get("profile", "")

    prompt_parts = [
        "You are a profiling system for an AI assistant named Angela. "
        "Based on the conversation excerpts below, create a concise profile of this user. "
        "Extract ONLY factual information and observed patterns. Be brief and structured.\n\n"
        "Include if available:\n"
        "- Display name and any known role/title\n"
        "- Key personality traits observed\n"
        "- Important facts they shared (preferences, background, etc)\n"
        "- How they typically interact (formal, casual, provocative, etc)\n"
        "- Any specific topics they care about\n\n"
        "Keep it under 300 characters. No fluff, just useful intel.\n"
    ]

    if existing_profile:
        prompt_parts.append(f"\nEXISTING PROFILE (update/merge with new info):\n{existing_profile}\n")

    prompt_parts.append(f"\nRECENT CONVERSATIONS WITH {user_name.upper()}:\n")
    prompt_parts.append("\n".join(messages[-20:]))  # Last 20 messages max
    prompt_parts.append("\n\nGenerate the updated profile now. Output ONLY the profile text, nothing else.")

    payload = {
        "contents": [{"role": "user", "parts": [{"text": "".join(prompt_parts)}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 512,
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PROFILE_API_URL}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        for part in parts:
                            if "text" in part:
                                profile_text = part["text"].strip()
                                if profile_text:
                                    update_profile(user_id, profile_text)
                                    save_to_disk()
                                    return
                    print(f"[Memory] Profile generation returned no content for {user_name}")
                else:
                    error_text = await resp.text()
                    print(f"[Memory] Profile API error {resp.status}: {error_text[:200]}")
    except Exception as e:
        print(f"[Memory] Profile generation failed for {user_name}: {type(e).__name__}: {e}")


def cleanup_memories():
    now = datetime.utcnow()

    expired_mem = [
        uid for uid, mem in user_memories.items()
        if now - _parse_timestamp(mem["last_updated"]) > timedelta(days=MEMORY_EXPIRY_DAYS)
    ]
    for uid in expired_mem:
        user_memories.pop(uid, None)

    expired_prof = [
        uid for uid, prof in user_profiles.items()
        if now - _parse_timestamp(prof["last_updated"]) > timedelta(days=MEMORY_EXPIRY_DAYS)
    ]
    for uid in expired_prof:
        user_profiles.pop(uid, None)

    empty = [
        uid for uid, hist in conversation_history.items()
        if not hist
    ]
    for uid in empty:
        conversation_history.pop(uid, None)

    if expired_mem or expired_prof or empty:
        _mark_dirty()
        print(f"[Memory] Cleaned {len(expired_mem)} memories, {len(expired_prof)} profiles, {len(empty)} empty histories")

    save_to_disk()