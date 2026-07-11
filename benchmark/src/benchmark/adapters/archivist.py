"""Archivist adapter: drives the real dev-stack server via an injected MCP
tool-caller. Keyword == asset slug. Compaction merges via the write-engine and
writes back with memory_upsert — the real long-run behaviour under test."""
from __future__ import annotations

import json
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


def _parse_tool_result(result) -> dict:
    """Normalize an MCP CallToolResult to the dict the adapter expects.

    The prod Archivist server returns tool payloads as a JSON string inside a
    text `content` block and leaves `structuredContent` unset, so reading only
    `structuredContent` (as the first cut did) makes every read look empty —
    the adapter then never sees existing entries and degrades every write to an
    overwrite (no append, no compaction). Prefer `structuredContent` when the
    server does populate it, else parse the concatenated text content as JSON."""
    sc = getattr(result, "structuredContent", None)
    if sc:
        return sc
    text = "".join(getattr(b, "text", "") for b in (getattr(result, "content", None) or []))
    if text.strip():
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {"result": data}
    return {}


def _is_rate_limited(exc: BaseException) -> bool:
    """True if `exc` (or anything nested in it) is the server's 429/rate-limit.

    A 429 from prod arrives wrapped: httpx `HTTPStatusError` re-raised through
    anyio's `ExceptionGroup` during the streamable-HTTP session teardown. Walk
    sub-exceptions and causes so we recognise it wherever it hides."""
    seen: list[str] = []

    def walk(e: BaseException) -> None:
        seen.append(str(e))
        for sub in getattr(e, "exceptions", None) or []:
            walk(sub)
        if e.__cause__ is not None:
            walk(e.__cause__)

    walk(exc)
    blob = " ".join(seen)
    return "429" in blob or "Too Many Requests" in blob or "RATE_LIMITED" in blob


def make_mcp_caller(base_url: str, api_key: str, *, max_rpm: int | None = None):
    """Build a real Streamable-HTTP tool-caller bound to a persistent session.

    Returns (call_tool, aclose). Integration-only: exercised by run.py against
    the real server, not by unit tests (JSON-normalization via `_parse_tool_result`
    and 429-detection via `_is_rate_limited` are unit-tested).

    The prod server rate-limits per key (default 120/min). The benchmark's
    checkpoint retrieve-storm bursts far above that, so this caller (a) throttles
    to a safe rate (env `ARCHIVIST_MAX_RPM`, default 90) with a min-interval gate,
    and (b) on a 429 tears down the corrupted session, backs off, and retries on a
    fresh one — a single 429 must not abort a multi-hour sweep."""
    import asyncio
    import os
    import time
    from contextlib import AsyncExitStack

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    if max_rpm is None:
        max_rpm = int(os.environ.get("ARCHIVIST_MAX_RPM", "90"))
    min_interval = 60.0 / max_rpm if max_rpm and max_rpm > 0 else 0.0

    state: dict = {"stack": None, "session": None, "next_at": 0.0}
    gate = asyncio.Lock()

    async def _ensure() -> ClientSession:
        if state["session"] is None:
            stack = AsyncExitStack()
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(base_url, headers={"Authorization": f"Bearer {api_key}"})
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            state["stack"], state["session"] = stack, session
        return state["session"]

    async def _drop_session() -> None:
        stack = state["stack"]
        state["stack"], state["session"] = None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass  # the session is already broken; teardown errors are moot

    async def _throttle() -> None:
        if min_interval <= 0:
            return
        async with gate:
            now = time.monotonic()
            wait = state["next_at"] - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            state["next_at"] = now + min_interval

    async def call_tool(name: str, args: dict) -> dict:
        for attempt in range(4):
            await _throttle()
            try:
                session = await _ensure()
                result = await session.call_tool(name, args)
                return _parse_tool_result(result)
            except Exception as e:  # noqa: BLE001 — classify then re-raise
                if _is_rate_limited(e) and attempt < 3:
                    await _drop_session()
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
                    continue
                raise
        raise RuntimeError("unreachable")

    async def aclose() -> None:
        await _drop_session()

    return call_tool, aclose
