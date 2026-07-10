"""Archivist adapter: drives the real dev-stack server via an injected MCP
tool-caller. Keyword == asset slug. Compaction merges via the write-engine and
writes back with memory_upsert — the real long-run behaviour under test."""
from __future__ import annotations

from typing import Awaitable, Callable

from benchmark.tokens import count_tokens
from benchmark.types import CompactResult, FootprintSnapshot, News, RetrieveResult, WriteResult

ToolCaller = Callable[[str, dict], Awaitable[dict]]


class ArchivistAdapter:
    name = "archivist"

    def __init__(self, call_tool: ToolCaller, engine, project: str) -> None:
        self._call = call_tool
        self._engine = engine
        self._project = project
        self._compaction_due: dict[str, bool] = {}

    async def reset(self) -> None:
        listing = await self._call("memory_list", {"project": self._project})
        for e in listing.get("entries", []):
            await self._call("memory_delete", {"project": self._project, "keyword": e["keyword"]})
        self._compaction_due.clear()

    async def write(self, news: News) -> WriteResult:
        kw = news.asset
        fragment, llm = await self._engine.refine_fragment(news)
        got = await self._call("memory_get", {"project": self._project, "keywords": kw})
        exists = bool(got.get("entries"))
        if not exists:
            resp = await self._call(
                "memory_upsert", {"project": self._project, "keyword": kw, "content": fragment}
            )
        else:
            resp = await self._call(
                "memory_append", {"project": self._project, "keyword": kw, "fragment": fragment}
            )
        self._compaction_due[kw] = bool(resp.get("compaction_due"))
        return WriteResult(llm_tokens=llm)

    async def maybe_compact(self, asset: str) -> CompactResult | None:
        if not self._compaction_due.get(asset):
            return None
        got = await self._call("memory_get", {"project": self._project, "keywords": asset})
        entry = (got.get("entries") or [{}])[0]
        fragments = [f["content"] for f in entry.get("fragments", [])]
        current = entry.get("content", "")
        merged, llm = await self._engine.merge(asset, current, fragments)
        await self._call(
            "memory_upsert",
            {
                "project": self._project,
                "keyword": asset,
                "content": merged,
                "expected_version": entry.get("version"),
                "last_fragment_id": entry.get("last_fragment_id"),
            },
        )
        self._compaction_due[asset] = False
        return CompactResult(llm_tokens=llm, merged=True)

    async def context_footprint(self) -> FootprintSnapshot:
        listing = await self._call("memory_list", {"project": self._project})
        entries = listing.get("entries", [])
        total = sum(int(e.get("token_count", 0)) for e in entries)
        index_text = "\n".join(str(e.get("keyword", "")) for e in entries)
        total += count_tokens(index_text)
        return FootprintSnapshot(startup_tokens=total, detail={"entries": len(entries)})

    async def retrieve(self, query: str) -> RetrieveResult:
        search = await self._call(
            "memory_search", {"project": self._project, "query": query, "intent": "read"}
        )
        keywords = [r["keyword"] for r in search.get("results", [])]
        if not keywords:
            return RetrieveResult(payload="", tokens=0)
        got = await self._call("memory_get", {"project": self._project, "keywords": keywords})
        parts: list[str] = []
        for e in got.get("entries", []):
            if e.get("content"):
                parts.append(e["content"])
            parts.extend(f["content"] for f in e.get("fragments", []))
        payload = "\n---\n".join(parts)
        return RetrieveResult(payload=payload, tokens=count_tokens(payload))


def make_mcp_caller(base_url: str, api_key: str):
    """Build a real Streamable-HTTP tool-caller bound to a persistent session.

    Returns (call_tool, aclose). Integration-only: exercised by run.py against
    `make up`, not by unit tests.
    """
    from contextlib import AsyncExitStack

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    stack = AsyncExitStack()
    state: dict = {}

    async def _ensure() -> ClientSession:
        if "session" not in state:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(base_url, headers={"Authorization": f"Bearer {api_key}"})
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            state["session"] = session
        return state["session"]

    async def call_tool(name: str, args: dict) -> dict:
        session = await _ensure()
        result = await session.call_tool(name, args)
        return result.structuredContent or {}

    async def aclose() -> None:
        await stack.aclose()

    return call_tool, aclose
