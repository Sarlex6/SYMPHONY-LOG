import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

# ── Settings ─────────────────────────────────────────────────────────────────
MAX_RECENT_MESSAGES = 20       # Full messages kept per user
MEMORY_EXPIRY_DAYS = 3         # How long summaries persist
MAX_SUMMARY_LENGTH = 500       # Characters per summary
SAVE_INTERVAL_MINUTES = 5      # How often to auto-save to disk

# ── Disk storage path ────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
HISTORY_FILE = os.path.join(DATA_DIR, "conversation_history.json")
MEMORIES_FILE = os.path.join(DATA_DIR, "user_memories.json")

# ── In-memory storage ────────────────────────────────────────────────────────
# Recent messages per user: { user_id: [ {"role": "user"|"model", "text": "...", "timestamp": str}, ... ] }
conversation_history = defaultdict(list)

# Summarized memory per user: { user_id: { "summary": "...", "last_updated": str } }
user_memories = {}

# Track if we have unsaved changes
_dirty = False


# ── Disk persistence ─────────────────────────────────────────────────────────

def _ensure_data_dir():
    """Create the data directory if it doesn't exist."""
    os.makedirs(DATA_DIR, exist_ok=True)


def load_from_disk():
    """Load conversation history and memories from disk on startup."""
    global conversation_history, user_memories

    _ensure_data_dir()

    # Load conversation history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                raw = json.load(f)
            # Convert string keys back to int user IDs
            conversation_history = defaultdict(list)
            for uid_str, messages in raw.items():
                conversation_history[int(uid_str)] = messages
            print(f"[Memory] Loaded conversation history for {len(conversation_history)} users")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Memory] Failed to load history, starting fresh: {e}")
            conversation_history = defaultdict(list)

    # Load user memories
    if os.path.exists(MEMORIES_FILE):
        try:
            with open(MEMORIES_FILE, "r") as f:
                raw = json.load(f)
            user_memories = {int(uid_str): mem for uid_str, mem in raw.items()}
            print(f"[Memory] Loaded memory summaries for {len(user_memories)} users")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Memory] Failed to load memories, starting fresh: {e}")
            user_memories = {}


def save_to_disk():
    """Save current state to disk. Only writes if there are unsaved changes."""
    global _dirty

    if not _dirty:
        return

    _ensure_data_dir()

    try:
        # Convert int keys to strings for JSON
        history_data = {str(uid): messages for uid, messages in conversation_history.items()}
        with open(HISTORY_FILE, "w") as f:
            json.dump(history_data, f, default=str)

        memories_data = {str(uid): mem for uid, mem in user_memories.items()}
        with open(MEMORIES_FILE, "w") as f:
            json.dump(memories_data, f, default=str)

        _dirty = False
    except IOError as e:
        print(f"[Memory] Failed to save to disk: {e}")


def _mark_dirty():
    """Mark that in-memory state has changed and needs saving."""
    global _dirty
    _dirty = True


# ── Core memory functions ────────────────────────────────────────────────────

def add_message(user_id, role, text):
    """Add a message to a user's conversation history."""
    conversation_history[user_id].append({
        "role": role,
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # If we exceed the limit, summarize the oldest messages before trimming
    if len(conversation_history[user_id]) > MAX_RECENT_MESSAGES:
        _summarize_and_trim(user_id)

    _mark_dirty()


def get_history(user_id):
    """Get recent conversation history for API calls (just role + text)."""
    return [
        {"role": msg["role"], "text": msg["text"]}
        for msg in conversation_history[user_id]
    ]


def get_memory_summary(user_id):
    """Get the summarized memory of past conversations, if any."""
    mem = user_memories.get(user_id)
    if not mem:
        return None

    # Parse the stored timestamp
    last_updated = _parse_timestamp(mem["last_updated"])

    # Check if expired
    if datetime.utcnow() - last_updated > timedelta(days=MEMORY_EXPIRY_DAYS):
        user_memories.pop(user_id, None)
        _mark_dirty()
        return None

    return mem["summary"]


def _parse_timestamp(ts):
    """Parse a timestamp that might be a string or datetime."""
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.utcnow()


def _summarize_and_trim(user_id):
    """Summarize the oldest messages and keep only the recent ones."""
    history = conversation_history[user_id]

    # Take the oldest messages that will be trimmed
    overflow = history[:-MAX_RECENT_MESSAGES]
    kept = history[-MAX_RECENT_MESSAGES:]

    # Build a simple summary from the overflow
    summary_lines = []
    for msg in overflow:
        speaker = "User" if msg["role"] == "user" else "Angela"
        # Truncate long messages in the summary
        text = msg["text"][:100] + "..." if len(msg["text"]) > 100 else msg["text"]
        summary_lines.append(f"{speaker}: {text}")

    new_summary = "\n".join(summary_lines)

    # Merge with existing summary
    existing = user_memories.get(user_id, {}).get("summary", "")
    if existing:
        combined = existing + "\n" + new_summary
    else:
        combined = new_summary

    # Trim combined summary if too long (keep the most recent parts)
    if len(combined) > MAX_SUMMARY_LENGTH:
        combined = "..." + combined[-(MAX_SUMMARY_LENGTH - 3):]

    user_memories[user_id] = {
        "summary": combined,
        "last_updated": datetime.utcnow().isoformat(),
    }

    # Replace history with only the kept messages
    conversation_history[user_id] = kept
    _mark_dirty()


def cleanup_memories():
    """Remove expired memories and empty histories. Call periodically."""
    now = datetime.utcnow()

    # Clean expired memories
    expired = [
        uid for uid, mem in user_memories.items()
        if now - _parse_timestamp(mem["last_updated"]) > timedelta(days=MEMORY_EXPIRY_DAYS)
    ]
    for uid in expired:
        user_memories.pop(uid, None)

    # Clean empty conversation histories
    empty = [
        uid for uid, hist in conversation_history.items()
        if not hist
    ]
    for uid in empty:
        conversation_history.pop(uid, None)

    if expired or empty:
        _mark_dirty()
        print(f"[Memory] Cleaned {len(expired)} expired memories, {len(empty)} empty histories")

    # Save after cleanup
    save_to_disk()