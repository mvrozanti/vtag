"""Boolean search grammar for vtag, scored.

Grammar (precedence: NOT > AND > OR, implicit AND between adjacent terms):
    expr   := or
    or     := and (OR and)*
    and    := not (AND? not)*
    not    := NOT not | atom
    atom   := '(' expr ')' | TERM
Quote a term ("two buttons") to include spaces or a literal and/or/not.

`compile_query(q, term_scorer)` returns a function doc -> Hit | None, where
None means no match. `term_scorer(text)` builds the per-term function. AND sums
its operands, OR keeps the best, NOT matches with score 0.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Hit:
    score: float
    fuzzy: bool = field(default=False)

    def __add__(self, other: Hit) -> Hit:
        return Hit(self.score + other.score, self.fuzzy or other.fuzzy)


Scorer = Callable[[Any], "Hit | None"]

_TOKEN_RE = re.compile(r'\s*("[^"]*"|\(|\)|[^\s()]+)')
_OPERATORS = {"AND", "OR", "NOT"}


def _tokenize(q: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(q):
        raw = m.group(1)
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            tokens.append(("TERM", raw[1:-1]))
        elif raw in ("(", ")") or raw in _OPERATORS:
            tokens.append((raw, raw))
        else:
            tokens.append(("TERM", raw))
    return tokens


def _never(_doc: Any) -> None:
    return None


def _both(a: Scorer, b: Scorer) -> Scorer:
    def run(doc: Any) -> Hit | None:
        left = a(doc)
        if left is None:
            return None
        right = b(doc)
        return None if right is None else left + right
    return run


def _either(a: Scorer, b: Scorer) -> Scorer:
    def run(doc: Any) -> Hit | None:
        left, right = a(doc), b(doc)
        if left is None:
            return right
        if right is None:
            return left
        return left if left.score >= right.score else right
    return run


def _negate(a: Scorer) -> Scorer:
    return lambda doc: Hit(0.0) if a(doc) is None else None


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], term_scorer: Callable[[str], Scorer]):
        self.toks = tokens
        self.i = 0
        self.term_scorer = term_scorer

    def _peek(self) -> str | None:
        return self.toks[self.i][0] if self.i < len(self.toks) else None

    def _next(self) -> tuple[str, str]:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self) -> Scorer:
        return self._or()

    def _or(self) -> Scorer:
        left = self._and()
        while self._peek() == "OR":
            self._next()
            left = _either(left, self._and())
        return left

    def _and(self) -> Scorer:
        left = self._not()
        while True:
            nxt = self._peek()
            if nxt == "AND":
                self._next()
            elif nxt not in ("TERM", "NOT", "("):
                break
            left = _both(left, self._not())
        return left

    def _not(self) -> Scorer:
        if self._peek() == "NOT":
            self._next()
            return _negate(self._not())
        return self._atom()

    def _atom(self) -> Scorer:
        kind = self._peek()
        if kind == "(":
            self._next()
            inner = self._or()
            if self._peek() == ")":
                self._next()
            return inner
        if kind == "TERM":
            _, term = self._next()
            return self.term_scorer(term)
        self.i += 1
        return _never


def compile_query(q: str, term_scorer: Callable[[str], Scorer]) -> Scorer:
    tokens = _tokenize(q)
    if not tokens:
        return lambda _doc: Hit(0.0)
    return _Parser(tokens, term_scorer).parse()
