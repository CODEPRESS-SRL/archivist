# Architecture

How Archivist is built, and the design decisions behind it. For how to *use*
the memory cycle as an agent, see [concepts.md](concepts.md); for the tool
contract, see [mcp-tools.md](mcp-tools.md).

## The one-line summary

Archivist is a **remote MCP server backed by PostgreSQL**. It stores and
searches keyword-addressed memory for a team of coding agents. It runs **no
LLM**: refining and merging content is the calling agent's job. The server's
critical path is pure SQL inside a transaction.

## Components

```
                         ┌────────────────────────────────────┐
   MCP client            │  VPS (Docker Compose)              │
  (Claude Code,          │                                    │
   Codex, Cursor)  HTTPS │   ┌────────┐   ┌────────────────┐  │
        ────────────────────▶│ proxy  │──▶│  archivist-api │  │
     Streamable HTTP /mcp │   │ (TLS)  │   │  FastAPI + MCP │  │
                         │   └────────┘   └───────┬────────┘  │
                         │                        │ SQL       │
                         │                ┌───────▼────────┐  │
                         │                │  PostgreSQL 16 │  │
                         │                │  FTS + pgvector│  │
                         │                └────────────────┘  │
                         └────────────────────────────────────┘
```

| Component | Role |
|---|---|
| **proxy (TLS)** | Terminates HTTPS and reverse-proxies to the api. Either a self-contained **Caddy** (dedicated host) or an existing shared **nginx/certbot** (see [self-hosting.md](self-hosting.md)). |
| **archivist-api** | FastAPI app. Mounts the MCP server (Streamable HTTP) on `/mcp`, a Bearer-auth middleware, a `/healthz` probe, and the `/admin` inspection console. |
| **PostgreSQL 16+** | The single source of truth: memories, fragments, history, teams, keys, projects. Full-text search via a generated `tsvector` + GIN index; optional semantic search via `pgvector`. |

The api and db publish **no host ports** in production — they speak over an
internal Docker network, and only the proxy is reachable from the internet.

### Inside `archivist-api`

```
api/app/
├── main.py           # FastAPI app factory, lifespan, /healthz, mounts
├── mcp_server.py     # the 6 MCP tools + the server "instructions" (protocol)
├── auth.py           # Bearer-key middleware, team/owner contextvar, rate limit
├── config.py         # Settings (pydantic-settings, env-driven)
├── db.py             # async engine + session factory
├── models.py         # SQLAlchemy ORM models
├── log.py            # structured JSON logging (audit trail)
├── errors.py         # canonical error codes + actionable messages
├── worker.py         # async embedding backfill (hybrid search, off write path)
├── services/         # pure-SQL logic: memory.py, search.py, hybrid.py
├── embeddings/       # pluggable embedders: local (fastembed) / external
├── utils/            # keyword normalization, tiktoken token counting
└── admin/            # /admin console: auth, read views, router
```

Each MCP tool in `mcp_server.py` is a thin adapter: open a session, read the
caller's team + owner from the auth contextvar, call a pure-SQL service, and
return the JSON contract. Service rejections are mapped to actionable error
envelopes — never raised as raw exceptions to the agent.

## Data schema

```sql
teams        (id, name UNIQUE, created_at)
projects     (id, team_id, slug, created_at)            -- UNIQUE(team_id, slug)
api_keys     (id, team_id, key_hash, owner_name,
              created_at, last_used_at, revoked)
memories     (id, project_id, keyword,                  -- UNIQUE(project_id, keyword)
              content TEXT, content_tsv tsvector GENERATED,   -- GIN index
              version INT, token_count INT,
              updated_by, created_at, updated_at)
memory_fragments (id, memory_id, content, content_tsv,  -- the "event queue"
              created_by, created_at)                   -- consumed by compaction
memory_history   (id, memory_id, version, content, updated_by, created_at)
```

- **Tenant = team.** Every key, project, and (transitively) memory belongs to
  exactly one team. See [Team tenancy](#team-tenancy).
- **Namespace = project**, shared by the whole team. The server creates a
  project row on first use. The slug is derived from the git remote — see
  [concepts.md](concepts.md#project-namespacing).
- **`memory_fragments` is the append queue.** A write to an existing keyword is
  an event appended here, not a rewrite of `memories`.
- **`memory_history`** gives audit + rollback for free: one row per compaction.

## Key design decisions

### Append-only writes (no write races)

A concurrent-write conflict is impossible *by construction*, not by locking.
`memory_append` neither reads nor rewrites the base content — it inserts one
fragment row. Two teammates saving to the same keyword at the same moment both
succeed; their fragments queue in timestamp order. The only optimistic lock
(`expected_version`) is on **compaction**, where a conflict is rare and
harmless (redo the merge, lose nothing).

### Merge-on-read compaction

Fragments accumulate until someone reads the keyword and the server flags
`compaction_due=true`. That reader — who already has the content in context —
re-refines base + fragments into one dense value and writes it back. Merging is
LLM work, so it is done lazily by whoever is already paying the context cost,
never by the server.

Compaction is **atomic and idempotent**: the write consumes only fragments with
`id <= last_fragment_id` (the ones the reader actually saw). A fragment that
lands mid-merge survives to the next round. Backpressure caps the queue: past a
hard limit (10 pending fragments) `memory_append` rejects with an instruction to
compact first, so the queue can't grow unbounded when nobody reads.

### Why not a real event queue (Redis Streams + worker)?

The consumer of such a queue would have to perform the merge — which is LLM
work, forbidden server-side. It would delegate back to an agent anyway, so the
broker would degenerate into a table of pending events. `memory_fragments`
**is** that table, without the extra moving part: writes never block, ordering
is by timestamp, and consumption (compaction) is idempotent and opportunistic.

### Zero server-side LLM

Search, get, append, list, delete, and the compaction write are all pure SQL in
a transaction. This is a feature, not a limitation: no per-request model cost,
no latency spike, no provider dependency, and refine quality is governed by the
agent (iterated through the MCP `instructions`, not server code).

### Team tenancy

The archive is segregated per **team**. The invariants:

- **`team_id` comes from the authenticated key, never from a tool parameter.**
  The auth middleware resolves the Bearer key to a `(team_id, team_name)` pair
  in a request-scoped contextvar; every service call takes that `team_id`. No
  tool input can select or override a team.
- **Cross-tenant access returns `NOT_FOUND`, never `FORBIDDEN`.** A query scoped
  to `team_id` simply doesn't match another team's rows, so a foreign
  keyword/project behaves exactly as if it never existed — no existence leak.
- **`scope="all"` is still team-bounded.** Cross-project search never crosses
  the team boundary.

A key is **shared by a whole team** — there is no per-person key. `owner_name`
(the key's label) is an audit hint, not an authorization boundary. See
[SECURITY.md](../SECURITY.md).

### Hybrid retrieval (pgvector)

Lexical search (exact/prefix keyword + Postgres FTS) is always on. **Hybrid**
mode adds `pgvector` semantic search fused with the lexical ranking via
Reciprocal Rank Fusion (rank-based, `k=60`; raw scores and cosine distances are
never mixed) — see [`hybrid.py`](../api/app/services/hybrid.py). It ships **on
by default** (`ENABLE_HYBRID_SEARCH=true`); set it to `false` to fall back to
pure lexical search. The pilot's original GA gate is retired: the failure modes
it was watching for were already observed and local embeddings cost close to
nothing. Content embeddings for search are still computed **asynchronously, off
the write path** (`worker.py`), so enabling hybrid never slows down a write.
Search degrades gracefully on both sides of the KNN. Entries the worker has not
embedded yet (embedder down or backlogged) are simply invisible to the KNN list
and still ranked by the lexical retriever. And if the embedder is unavailable
at query time (e.g. missing/broken model), `hybrid_search` falls back to
pure-lexical ranking instead of erroring the whole search. Either way, un- or
mis-embedded documents remain retrievable lexically — never an error, mirroring
how `near_duplicates` degrades on write.

### Keyword embeddings and near-duplicate detection

A second, narrower use of `pgvector`: `memories.keyword_embedding` (migration
`0005`, nullable) holds a 384-dim embedding of just the *keyword* — filled
**synchronously at create** (local `fastembed`, ~5 ms, off the event loop) so
it is available immediately, with the same backfill worker catching any row
where the embedder failed or was disabled at write time. On `memory_upsert`
create, the server does a cosine-similarity lookup against other keywords in
the same project and, above `NEAR_DUPLICATE_THRESHOLD` (default 0.80), returns
them as `near_duplicates: [{keyword, similarity, preview}]`. This is purely
**advisory** — it never blocks or rejects the write (rule 3) — and is omitted
from the response entirely when there are no matches or the embedder is
unavailable. The same threshold and column back the read-only
`/admin/reports/duplicates` view (see [SECURITY.md](../SECURITY.md#admin-console-admin)).

## Request lifecycle (a write)

1. Client sends an MCP tool call over Streamable HTTP to `/mcp` with
   `Authorization: Bearer arc_…`.
2. Auth middleware hashes the key (SHA-256), looks it up, sets the
   `(team_id, team_name, owner_name)` contextvar, and enforces the per-key rate
   limit (120 req/min → `RATE_LIMITED`).
3. The tool adapter opens an async session and calls the service with `team_id`.
4. The service runs pure SQL in a transaction and returns the JSON contract (or
   raises a `ServiceError` mapped to an actionable error envelope).
5. One structured JSON audit line is logged (`tool`, `project`, `keyword`,
   `key_owner`, `duration_ms`, `outcome`).

## See also

- [concepts.md](concepts.md) — the refine → append → compaction → retrieve cycle
- [mcp-tools.md](mcp-tools.md) — the six tools, signatures, and error codes
- [self-hosting.md](self-hosting.md) — deploy, configure, administer, back up
- [../SECURITY.md](../SECURITY.md) — threat model and controls
