# Changelog

All notable changes to Archivist are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] — 2026-10-25

First public release.

### Added

- **`memory_history` tool.** Read an entry's recorded version history (newest
  first): the content as it stood at each version, who wrote it, and when.
  Read-only — it never changes state. Use it after a compaction to check what
  the merge dropped, or to recover wording that was compressed away.
- **`include_deleted` on `memory_history`.** Pass `include_deleted=true` to
  read the history of a soft-deleted (pruned) entry — the newest generation of
  a keyword only. This is how an agent checks, or recovers, what a forgetting
  sweep dropped.

- **A single filesystem root, on every platform.** Everything Archivist
  persists on a machine now lives under one directory —
  `%LOCALAPPDATA%\Archivist` (Windows), `~/Library/Application Support/Archivist`
  (macOS), `$XDG_DATA_HOME/archivist` (Linux, fallback `~/.local/share`), with
  the cache in the platform's conventional place. `ARCHIVIST_HOME` relocates the
  whole tree and is the only override. Nothing is written to the working
  directory, the root of the home directory, or next to the sources.
- **`archivist paths` / `archivist gc` / `archivist purge`** (npm bin) and
  `python -m app.paths [--gc|--purge]` (server): print exactly what is written
  and where, sweep leftovers, or remove everything. Both READMEs document what
  remains on disk after uninstalling.
- **Guaranteed temp-directory cleanup.** Temporary directories are created
  under the Archivist root with an `archivist-<pid>-` prefix and removed by a
  context manager, plus `atexit` and SIGINT/SIGTERM handlers (chained, so
  uvicorn's graceful shutdown is preserved). A SIGKILL defeats all of those, so
  every entry point also runs a startup garbage collection: it removes only
  Archivist-prefixed directories, older than 12 hours, whose owning PID is
  gone. Idempotent, and it can never block startup.

### Fixed

- **fastembed and tiktoken no longer cache outside the declared root.** The
  tokenizer downloaded `cl100k_base` into the OS temp directory on first use
  (in the container too — the image never pre-warmed it), and outside Docker
  fastembed put its ~130 MB ONNX model there as well. Both caches are now
  pinned under the Archivist root, and the image pre-downloads and pins both,
  so a container performs no runtime download and writes no cache outside
  itself. An explicitly-set `FASTEMBED_CACHE_PATH`/`TIKTOKEN_CACHE_DIR` always
  wins.
- **Logs go to stderr, not stdout.** stdout is reserved for MCP protocol
  traffic. A log file is opt-in (`ARCHIVIST_LOG_FILE=1`), lives under the
  Archivist root, and rotates at 1 MB keeping one backup — bounded at 2 MB
  forever.
- **The companion API key is no longer passed on a child's command line.** The
  headless `claude -p` runs received the team key inline via `--mcp-config`,
  where any local process could read it out of `ps` / Task Manager. It is now
  staged in a `chmod 600` file under the Archivist root.
- **Two companion regression tests were silently inert.** The "configured
  against a live MCP server" cases built their environment by deleting
  `ARCHIVIST_SERVER_URL`/`ARCHIVIST_API_KEY` *after* applying them, so the
  scripts ran unconfigured and never exercised the undici teardown path they
  were meant to guard.
- **Parameter-name aliases on `memory_append` and `memory_get`.** The two
  tools whose parameter name differs from the rest of the surface were the
  only source of malformed calls in the field: agents carry `content` over
  from `memory_upsert` to `memory_append`, and write the singular `keyword`
  on `memory_get`. Both spellings are now accepted (`content` → `fragment`,
  `keyword` → `keywords`), and a call carrying neither returns an actionable
  `VALIDATION_ERROR` instead of a raw pydantic validation exception.

### Changed

- **Dev compose stack is ready on `make up`.** The `api` container now applies
  database migrations on start, so a fresh checkout boots without a manual
  `alembic upgrade`. Published ports bind to `127.0.0.1` only, so the dev
  database and API are not exposed to the local network.
- **Dev compose project renamed `archivist-dev`.** Dev and prod both resolved
  to the project `archivist` and shared the `archivist_pgdata` volume. The dev
  stack now uses `archivist-dev_pgdata`; an existing local dev volume
  `archivist_pgdata` is no longer used — migrate its data or remove it. On a
  host that also runs the prod stack, `archivist_pgdata` is the prod volume:
  do not remove it there.
- **`.env` is read inside the containers.** Both compose files load `.env`
  into the `api` services through an optional `env_file`, so settings such as
  `ENABLE_HYBRID_SEARCH` or `ENTRY_TOKEN_TARGET` reach the server instead of
  being silently ignored.
- **Companion config moved into the Archivist root** —
  `<root>/companion.json`, written `chmod 600` because it holds the team API
  key. The pre-1.1 `~/.archivist-companion.json` is still read as a fallback
  and is migrated (then deleted) by the next `archivist-connect` run, so
  existing installs keep working untouched. `ARCHIVIST_COMPANION_CONFIG` is
  removed in favour of the single `ARCHIVIST_HOME` override.
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

### Security

- **Companion executables are resolved from `PATH` only.** On Windows, a
  `claude.exe`, `git.exe` or `claude.cmd` committed to the repository where
  `archivist-connect` or a companion hook runs could be executed and receive
  the team key in its arguments. Every companion spawn now sets
  `NoDefaultCurrentDirectoryInExePath`.
- **`archivist-connect --scope project` keeps the key out of `.mcp.json`.** It
  registers `Authorization: Bearer ${ARCHIVIST_TOKEN}`, which Claude Code
  expands from the environment, instead of the plaintext key in a file meant to
  be committed.
- **`~/.cursor/mcp.json` is owner-only after every write**, not only when
  `archivist-connect` creates it.
- **`archivist-connect` validates `--server`** (http/https only, no control
  characters, quotes or whitespace) before writing it into any agent config.
- **Weak admin session secrets are flagged.** An empty `ADMIN_SESSION_SECRET`
  now logs the ephemeral-secret warning (it fell back to an ephemeral secret
  silently before), and a secret shorter than 32 characters logs a WARNING with
  a generation hint. Startup is never refused.

## [1.1.0] — 2026-07-04

Follow-up pass on the v1 MVP: hybrid search graduates to the default, and
three small freshness/dedup features close loops that dogfooding surfaced.

### Added

- **Hybrid retrieval, on by default.** `ENABLE_HYBRID_SEARCH` now defaults to
  `true` (was the prototype opt-in); set it to `false` to fall back to pure
  lexical search. The opt-in gate is retired — the failure modes it was
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

Initial release: the v1 MVP, deployed and dogfooded.

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

Versions 1.0.0 and 1.1.0 were released before the public history began, so
they have no tags or release pages. 1.2.0 is the first tagged release.

[Unreleased]: https://github.com/CODEPRESS-SRL/archivist/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/CODEPRESS-SRL/archivist/releases/tag/v1.2.0
