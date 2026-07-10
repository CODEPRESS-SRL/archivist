"""Common interface every memory system implements for the benchmark."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from benchmark.types import CompactResult, FootprintSnapshot, News, RetrieveResult, WriteResult


@runtime_checkable
class MemoryAdapter(Protocol):
    name: str

    async def reset(self) -> None: ...
    async def write(self, news: News) -> WriteResult: ...
    async def maybe_compact(self, asset: str) -> CompactResult | None: ...
    async def context_footprint(self) -> FootprintSnapshot: ...
    async def retrieve(self, query: str) -> RetrieveResult: ...
