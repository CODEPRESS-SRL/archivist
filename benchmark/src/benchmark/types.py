"""Shared value types for the benchmark harness."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fact:
    """A structured, ground-truth fact extracted from a news item."""
    type: str            # e.g. "dividend", "guidance", "rating", "price_move", "ma"
    value: str           # the salient value, as a string ("0.24", "downgrade", ...)
    unit: str | None = None


@dataclass(frozen=True)
class News:
    """One news item for one asset on one day."""
    asset: str
    date: str            # ISO "YYYY-MM-DD"
    title: str
    body: str
    facts: tuple[Fact, ...] = ()


@dataclass
class WriteResult:
    llm_tokens: int = 0   # write-engine LLM tokens spent (0 for systems without LLM writes)


@dataclass
class CompactResult:
    llm_tokens: int = 0
    merged: bool = False


@dataclass
class FootprintSnapshot:
    startup_tokens: int          # tokens the system injects into context at session start
    detail: dict = field(default_factory=dict)   # per-source/per-key breakdown


@dataclass
class RetrieveResult:
    payload: str                 # text the system surfaces for a query
    tokens: int                  # token cost of that payload
