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


def _extract_rpc_result(resp) -> dict:
    """Pull the tool payload out of a JSON-RPC-over-HTTP response and normalize it.

    The stateless server answers a `tools/call` POST with either a single JSON
    object or (usually) a `text/event-stream` body carrying one `data:` line. In
    both cases the JSON-RPC envelope's `result` holds the tool output, which we
    hand to `_parse_tool_result` (structuredContent, else the JSON text block)."""
    import types

    ct = resp.headers.get("content-type", "")
    msg = None
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[len("data:"):].strip())
                break
    elif resp.text.strip():
        msg = resp.json()
    if not msg:
        return {}
    if "error" in msg:
        raise RuntimeError(f"MCP error: {msg['error']}")
    result = msg.get("result", {}) or {}
    shim = types.SimpleNamespace(
        structuredContent=result.get("structuredContent"),
        content=[types.SimpleNamespace(text=b.get("text", ""))
                 for b in result.get("content", []) if isinstance(b, dict)],
    )
    return _parse_tool_result(shim)


_MAX_ATTEMPTS = 5
_RATE_LIMIT_BACKOFF_S = 60.0   # one full fixed-window minute


def make_mcp_caller(base_url: str, api_key: str, *, max_rpm: int | None = None,
                    _client=None, _sleep=None, _now=None):
    """Build a tool-caller over the server's **stateless** Streamable-HTTP endpoint.

    Returns (call_tool, aclose). The server is `FastMCP(stateless_http=True)`, so
    every `tools/call` is an independent HTTP POST (no session-id, no `initialize`
    handshake, no persistent SSE stream). Doing exactly that with a plain
    `httpx.AsyncClient` sidesteps the whole failure class a long-lived MCP session
    hit over a multi-hour run: a reverse-proxy idle-drop of the SSE stream, and the
    anyio cancel-scope debris left when such a session is closed. What remains is
    ordinary HTTP, so resilience is simple: throttle under the per-key rate limit
    (env `ARCHIVIST_MAX_RPM`, default 90) and back off on 429 / 5xx / transport
    errors. `_client`/`_sleep`/`_now` are unit-test seams."""
    import asyncio
    import os
    import time

    if max_rpm is None:
        max_rpm = int(os.environ.get("ARCHIVIST_MAX_RPM", "90"))
    min_interval = 60.0 / max_rpm if max_rpm and max_rpm > 0 else 0.0
    sleep = _sleep or asyncio.sleep
    now_fn = _now or time.monotonic

    # Post to the CANONICAL trailing-slash URL. The server mounts the MCP app at
    # `/mcp` with streamable_http_path="/", so `/mcp` 307-redirects to `/mcp/`.
    # Following that redirect would send TWO requests per call, and the fixed-window
    # rate limiter counts both — silently halving the usable request budget.
    url = base_url if base_url.endswith("/") else base_url + "/"

    if _client is None:
        import httpx
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True)
    else:
        client = _client

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    state = {"next_at": 0.0, "id": 0}
    gate = asyncio.Lock()

    async def _throttle() -> None:
        if min_interval <= 0:
            return
        async with gate:
            wait = state["next_at"] - now_fn()
            if wait > 0:
                await sleep(wait)
            state["next_at"] = now_fn() + min_interval

    async def call_tool(name: str, args: dict) -> dict:
        import httpx
        for attempt in range(_MAX_ATTEMPTS):
            await _throttle()
            state["id"] += 1
            body = {"jsonrpc": "2.0", "id": state["id"], "method": "tools/call",
                    "params": {"name": name, "arguments": args}}
            last = attempt >= _MAX_ATTEMPTS - 1
            try:
                resp = await client.post(url, headers=headers, json=body)
            except httpx.HTTPError:      # transport error (connect/read/timeout)
                if last:
                    raise
                await sleep(2 ** attempt)
                continue
            if resp.status_code == 429 and not last:
                # Fixed-window per-minute limiter that counts rejected requests too,
                # so a short retry stays in the saturated window. Wait a full window
                # for it to reset. (With the /mcp/ fix this should never trigger.)
                await sleep(_RATE_LIMIT_BACKOFF_S)
                continue
            if resp.status_code >= 500 and not last:
                await sleep(2 ** attempt)  # transient server error
                continue
            resp.raise_for_status()
            return _extract_rpc_result(resp)
        raise RuntimeError("unreachable")

    async def aclose() -> None:
        await client.aclose()

    return call_tool, aclose
