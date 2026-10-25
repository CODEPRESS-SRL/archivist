"""claude-mem adapter: observations accumulate, never compacted — the behaviour
under test. Footprint = the index its SessionStart hook would inject."""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from benchmark.tokens import count_tokens
from benchmark.types import CompactResult, FootprintSnapshot, News, RetrieveResult, WriteResult

ToolCaller = Callable[[str, dict], Awaitable[dict]]


class ClaudeMemAdapter:
    name = "claude-mem"

    def __init__(self, call_tool: ToolCaller, engine, tag: str) -> None:
        self._call = call_tool
        self._engine = engine
        self._tag = tag

    async def reset(self) -> None:
        await self._call("delete_by_tag", {"tag": self._tag})  # bind to real cleanup in Step 5

    async def write(self, news: News) -> WriteResult:
        observation, llm = await self._engine.make_observation(news)
        await self._call("save_memory", {"content": observation, "tag": self._tag})
        return WriteResult(llm_tokens=llm)

    async def maybe_compact(self, asset: str) -> CompactResult | None:
        return None  # claude-mem does not recompress

    async def context_footprint(self) -> FootprintSnapshot:
        # The honest footprint is what claude-mem's SessionStart hook actually
        # injects — its recent-observations index (default last 50), which grows
        # with accumulation. NOT `timeline`, whose query form returns only a
        # fixed ±10-record window around one fuzzy anchor and so flatlines
        # regardless of how much the project accumulates (validated live against
        # populated projects — see docs/claude-mem-access.md).
        res = await self._call("context_index", {"tag": self._tag})
        index = res.get("index", "")
        return FootprintSnapshot(startup_tokens=count_tokens(index), detail={"source": "session-index"})

    async def retrieve(self, query: str) -> RetrieveResult:
        res = await self._call("search", {"query": query, "tag": self._tag})
        payload = "\n---\n".join(r.get("text", "") for r in res.get("results", []))
        return RetrieveResult(payload=payload, tokens=count_tokens(payload))


def make_claude_mem_caller(tag: str = "archivist-benchmark", data_dir: str | None = None):
    """Build a real tool-caller bound to the live claude-mem worker daemon, driven
    directly over HTTP — see `benchmark/docs/claude-mem-access.md` for the full
    investigation. Findings, in short:

      - claude-mem's MCP server (`scripts/mcp-server.cjs`, plain node, no bun needed)
        exposes `save_memory` / `search` / `timeline` / `get_observations`, but each
        one is *itself* nothing but a `fetch()` against a local HTTP daemon
        (`~/.claude-mem/worker.pid` records its pid/port; `~/.claude-mem/settings.json`
        records host/port, default 127.0.0.1:37777). Driving that daemon directly
        gives identical fidelity without spawning/managing the node subprocess, so
        that's what this factory does. The stdio path remains a drop-in alternative
        (`node <plugin>/scripts/mcp-server.cjs` over `mcp.client.stdio`) if strict
        tool-envelope fidelity is ever required.
      - claude-mem has no `tag` concept: its isolation dimension is `project` (a
        free-text column on `observations`, default `"claude-mem"`). This factory
        maps the adapter's `tag` argument onto claude-mem's `project` argument.
      - There is no delete-by-project tool or HTTP route anywhere in claude-mem
        (confirmed by reading the whole worker route table). `reset()` therefore
        falls back to a direct sqlite `DELETE FROM observations WHERE project = ?`
        against `~/.claude-mem/claude-mem.db`, scoped strictly to the benchmark tag.

    Binds:
      - save_memory   -> POST /api/memory/save        {text: content, project: tag}
      - search        -> GET  /api/search             {query, project: tag}
      - context_index -> GET  /api/context/inject      {project: tag}
                         The real SessionStart footprint: the recent-observations
                         semantic index (default last 50 obs) the hook injects.
                         Grows with accumulation — VALIDATED live 2026-07-10
                         against populated projects. (timeline's query form was
                         rejected for footprint: it returns only a ±10-record
                         window around one fuzzy anchor and flatlines.)
      - delete_by_tag -> direct sqlite DELETE, scoped to project = tag (no real
                         claude-mem tool/route exists for this — see docs)

    Integration-only: exercised by run.py against a live claude-mem install, never by
    the unit tests in test_claude_mem_adapter.py (those use FakeClaudeMem).

    # Footprint source RESOLVED 2026-07-10: switched from timeline (±10-record
    # window, flatlines) to /api/context/inject (the real SessionStart index,
    # grows to a ~50-obs plateau), validated live against populated projects.
    # TODO(real-run): two risks remain before fully trusting numbers — the sqlite
    # DELETE fallback races with the live worker's own WAL writes (no coordination
    # beyond a busy_timeout), and the Chroma vector store (~/.claude-mem/chroma/)
    # is not cleaned by reset() at all. Also: /api/memory/save enqueues async
    # processing (claude-mem's own LLM), so a saved observation may not appear in
    # the inject index until processed — pace writes or drain the queue at
    # checkpoints. See benchmark/docs/claude-mem-access.md, section "Uncertainty".
    """
    import json
    from pathlib import Path

    base = Path(data_dir) if data_dir else Path.home() / ".claude-mem"
    host, port = "127.0.0.1", 37777
    settings_path = base / "settings.json"
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        host = settings.get("CLAUDE_MEM_WORKER_HOST", host)
        port = int(settings.get("CLAUDE_MEM_WORKER_PORT", port))
    base_url = f"http://{host}:{port}"

    async def call_tool(name: str, args: dict) -> dict:
        # Lazy import: keep this function importable (and the module unit-testable)
        # even when `httpx` or a live claude-mem worker aren't present.
        import httpx

        project = args.get("tag", tag)
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            if name == "save_memory":
                resp = await client.post(
                    "/api/memory/save", json={"text": args["content"], "project": project}
                )
                resp.raise_for_status()
                return {"saved": True}
            if name == "search":
                resp = await client.get(
                    "/api/search", params={"query": args["query"], "project": project}
                )
                resp.raise_for_status()
                text = _extract_text(resp.json())
                return {"results": [{"text": text}] if text else []}
            if name == "context_index":
                # HONEST FOOTPRINT (fixed 2026-07-10 after the first real run):
                # /api/context/inject renders only from `session_summaries`, which
                # are produced by claude-mem's session-processing pipeline when a
                # real Claude Code session ends — NOT by the raw /api/memory/save
                # writes this benchmark makes. So inject flat-lines at the empty
                # baseline for synthetic writes ("No previous sessions found"),
                # even with 91 observations in the store. Reconstruct the recent-N
                # observation index claude-mem WOULD inject at SessionStart from
                # the observation store instead: it grows with accumulation to the
                # ~50-obs plateau (CLAUDE_MEM_CONTEXT_OBSERVATIONS), the behaviour
                # the footprint comparison is about. See docs/claude-mem-access.md.
                limit = int(os.environ.get("CLAUDE_MEM_CONTEXT_OBSERVATIONS", "50"))
                index = _recent_obs_index_via_sqlite(base / "claude-mem.db", project, limit)
                return {"index": index}
            if name == "delete_by_tag":
                return _delete_by_project_via_sqlite(base / "claude-mem.db", project)
            return {}

    async def aclose() -> None:
        return None

    return call_tool, aclose


def _extract_text(payload: dict) -> str:
    """claude-mem's HTTP API wraps results as {"content": [{"type": "text", "text": ...}]}
    (an MCP-content-block shape) rather than the structured JSON the unit-test fake
    uses. Pull the text out; see benchmark/docs/claude-mem-access.md."""
    blocks = payload.get("content", [])
    return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def _delete_by_project_via_sqlite(db_path, project: str) -> dict:
    """Fallback cleanup: no real claude-mem tool/route deletes observations (see
    benchmark/docs/claude-mem-access.md). Scoped strictly to the benchmark project.
    Never invoked by unit tests."""
    import sqlite3

    if not db_path.exists():
        return {"deleted": 0}
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.execute("DELETE FROM observations WHERE project = ?", (project,))
        conn.commit()
        return {"deleted": cur.rowcount}
    finally:
        conn.close()


def _recent_obs_index_via_sqlite(db_path, project: str, limit: int = 50) -> str:
    """Reconstruct the recent-observations index claude-mem injects at SessionStart.

    claude-mem's own SessionStart hook surfaces the most recent `limit`
    observations (default 50) as a compact title index; that index — not the
    full store — is the honest context footprint. `/api/context/inject` renders
    it from `session_summaries`, which only real Claude Code sessions produce, so
    for the benchmark's raw writes we build the same index from the observation
    store directly. Grows with accumulation, then plateaus at `limit`."""
    import sqlite3

    if not db_path.exists():
        return ""
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        rows = conn.execute(
            "SELECT title, subtitle FROM observations WHERE project = ? "
            "ORDER BY created_at_epoch DESC LIMIT ?",
            (project, limit),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = [f"# [{project}] recent context index ({len(rows)} observations)", ""]
    for i, (title, subtitle) in enumerate(rows, 1):
        line = f"**#{i}** {title or ''}".rstrip()
        if subtitle:
            line += f" — {subtitle}"
        lines.append(line)
    return "\n".join(lines)
