"""Contractions, so "i'm", "im" and "i am" find each other.

Captions and OCR write the same words three ways: with an apostrophe,
without one, and spelled out. `expand` rewrites every known contraction in
lowercase text to its spelled-out form. Search indexes both the original and
the expanded text, and expands each query term the same way.

Apostrophe-less forms that are also ordinary words ("ill", "its", "were",
"well", "wont", "hes", "lets") are left out on purpose.
"""
from __future__ import annotations

import re

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})
_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")

CONTRACTIONS = {
    "i'm": "i am", "im": "i am",
    "i've": "i have", "ive": "i have",
    "i'll": "i will",
    "i'd": "i would",
    "you're": "you are", "youre": "you are",
    "you've": "you have", "youve": "you have",
    "you'll": "you will", "youll": "you will",
    "he's": "he is",
    "she's": "she is",
    "it's": "it is",
    "that's": "that is", "thats": "that is",
    "what's": "what is", "whats": "what is",
    "there's": "there is", "theres": "there is",
    "we're": "we are",
    "they're": "they are", "theyre": "they are",
    "we've": "we have",
    "they've": "they have",
    "don't": "do not", "dont": "do not",
    "doesn't": "does not", "doesnt": "does not",
    "didn't": "did not", "didnt": "did not",
    "can't": "cannot", "cant": "cannot",
    "won't": "will not",
    "isn't": "is not", "isnt": "is not",
    "aren't": "are not", "arent": "are not",
    "wasn't": "was not", "wasnt": "was not",
    "weren't": "were not",
    "haven't": "have not", "havent": "have not",
    "hasn't": "has not", "hasnt": "has not",
    "couldn't": "could not", "couldnt": "could not",
    "shouldn't": "should not", "shouldnt": "should not",
    "wouldn't": "would not", "wouldnt": "would not",
    "ain't": "is not", "aint": "is not",
    "let's": "let us",
    "gonna": "going to",
    "wanna": "want to",
    "gotta": "got to",
}


def normalize(text: str) -> str:
    return text.lower().translate(_APOSTROPHES)


def expand(text: str) -> str:
    return _WORD.sub(lambda m: CONTRACTIONS.get(m.group(0), m.group(0)), normalize(text))


def variants(text: str) -> list[str]:
    plain = normalize(text)
    spelled = expand(plain)
    return [plain] if spelled == plain else [plain, spelled]
