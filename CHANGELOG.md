# Changelog

All notable changes to Archivist are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Three-tier entry size model.** The compaction/sweep trigger is decoupled
  from the 2 000-token soft cap. An entry is now flagged `compaction_due` (and
  swept by the companion) only when it exceeds the new **8 000-token target**
  (`ENTRY_TOKEN_TARGET`) or accumulates more than `COMPACTION_FRAGMENT_THRESHOLD`
  fragments. The 2 000-token soft cap still emits the `soft_cap_exceeded` write
  warning; the 10 000-token hard cap still rejects. Entries in the 2k–8k band
  are an acceptable size and no longer churn as perpetually `compaction_due`.
  The companion merge/refine prompt compresses toward ~2 000 tokens with the
  8 000 ceiling, exceeded only for genuinely irreducible content.

### Removed

- **`COMPACTION_TOKEN_RATIO`** setting (was `0.66`), superseded by the explicit
  `ENTRY_TOKEN_TARGET`.

## [1.1.0] — 2026-07-04

Gap-closure pass on the v1 MVP: hybrid search graduates to the default, and
three small freshness/dedup features close loops the pilot surfaced.

### Added

- **Hybrid retrieval, on by default.** `ENABLE_HYBRID_SEARCH` now defaults to
  `true` (was the prototype opt-in); set it to `false` to fall back to pure
  lexical search. The pilot's GA gate is retired — the failure modes it was
  watching for were already observed in dogfooding, and local embeddings cost
  close to nothing. Embeddings are still computed asynchronously, off the
  write path.
- **`keyword_embedding` + `near_duplicates`** (migration `0005`): a per-entry
  keyword embedding, filled synchronously (~5 ms, local `fastembed`) at
  create and backfilled for older rows by the async worker. `memory_upsert`'s
  create response may now carry an advisory `near_duplicates` list (existing
  keywords that look like the same concept, with similarity + a content
  preview) — never a rejection, omitted when empty or the embedder is down.
  The pre-write search probe is correspondingly downgraded from required to
  optional in the protocol.
- **`age_days` freshness signal.** `memory_search` hits and `memory_get`
  entries (the latter also gains `fragment_count`) now carry `age_days`. No
  ranking change — it's for the reading agent's judgment: correct a stale or
  contradicted memory in the same turn instead of just noting it.
- **Admin reports**: `/admin/reports/stale` (entries untouched for
  `STALE_REPORT_DAYS`, default 90) and `/admin/reports/duplicates` (same-project
  keyword pairs above `NEAR_DUPLICATE_THRESHOLD`, default 0.80), both
  team-scoped and read-only, linked from the dashboard.
- **MCP instructions v3** (592 tokens): documents the optional probe, the
  `near_duplicates` self-correction, `age_days`-driven correct-on-contradiction,
  and softens compaction to "same turn if you can" — a team-level sweeper is
  planned as the safety net for deferred compactions (not yet built).

## [1.0.0] — 2026-07-04

First public release: the v1 MVP, deployed and dogfooded.

### Added

- **Remote MCP server** (FastAPI + MCP Python SDK, Streamable HTTP on `/mcp`)
  backed by PostgreSQL 16 — zero server-side LLM in the critical path.
- **Six memory tools:** `memory_search`, `memory_get`, `memory_append`,
  `memory_upsert`, `memory_list`, `memory_delete`. See
  [docs/mcp-tools.md](docs/mcp-tools.md).
- **The refine → append → compaction → retrieve protocol**, shipped to every
  client in the MCP `initialize` handshake — no agent-side code required.
- **Append-only writes** (never blocking, never conflicting) with
  **merge-on-read compaction** (atomic, idempotent, fully versioned in
  `memory_history`).
- **Full-text search** over content and pending fragments (generated `tsvector`
  + GIN, exact/prefix keyword match weighted above FTS) with `related_keywords`
  suggestions and cross-project `scope="all"`.
- **Token caps and backpressure** via tiktoken (`cl100k_base`): entry cap 2000,
  fragment cap 500, compaction threshold, and a 10-fragment hard limit — all
  surfaced as actionable errors.
- **Team tenancy:** a shared API key identifies a team; `team_id` is resolved
  from the authenticated key, never from a parameter; strict cross-team
  isolation (foreign data returns `NOT_FOUND`).
- **Bearer-key auth** (SHA-256 hashes stored), per-key rate limiting
  (120 req/min), and structured JSON audit logging per tool call.
- **CLI key management** (`scripts/create_api_key.py`): `team-create`,
  `key-issue`, `key-revoke`, `key-rotate`; `make team` / `make apikey` wrappers.
- **Admin console** at `/admin`: per-team login with a team key; browse
  projects → entries → detail; soft-delete and naïve compaction.
- **Production deployment**: Docker Compose stack with self-contained Caddy TLS
  (dedicated host) or behind a shared reverse proxy; daily `pg_dump` backups
  with retention and a destructive-safe restore script. See
  [docs/self-hosting.md](docs/self-hosting.md) and [RUNBOOK.md](RUNBOOK.md).
- **Prototype hybrid retrieval** (`ENABLE_HYBRID_SEARCH`, **off by default**):
  `pgvector` semantic search fused with lexical ranking via Reciprocal Rank
  Fusion; embeddings computed asynchronously, off the write path.
- **Search intent tag** (`intent="read"|"write"`) — an audit-only signal to
  de-confound the read-path zero-result rate; never affects results.
- Documentation set: [README](README.md), [architecture](docs/architecture.md),
  [concepts](docs/concepts.md), [MCP tools](docs/mcp-tools.md),
  [self-hosting](docs/self-hosting.md), [runbook](RUNBOOK.md),
  [security](SECURITY.md).

[1.0.0]: https://github.com/CODEPRESS-SRL/archivist/releases/tag/v1.0.0
