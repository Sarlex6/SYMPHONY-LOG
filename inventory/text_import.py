"""Turn a hand-written text log into cart entries. EXPERIMENTAL.

Input is what people paste from their notes:

    Iron Ore 25
    copper-wire 10, Med Kit x3
    5 rubies

An entry is an item name followed by a quantity. Entries are separated by line
breaks, or just by whitespace: a quantity ends the entry before it. Names can
be several words long.

Matching, cheapest first, so most entries never reach the expensive part:

  1. exact, after normalising: case, diacritics, and every space, dash and
     punctuation mark ignored. "copper-wire", "Copper Wire" and "copperwire"
     are the same key.
  2. the same, with the words in any order.
  3. fuzzy: a small number of typos (insertions, deletions, substitutions,
     swapped neighbours), scaled to the length of the name.
  4. partial: every word typed is a word of the item ("iron" for "Iron Ore").

A guess that is not exact is reported back for the member to check, and a
name close to two items equally is refused rather than guessed. Pure Python
with no extra libraries: the server is small, and with a few hundred items
this is milliseconds per entry. A deadline bounds it anyway.

Additions only. Removing stock still goes through the normal menu.
"""

import re
import time
import unicodedata

#: Hard cap on how long one import may spend matching, in seconds. Whatever is
#: left when it runs out is reported as not processed instead of being waited on.
DEFAULT_TIME_BUDGET = 15.0

#: Lines read from one import. A Discord text box holds 4000 characters, so this
#: is generous; it only stops pathological input.
MAX_LINES = 200

MAX_QUANTITY = 1_000_000

#: Accept a match at or above this score...
ACCEPT_SCORE = 0.66
#: ...unless another item scores within this much of it, in which case the name
#: is ambiguous and nothing is guessed.
AMBIGUITY_MARGIN = 0.05

#: Words people put around quantities that are never part of an item name.
_FILLER = {"x", "×", "pcs", "pc", "pieces", "piece", "units", "unit", "qty",
           "amount", "-", "–", "—", "+"}

#: "25", "x25", "25x", "×25", "25pcs", "-25", "+25". A sign in front is read as a
#: separator ("Iron -25"), not as a removal: imports only add.
_QUANTITY = re.compile(
    r"^[+\-–—]?(?:[x×*])?(\d{1,7})(?:x|×|pcs?|pieces?|units?)?$",
    re.IGNORECASE,
)

#: List markers at the start of a line: "1.", "2)", "-", "*", "•", ">".
_LIST_MARKER = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*•·>]+)\s+")

#: Punctuation that only ever separates things. Dashes and dots are left alone
#: here because they also occur inside names ("Med-Kit", "5.56 Ammo"); a dash
#: standing on its own is dropped as filler instead.
_SEPARATORS = re.compile(r"[\[\](){}<>,;:|=•·\"“”'`]")

#: Letters NFKD does not decompose into a base letter plus an accent.
_LETTERS = str.maketrans({
    "ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ð": "d",
    "þ": "th", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ı": "i",
})


# ── Normalisation ───────────────────────────────────────────────────────────

def _plain(text):
    """Lower case, no diacritics."""
    text = unicodedata.normalize("NFKD", str(text).translate(_LETTERS))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _words(text):
    return re.findall(r"[a-z0-9]+", _plain(text))


def _compact(words):
    return "".join(words)


def _singular(word):
    """Crude plural folding, applied to both sides alike, so "rubies" meets
    "Ruby" and "bandage" meets "Bandages". Over-folding ("lens" -> "len") is
    harmless for the same reason."""
    if word.isdigit() or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _loose(words):
    return [_singular(word) for word in words]


# ── Fuzzy distance ──────────────────────────────────────────────────────────

def _distance(a, b, limit):
    """Optimal-string-alignment distance (Levenshtein plus swapped
    neighbours), giving up as soon as it must exceed `limit`."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1

    previous2 = None
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        row_min = i
        a_char = a[i - 1]
        for j in range(1, len(b) + 1):
            cost = 0 if a_char == b[j - 1] else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if (i > 1 and j > 1 and a_char == b[j - 2] and a[i - 2] == b[j - 1]):
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
            if value < row_min:
                row_min = value
        if row_min > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


def _allowed_typos(length):
    """How many typos a name of this many letters may carry and still match."""
    if length < 4:
        return 0
    if length <= 6:
        return 1
    if length <= 12:
        return 2
    return 3


# ── The item index ──────────────────────────────────────────────────────────

class _Item:
    __slots__ = ("item", "key", "loose_key", "sorted_key", "word_set")

    def __init__(self, item):
        self.item = item
        words = _words(item["name"])
        loose = _loose(words)
        self.key = _compact(words)
        self.loose_key = _compact(loose)
        self.sorted_key = _compact(sorted(loose))
        self.word_set = set(loose)


class ItemIndex:
    """Every item from every register, prepared for matching."""

    def __init__(self, items):
        self.items = [_Item(item) for item in items if str(item.get("name", "")).strip()]
        self.items = [entry for entry in self.items if entry.key]
        self.by_key, self.by_loose_key, self.by_sorted_key = {}, {}, {}
        for entry in self.items:
            self.by_key.setdefault(entry.key, []).append(entry)
            self.by_loose_key.setdefault(entry.loose_key, []).append(entry)
            self.by_sorted_key.setdefault(entry.sorted_key, []).append(entry)

    def candidates(self, name):
        """[(score, _Item)] for a typed name, best first. Empty if nothing is close."""
        words = _words(name)
        key = _compact(words)
        if not key:
            return []

        # Exact, ignoring case, accents, spaces and punctuation.
        found = self.by_key.get(key)
        if found:
            return [(1.0, entry) for entry in found]

        loose = _loose(words)
        loose_key = _compact(loose)
        found = self.by_loose_key.get(loose_key)
        if found:
            return [(0.97, entry) for entry in found]          # plural / singular

        sorted_key = _compact(sorted(loose))
        found = self.by_sorted_key.get(sorted_key)
        if found:
            return [(0.95, entry) for entry in found]          # words reordered

        # Only names with no exact match get here, so the per-item work below
        # is paid for the typos alone.
        word_set = set(loose)
        scored = []
        for entry in self.items:
            score = 0.0

            limit = _allowed_typos(max(len(loose_key), len(entry.loose_key)))
            if limit:
                typos = min(
                    _distance(loose_key, entry.loose_key, limit),
                    _distance(sorted_key, entry.sorted_key, limit),
                )
                if typos <= limit:
                    score = 0.9 - 0.08 * typos

            # Every word typed is a whole word of the item: an abbreviated name.
            if word_set < entry.word_set:
                coverage = len(loose_key) / len(entry.loose_key)
                score = max(score, 0.6 + 0.25 * coverage)

            if score:
                scored.append((score, entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored


# ── Parsing ─────────────────────────────────────────────────────────────────

def _quantity(token):
    match = _QUANTITY.match(token)
    return int(match.group(1)) if match else None


def _tokens(line):
    line = _LIST_MARKER.sub("", line)
    line = _SEPARATORS.sub(" ", line)
    return line.split()


def split_entries(text):
    """Text -> ([(raw_name, quantity)], [problem text]).

    A quantity closes the entry in front of it. Two quantities in a row mean
    the first belongs to the name ("Glock 17 5"). A line holding only a name
    takes its quantity from the start of the next line. A quantity before the
    name ("5 Iron Ore") is accepted when nothing follows the name.
    """
    entries, problems = [], []
    lines = str(text or "").splitlines()
    if len(lines) > MAX_LINES:
        problems.append(f"only the first {MAX_LINES} lines were read")
        lines = lines[:MAX_LINES]

    carry = []   # name tokens still waiting for a quantity
    for line in lines:
        tokens = carry + _tokens(line)
        carry = []
        name, leading = [], None

        for index, token in enumerate(tokens):
            quantity = _quantity(token)
            if quantity is None:
                if token.casefold() not in _FILLER:
                    name.append(token)
                continue

            following = _quantity(tokens[index + 1]) if index + 1 < len(tokens) else None
            if name and following is not None and token.isdigit():
                name.append(token)                      # part of the name
            elif name:
                entries.append((" ".join(name), quantity))
                name = []
                leading = None
            elif leading is None:
                leading = quantity                      # "5 Iron Ore"
            else:
                problems.append(f"`{token}` — a quantity with no item name")

        if name and leading is not None:
            entries.append((" ".join(name), leading))
        elif name:
            carry = name                                # quantity may be on the next line
        elif leading is not None:
            problems.append(f"`{leading}` — a quantity with no item name")

    if carry:
        problems.append(f"`{' '.join(carry)}` — no quantity given")
    return entries, problems


# ── The whole import ────────────────────────────────────────────────────────

class ImportResult:
    def __init__(self):
        #: [(item dict, quantity, typed name, score)] — one per distinct item
        self.matched = []
        #: [(typed name, quantity)] — nothing close enough
        self.unrecognized = []
        #: [(typed name, quantity, [item dicts])] — too close to several items
        self.ambiguous = []
        #: problems with the text itself (missing quantities, zero amounts ...)
        self.problems = []
        #: entries left unread because the time budget ran out
        self.unprocessed = 0


def import_log(text, items, time_budget=DEFAULT_TIME_BUDGET):
    """Match a text log against `items` (dicts with at least "name").

    Blocking and CPU-bound: run it in a worker thread.
    """
    deadline = time.monotonic() + time_budget
    result = ImportResult()

    entries, result.problems = split_entries(text)
    index = ItemIndex(items)

    merged = {}   # id(item) -> position in result.matched
    for position, (name, quantity) in enumerate(entries):
        if time.monotonic() > deadline:
            result.unprocessed = len(entries) - position
            break

        if quantity <= 0:
            result.problems.append(f"`{name}` — quantity is 0")
            continue
        if quantity > MAX_QUANTITY:
            result.problems.append(f"`{name} {quantity}` — quantity is too large")
            continue

        candidates = index.candidates(name)
        if not candidates or candidates[0][0] < ACCEPT_SCORE:
            result.unrecognized.append((name, quantity))
            continue

        best_score, best = candidates[0]
        rivals = [
            entry for score, entry in candidates[1:]
            if best_score - score < AMBIGUITY_MARGIN
        ]
        if rivals:
            result.ambiguous.append(
                (name, quantity, [best.item] + [entry.item for entry in rivals[:3]])
            )
            continue

        # The same item named twice becomes one entry with the total.
        key = id(best.item)
        if key in merged:
            item, total, typed, score = result.matched[merged[key]]
            result.matched[merged[key]] = (item, total + quantity, typed, min(score, best_score))
        else:
            merged[key] = len(result.matched)
            result.matched.append((best.item, quantity, name, best_score))

    return result
