"""
Turns a raw lot title into a 0-10 score for a given profile+term.

Two layers, combined:
  1. Rule-based: how close the lot's quantity is to what you asked for,
     nudged by a short list of condition words. Works with zero feedback.
  2. Learned: per profile+term word weights, built up from total thumbs
     up/down clicks over time. Starts at zero effect and grows as used.
"""

from __future__ import annotations

import re

# --- quantity extraction -----------------------------------------------

# Checked in order - most specific/reliable patterns first.
_QUANTITY_PATTERNS = [
    re.compile(r"\bset of (\d+)\b", re.I),
    re.compile(r"\blot of (\d+)\b", re.I),
    re.compile(r"\bqty[:\s]*(\d+)\b", re.I),
    re.compile(r"\((\d+)\)"),
    re.compile(r"\bx\s?(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:pc|pcs|piece|pieces|pack)\b", re.I),
    re.compile(r"^(\d+)\s"),  # titles that just start with a count
]
_WORD_QUANTITIES = {
    "single": 1,
    "pair": 2,
    "couple": 2,
    "trio": 3,
    "triple": 3,
}


def extract_quantity(title: str) -> int:
    """Best-effort guess at how many items are in this lot. Defaults to 1,
    since most auction titles for a single item never mention a count."""
    if not title:
        return 1
    for pattern in _QUANTITY_PATTERNS:
        match = pattern.search(title)
        if match:
            try:
                return max(1, int(match.group(1)))
            except ValueError:
                continue
    lowered = title.lower()
    for word, qty in _WORD_QUANTITIES.items():
        if re.search(rf"\b{word}\b", lowered):
            return qty
    return 1


# --- rule-based condition words ------------------------------------------

_POSITIVE_WORDS = {
    "new", "sealed", "unused", "nib", "matching", "complete", "unopened",
}
_NEGATIVE_WORDS = {
    "damaged", "broken", "chipped", "cracked", "stained", "scratched",
    "missing", "incomplete", "as-is", "asis", "parts", "repair", "worn",
}

_QUANTITY_SCORE_TABLE = {0: 10.0, 1: 7.0, 2: 5.0}
_QUANTITY_SCORE_FLOOR = 3.0


def _quantity_score(target: int | None, found: int) -> float:
    if not target:
        return 8.0  # no preference stated - stay neutral, not penalized
    diff = abs(target - found)
    return _QUANTITY_SCORE_TABLE.get(diff, _QUANTITY_SCORE_FLOOR)


def _tokenize(title: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (title or "").lower())


def rule_score(title: str, target_quantity: int | None) -> tuple[float, int]:
    """Returns (score, quantity_found) using only fixed rules - no
    per-user learning involved."""
    found_qty = extract_quantity(title)
    score = _quantity_score(target_quantity, found_qty)

    words = set(_tokenize(title))
    score += 1.0 * len(words & _POSITIVE_WORDS)
    score -= 1.5 * len(words & _NEGATIVE_WORDS)

    return max(0.0, min(10.0, score)), found_qty


# --- learned adjustment ---------------------------------------------------

LEARNING_RATE = 0.5
MAX_LEARNED_ADJUSTMENT = 2.0


def learned_adjustment(title: str, word_weights: dict[str, float]) -> float:
    """Small nudge based on words this profile has historically liked/
    disliked in titles for this term. Zero until there's feedback."""
    if not word_weights:
        return 0.0
    words = _tokenize(title)
    if not words:
        return 0.0
    total = sum(word_weights.get(w, 0.0) for w in words)
    avg = total / len(words)
    return max(-MAX_LEARNED_ADJUSTMENT, min(MAX_LEARNED_ADJUSTMENT, avg))


def score_lot(title: str, target_quantity: int | None, word_weights: dict[str, float]) -> tuple[float, int]:
    """Final 0-10 score plus the extracted quantity, for storage/display."""
    base, found_qty = rule_score(title, target_quantity)
    final = base + learned_adjustment(title, word_weights)
    return max(0.0, min(10.0, round(final, 1))), found_qty


def apply_feedback_to_weights(title: str, liked: bool) -> dict[str, float]:
    """Returns {word: delta} to apply on top of existing weights for
    every meaningful word in the title. Call db.bump_word_weight for each."""
    delta = LEARNING_RATE if liked else -LEARNING_RATE
    words = set(_tokenize(title))
    # Skip extremely common/short tokens that carry no signal.
    stopwords = {"the", "a", "an", "for", "with", "and", "of", "in", "on", "to"}
    return {w: delta for w in words if w not in stopwords and len(w) > 2}
