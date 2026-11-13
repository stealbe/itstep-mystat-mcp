"""Full-text query helpers.

The SQLite FTS5 ``MATCH`` syntax is a small query language of its own: raw user
input such as ``C++ -исключение`` raises an error or silently changes meaning.
Every token is therefore turned into a quoted phrase, and FTS operators typed
by the user are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
MAX_TERMS = 16
_TOKEN_SPLIT = re.compile(r"[\s,;]+")


@dataclass(frozen=True, slots=True)
class SearchQuery:
    raw: str
    terms: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.terms

    def to_match_expression(self) -> str:
        if self.is_empty:
            return ""
        return " ".join(f'"{term}"' for term in self.terms)


def parse_query(raw: str) -> SearchQuery:
    cleaned = (raw or "").replace('"', " ").replace("*", " ").replace("(", " ").replace(")", " ")
    tokens = [token.strip() for token in _TOKEN_SPLIT.split(cleaned)]
    terms = [token for token in tokens if token and token.upper() not in FTS_OPERATORS]
    # Single-character tokens match too much with unicode61; keep them only if
    # they are the entire query.
    if len(terms) > 1:
        meaningful = [term for term in terms if len(term) > 1]
        terms = meaningful or terms
    return SearchQuery(raw=raw, terms=tuple(terms[:MAX_TERMS]))


def build_fts_query(raw: str) -> str:
    return parse_query(raw).to_match_expression()
