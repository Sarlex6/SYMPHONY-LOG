import os
import re
from collections import defaultdict

# ── Settings ─────────────────────────────────────────────────────────────────
KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")
MAX_CONTEXT_TOKENS = 3000       # Rough character limit for knowledge injected per request (~750 tokens)
MAX_SECTIONS = 3                # Max number of knowledge files to include per request

# ── Storage ──────────────────────────────────────────────────────────────────
# { topic_name: { "content": "...", "keywords": set(), "filename": "..." } }
knowledge_base = {}


def load_knowledge():
    """Load all .txt files from the knowledge/ directory."""
    global knowledge_base
    knowledge_base = {}

    if not os.path.exists(KNOWLEDGE_DIR):
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        print(f"[Knowledge] Created empty knowledge directory at {KNOWLEDGE_DIR}")
        return

    for filename in sorted(os.listdir(KNOWLEDGE_DIR)):
        if not filename.endswith(".txt"):
            continue

        filepath = os.path.join(KNOWLEDGE_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except (IOError, UnicodeDecodeError) as e:
            print(f"[Knowledge] Failed to read {filename}: {e}")
            continue

        if not content:
            continue

        # Derive topic name from filename: "third_lithite_resurgence.txt" -> "THIRD LITHITE RESURGENCE"
        topic = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").upper()

        # Extract keywords from the content for matching
        keywords = _extract_keywords(content)
        # Also add words from the topic name itself
        keywords.update(topic.lower().split())

        knowledge_base[topic] = {
            "content": content,
            "keywords": keywords,
            "filename": filename,
        }

    print(f"[Knowledge] Loaded {len(knowledge_base)} knowledge files: {', '.join(knowledge_base.keys())}")


def _extract_keywords(text):
    """Extract meaningful keywords from text for matching."""
    # Lowercase, split into words, filter short/common words
    words = re.findall(r'[a-zA-Z]{3,}', text.lower())

    # Common stop words to skip
    stop_words = {
        "the", "and", "was", "were", "are", "been", "being", "have", "has", "had",
        "does", "did", "will", "would", "could", "should", "may", "might", "shall",
        "can", "for", "not", "but", "with", "this", "that", "from", "they", "which",
        "their", "there", "then", "than", "these", "those", "what", "when", "where",
        "who", "whom", "how", "all", "each", "every", "both", "few", "more", "most",
        "other", "some", "such", "only", "own", "same", "into", "over", "after",
        "before", "between", "under", "again", "about", "also", "just", "very",
        "her", "his", "its", "our", "your", "she", "him", "any", "out", "too",
    }

    # Count word frequency, filter stop words
    freq = defaultdict(int)
    for word in words:
        if word not in stop_words and len(word) >= 3:
            freq[word] += 1

    # Return words that appear at least twice, plus all proper-noun-like words (capitalized in original)
    proper_nouns = set(re.findall(r'\b[A-Z][a-z]{2,}\b', text))
    keywords = {w for w, c in freq.items() if c >= 2}
    keywords.update(w.lower() for w in proper_nouns)

    return keywords


def get_relevant_knowledge(query):
    """Find knowledge sections relevant to the query. Returns formatted string or None."""
    if not knowledge_base:
        return None

    query_words = set(re.findall(r'[a-zA-Z]{3,}', query.lower()))

    if not query_words:
        return None

    # Score each knowledge section by keyword overlap
    scores = []
    for topic, data in knowledge_base.items():
        overlap = query_words & data["keywords"]
        if overlap:
            # Weight by number of matching keywords
            score = len(overlap)
            # Bonus for topic name matches (more likely to be relevant)
            topic_words = set(topic.lower().split())
            topic_overlap = query_words & topic_words
            score += len(topic_overlap) * 3
            scores.append((score, topic, data))

    if not scores:
        return None

    # Sort by score descending, take top N
    scores.sort(key=lambda x: x[0], reverse=True)
    top_sections = scores[:MAX_SECTIONS]

    # Build the knowledge context string, respecting the character limit
    parts = []
    total_chars = 0

    for score, topic, data in top_sections:
        content = data["content"]

        # Truncate if adding this would exceed limit
        remaining = MAX_CONTEXT_TOKENS - total_chars
        if remaining <= 100:
            break

        if len(content) > remaining:
            content = content[:remaining - 50] + "\n[... truncated for brevity]"

        parts.append(f"── {topic} ──\n{content}")
        total_chars += len(content) + len(topic) + 10

    if not parts:
        return None

    return "\n\n".join(parts)