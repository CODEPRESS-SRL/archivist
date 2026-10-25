# claude-mem access investigation

Read-only investigation of how a standalone Python process (the benchmark
harness) can reach the *real* claude-mem, done before wiring
`make_claude_mem_caller` in `benchmark/src/benchmark/adapters/claude_mem.py`.
No real claude-mem data was written, deleted, or modified — only `GET`
requests (health, search, timeline) and read-only sqlite/file inspection.

## What's installed

- Plugin: `thedotmack/claude-mem`, installed through the Claude Code plugin
  system. The investigation used v10.3.1.
- Data dir: the claude-mem home directory — contains:
  - `claude-mem.db` (+ `-shm`/`-wal`, WAL mode) — the real store, sqlite.
  - `chroma/` — a Chroma vector-store directory (semantic embeddings; not
    confirmed to be in the `search`/`timeline` read path — see Uncertainty).
  - `settings.json` — plain JSON config, including
    `CLAUDE_MEM_WORKER_HOST` / `CLAUDE_MEM_WORKER_PORT` (loopback host and
    the configured port).
  - `worker.pid` — `{pid, port, startedAt}` of a **long-running background
    HTTP daemon** (worker-service.cjs). It was live during this
    investigation (`GET /api/health` → `200 {"status":"ok", ...}`).

## Architecture: MCP server is a thin HTTP proxy to a local daemon

`.mcp.json` in the plugin registers one stdio MCP server:

```json
{"mcpServers": {"mcp-search": {"type": "stdio",
  "command": "${CLAUDE_PLUGIN_ROOT}/scripts/mcp-server.cjs"}}}
```

`scripts/mcp-server.cjs` (`#!/usr/bin/env node`, plain Node — no bun runtime
needed to launch it) exposes exactly 4 tools:

| MCP tool | HTTP call it makes | Params |
|---|---|---|
| `search` | `GET /api/search` | `query, project, limit, type, obs_type, dateStart, dateEnd, offset, orderBy` |
| `timeline` | `GET /api/timeline` | `anchor` **or** `query` (required — one of the two), `project, depth_before, depth_after` |
| `get_observations` | `POST /api/observations/batch` | `ids: number[]` (required) |
| `save_memory` | `POST /api/memory/save` | `text` (required), `title?`, `project?` (defaults to `"claude-mem"` if omitted) |

Every one of these is a call to `http://{WORKER_HOST}:{WORKER_PORT}` — the
same daemon whose pidfile/health-endpoint was inspected above. The MCP
stdio layer adds no logic beyond JSON-RPC framing and a tool-description
wrapper (confirmed by reading `mcp-server.cjs`'s `Ay`/`Ry` helper functions,
which are literal `fetch()` calls against `ai = http://{host}:{port}`).

**Key correction vs. the task brief's assumed shape:** claude-mem has no
`tag` concept anywhere (not in the MCP tool schemas, not as a column in
`observations`). The isolation dimension it actually has is **`project`**
(free-text, defaults to `"claude-mem"`), confirmed via `PRAGMA table_info
(observations)`:

```
id, memory_session_id, project, text, type, title, subtitle, facts,
narrative, concepts, files_read, files_modified, prompt_number,
discovery_tokens, created_at, created_at_epoch
```

So the factory maps the adapter's generic `tag` argument onto claude-mem's
`project` argument — that is the real dedicated-namespace mechanism.

## Chosen access path: HTTP directly to the worker daemon

Two paths were viable per the task's investigation callout:

1. **Stdio MCP client** driving `mcp-server.cjs` (node subprocess +
   `mcp.client.stdio`), matching the Archivist adapter's
   `make_mcp_caller` pattern.
2. **Direct HTTP** to the worker daemon the MCP server itself proxies to.

Chosen: **(2), direct HTTP**, because `mcp-server.cjs` was confirmed (by
reading its source) to add nothing but a JSON-RPC/tool-schema wrapper
around plain `fetch()` calls to the same worker HTTP API — driving the
worker directly gets identical fidelity without spawning and managing a
Node subprocess, resolving `${CLAUDE_PLUGIN_ROOT}`, or depending on the
plugin cache layout. A recent Node.js release confirms the stdio path
*would* work if strict "went through the actual MCP tool" fidelity is
required later — swapping it in is a small, isolated change (see TODO in
`make_claude_mem_caller`'s docstring).

Read-only probes performed (all safe — either non-existent
project/query or GETs with no side effects):

```
GET /api/health
  → {"status":"ok","version":"10.3.1", ...}   # worker is live
GET /api/search?project=__probe_nonexistent__&query=zzzznope
  → {"content":[{"type":"text","text":"No results found matching \"zzzznope\""}]}
GET /api/timeline?project=__probe_nonexistent__
  → {"content":[{"type":"text","text":"Error: Must provide either \"anchor\" or \"query\" parameter"}],"isError":true}
GET /api/timeline?query=__probe_nonexistent__&project=__probe_nonexistent__
  → {"content":[{"type":"text","text":"Found observation matching ..., but no timeline
      context available (10 records before, 10 records after)."}]}   # no crash
GET /api/projects
  → {"projects": [... existing project names ...]}   # confirms free-text project scoping
```

These confirm response shapes are **not** structured `{"results": [...]}`
JSON as the unit-test fake assumes — they are markdown-formatted text
blocks (`{"content":[{"type":"text","text":"<markdown>"}]}`), because
`search`/`timeline` are explicitly documented by claude-mem itself as a
*compact index* (titles + observation IDs + token estimates), not full
observation bodies — full text requires a second call to
`get_observations(ids=[...])`, which the given adapter contract does not
make (see `docs/mcp-tools.md`-equivalent workflow note embedded in the
tool's own description: "3-LAYER WORKFLOW: search → timeline → get_observations").

## Mapping implemented in `make_claude_mem_caller`

- `save_memory` (`content`, `tag`) → `POST /api/memory/save` with
  `{text: content, project: tag}`.
- `search` (`query`, `tag`) → `GET /api/search?query=...&project=...`;
  the markdown text block is wrapped as a single synthetic
  `{"results": [{"text": <markdown>}]}` to satisfy the adapter's existing
  `retrieve()` (which joins `r["text"]` for each result). This is a known
  fidelity approximation: real claude-mem's `search` output is an index,
  not full content — a fully faithful benchmark would need to add a
  `get_observations` follow-up call, which is out of scope for this
  adapter's fixed interface.
- `timeline` (`tag`) → `GET /api/timeline?query=<tag>&project=<tag>`.
  `timeline` **requires** `anchor` or `query`; the adapter contract only
  passes `tag`, so `tag` is reused as the `query` value. For an empty
  project it degrades gracefully to a short "no timeline context available"
  message (verified above), which is an acceptable footprint baseline
  (near-zero tokens). For a real (non-empty) benchmark project, it is
  ASSUMED to return the same kind of injected index the SessionStart hook
  would show, but this has NEVER been verified against populated data —
  see Uncertainty below.
- `delete_by_tag` (`tag`) → **no such tool or HTTP route exists** in
  claude-mem (confirmed: full-text search of `worker-service.cjs` for
  `DELETE FROM observations` and for any `r.delete("/api/...")` route
  touching observations found none — the only exposed deletes are for the
  pending-message queue and a `/sessions/:id` "complete" endpoint that does
  *not* delete rows). Fallback implemented: a direct sqlite
  `DELETE FROM observations WHERE project = ?` against
  the claude-mem database file, scoped strictly to the benchmark project
  name. This is the only way to satisfy `reset()`'s "remove only
  benchmark-tagged rows" requirement.

## Uncertainty / risks (flagged, not resolved)

- **UNVALIDATED — timeline-as-growing-index (blocks trusting real
  claude-mem footprint numbers):** `timeline(query=<tag>, project=<tag>)`
  was only probed against empty/nonexistent projects. It is ASSUMED to
  return the accumulating session-index (which is what `context_footprint`
  measures), but whether `query=<literal tag>` returns "everything in the
  project" vs a fuzzy search that may match nothing against real observation
  text was never verified against populated data. If it's the latter, the
  real-run footprint could flatline regardless of accumulation — silently
  defeating the growth measurement. MUST validate against a live, populated
  project before publishing claude-mem numbers.
- **Concurrent-write risk on the sqlite fallback.** The worker is a live
  daemon holding its own connection to `claude-mem.db` (WAL mode). A raw
  file-level `DELETE` from a second process is not coordinated with the
  worker and could race with it (e.g. mid-write flush, or the worker's
  own FTS triggers). Mitigated with `sqlite3.connect(..., timeout=...)`
  (busy-wait) but not further hardened — this needs validation against a
  live worker before being trusted in a real benchmark run.
  `# TODO(real-run)` in the factory flags this.
- **Chroma vector store is not touched by `reset()`.** If `search`/
  `timeline` end up consulting the Chroma embeddings (unconfirmed from
  static reading of `worker-service.cjs` alone — the route handlers were
  traced only as far as `handleUnifiedSearch`/`handleUnifiedTimeline`,
  not into their internals), stale benchmark vectors could remain after a
  sqlite-only delete. This does not break isolation of *content* returned
  (an orphaned embedding pointing at a deleted row should not resolve to
  text), but it is an unconfirmed gap.
- **`get_observations` (full-text fetch) is not wired.** The adapter's
  fixed interface (from the task brief) never calls it, so
  `make_claude_mem_caller` does not bind it either, even though it exists
  and is one of the 4 real tools. If the benchmark is later extended to
  measure full-fidelity retrieval, this is the missing piece.
- **Stdio path unexercised.** The stdio `mcp-server.cjs` route was read
  and reasoned about, not actually driven end-to-end (no live stdio
  handshake was performed, to avoid needing to reason about
  `${CLAUDE_PLUGIN_ROOT}` substitution and subprocess lifecycle during a
  read-only investigation). The HTTP path was validated live instead.

## Isolation used by the benchmark

`make_claude_mem_caller(tag="archivist-benchmark", ...)` — a project
name not present in the real project listing at investigation
time (none of the existing projects was named
`archivist-benchmark`), used everywhere the adapter's `tag` is threaded
through. `reset()` deletes only `WHERE project = 'archivist-benchmark'`.
