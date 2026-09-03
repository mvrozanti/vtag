"""Optional boolean search grammar for vtag.

Plain queries with no operators are handled by the caller as a simple
substring test. When a query contains the uppercase operators AND / OR / NOT
or parentheses, `compile_query` turns it into a predicate over a (already
lowercased) haystack string.

Grammar (precedence: NOT > AND > OR, implicit AND between adjacent terms):
    expr   := or
    or     := and (OR and)*
    and    := not (AND? not)*
    not    := NOT not | atom
    atom   := '(' expr ')' | TERM
A TERM matches when its lowercased text is a substring of the haystack.
Quote a term ("two buttons") to include spaces or a literal and/or/not.
"""
from __future__ import annotations

import re
from typing import Callable

_TOKEN_RE = re.compile(r'\s*("[^"]*"|\(|\)|[^\s()]+)')
_OPERATORS = {"AND", "OR", "NOT"}


def _tokenize(q: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(q):
        raw = m.group(1)
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            tokens.append(("TERM", raw[1:-1]))
        elif raw in ("(", ")"):
            tokens.append((raw, raw))
        elif raw in _OPERATORS:
            tokens.append((raw, raw))
        else:
            tokens.append(("TERM", raw))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.toks = tokens
        self.i = 0

    def _peek(self) -> str | None:
        return self.toks[self.i][0] if self.i < len(self.toks) else None

    def _next(self) -> tuple[str, str]:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self) -> Callable[[str], bool]:
        node = self._or()
        return node

    def _or(self) -> Callable[[str], bool]:
        left = self._and()
        while self._peek() == "OR":
            self._next()
            right = self._and()
            left = (lambda a, b: (lambda h: a(h) or b(h)))(left, right)
        return left

    def _and(self) -> Callable[[str], bool]:
        left = self._not()
        while True:
            nxt = self._peek()
            if nxt == "AND":
                self._next()
                right = self._not()
            elif nxt in ("TERM", "NOT", "("):
                right = self._not()
            else:
                break
            left = (lambda a, b: (lambda h: a(h) and b(h)))(left, right)
        return left

    def _not(self) -> Callable[[str], bool]:
        if self._peek() == "NOT":
            self._next()
            operand = self._not()
            return (lambda a: (lambda h: not a(h)))(operand)
        return self._atom()

    def _atom(self) -> Callable[[str], bool]:
        kind = self._peek()
        if kind == "(":
            self._next()
            inner = self._or()
            if self._peek() == ")":
                self._next()
            return inner
        if kind == "TERM":
            _, term = self._next()
            needle = term.lower()
            return (lambda n: (lambda h: n in h))(needle)
        # Unexpected/empty: match nothing rather than raising.
        self.i += 1
        return lambda h: False


def compile_query(q: str) -> Callable[[str], bool]:
    """Return predicate(haystack_lowercased) -> bool for a boolean query."""
    tokens = _tokenize(q)
    if not tokens:
        return lambda h: True
    return _Parser(tokens).parse()
