"""fzf-style matching for filenames.

`squash` drops every separator so "pepe_frog", "pepe-frog", "Pepe Frog" and
"pepefrog" compare equal. `score` is fzf's v1 algorithm: the pattern must
appear as a subsequence; the tightest window is scored with bonuses for word
starts, camelCase humps and consecutive runs, and penalties for gaps.
"""
from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[^\w\x1f]+|_+")
FIELD_BREAK = "\x1f"

SCORE_MATCH = 16
SCORE_GAP_START = -3
SCORE_GAP_EXTENSION = -1
BONUS_BOUNDARY = SCORE_MATCH // 2
BONUS_NON_WORD = SCORE_MATCH // 2
BONUS_CAMEL = BONUS_BOUNDARY + SCORE_GAP_EXTENSION
BONUS_CONSECUTIVE = -(SCORE_GAP_START + SCORE_GAP_EXTENSION)
BONUS_FIRST_CHAR_MULTIPLIER = 2

MIN_PATTERN = 4
MIN_QUALITY = 18.0
MAX_SPAN_RATIO = 2

_NON_WORD, _LOWER, _UPPER, _DIGIT = range(4)


def squash(text: str) -> str:
    return _SEPARATORS.sub("", text.lower())


def eligible(pattern: str) -> bool:
    return len(pattern) >= MIN_PATTERN and any(ch.isalpha() for ch in pattern)


def _char_class(ch: str) -> int:
    if ch.isdigit():
        return _DIGIT
    if ch.isupper():
        return _UPPER
    if ch.isalpha():
        return _LOWER
    return _NON_WORD


def _bonus(prev: int, cur: int) -> int:
    if prev == _NON_WORD and cur != _NON_WORD:
        return BONUS_BOUNDARY
    if (prev == _LOWER and cur == _UPPER) or (prev != _DIGIT and cur == _DIGIT):
        return BONUS_CAMEL
    if cur == _NON_WORD:
        return BONUS_NON_WORD
    return 0


def _window(pattern: str, text: str) -> tuple[int, int] | None:
    pi, start = 0, -1
    for i, ch in enumerate(text):
        if ch == pattern[pi]:
            if start < 0:
                start = i
            pi += 1
            if pi == len(pattern):
                end = i + 1
                break
    else:
        return None
    pi = len(pattern) - 1
    for i in range(end - 1, start - 1, -1):
        if text[i] == pattern[pi]:
            pi -= 1
            if pi < 0:
                return i, end
    return start, end


def score(pattern: str, text: str) -> int | None:
    if not pattern:
        return 0
    lowered = text.lower()
    found = _window(pattern, lowered)
    if found is None:
        return None
    start, end = found
    total = 0
    pi = 0
    consecutive = 0
    first_bonus = 0
    in_gap = False
    prev = _NON_WORD if start == 0 else _char_class(text[start - 1])
    for i in range(start, end):
        cur = _char_class(text[i])
        if pi < len(pattern) and lowered[i] == pattern[pi]:
            total += SCORE_MATCH
            bonus = _bonus(prev, cur)
            if consecutive == 0:
                first_bonus = bonus
            else:
                if bonus >= BONUS_BOUNDARY and bonus > first_bonus:
                    first_bonus = bonus
                bonus = max(bonus, first_bonus, BONUS_CONSECUTIVE)
            total += bonus * BONUS_FIRST_CHAR_MULTIPLIER if pi == 0 else bonus
            consecutive += 1
            in_gap = False
            pi += 1
        else:
            total += SCORE_GAP_EXTENSION if in_gap else SCORE_GAP_START
            in_gap = True
            consecutive = 0
            first_bonus = 0
        prev = cur
    return total


def quality(pattern: str, text: str) -> float | None:
    lowered = text.lower()
    pos = -1
    for ch in pattern:
        pos = lowered.find(ch, pos + 1)
        if pos < 0:
            return None
    found = _window(pattern, lowered)
    if found is None or found[1] - found[0] > MAX_SPAN_RATIO * len(pattern):
        return None
    total = score(pattern, text)
    if total is None:
        return None
    per_char = total / len(pattern)
    return per_char if per_char >= MIN_QUALITY else None
