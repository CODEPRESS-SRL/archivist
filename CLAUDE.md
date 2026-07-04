# Archivist — Project Context

> Entry point for Claude Code working in this repo. Product docs live in
> [`docs/`](docs/): architecture ([docs/architecture.md](docs/architecture.md)),
> the memory protocol ([docs/concepts.md](docs/concepts.md)), the tool
> contract ([docs/mcp-tools.md](docs/mcp-tools.md)), and the agent-side
> commands ([docs/agent-commands.md](docs/agent-commands.md)). Contributor
> setup is in [CONTRIBUTING.md](CONTRIBUTING.md).

## What Archivist is

Shared, keyword-based memory for coding agents, hosted on a VPS, used by
a team. Content refinement is delegated to the agent itself: the server
is pure storage + search, **zero server-side LLM**.

**The cycle:**

1. **Refine** (agent-side) — extract keywords from the concept to save,
   produce a dense, compressed mini-context.
2. **Append-only writes** — keyword absent → `memory_upsert` creates the
   entry; present → `memory_append` queues a fragment. Writes are events:
   never blocking, never conflicting.
3. **Merge-on-read compaction** — whoever reads a keyword with
   `compaction_due=true` performs the merge (re-refines the whole value)
   and writes it back with `memory_upsert` (optimistic lock, atomic
   consumption of the fragments that were read).
4. **Retrieve** — `memory_search` (exact keyword + Postgres FTS) whenever
   context is needed; `scope="all"` for cross-project search.

## Stack

```text
VPS (Docker Compose):
  proxy (Caddy/nginx) → TLS, reverse proxy
  archivist-api       → FastAPI + MCP Python SDK (Streamable HTTP on /mcp)
  PostgreSQL 16+      → memories, memory_fragments, memory_history,
                        api_keys, projects, teams; FTS (tsvector + GIN)
```

No per-agent plugin: the server IS a remote MCP server. Every MCP client
(Claude Code, Codex, Cursor) connects with one command + an API key.
The refine→append→compaction protocol lives in the MCP instructions and
in the tool descriptions.

Tools: `memory_search`, `memory_get`, `memory_append`, `memory_upsert`,
`memory_list`, `memory_delete`. Signatures and semantics are in
[docs/mcp-tools.md](docs/mcp-tools.md) — do not invent parameters or tables
not defined there.

**Unique project slug**: repo name from the git remote, normalized
(`acme/Billing-API.git` → `billing-api`); fallback: root directory name.
The rule is encoded in the MCP instructions.

## Operating Rules

1. **Zero LLM in the server critical path.** Append, get, search, upsert:
   pure SQL inside a transaction. Merging is always the agent's job.
2. **The write path never blocks.** `memory_append` neither reads nor
   rewrites. The only allowed 409 is on compaction (`expected_version`).
3. **Compaction is atomic and idempotent.** It consumes only fragments
   with `id <= last_fragment_id` as read; a concurrent append survives.
4. **Tokens via tiktoken (cl100k_base).** Never `len(s)//4`. Entry cap
   ~2,000 tokens; compaction threshold: >3 fragments or 2/3 of the cap;
   hard limit 10 fragments → append rejected (backpressure).
5. **Keywords normalized server-side.** lowercase, trim, slug.
   `memory_search` suggests related keywords to prevent duplicates.
6. **Actionable errors.** Every rejection (cap, backpressure, 409) must
   tell the agent what to do next, in the message itself. Codes and text
   live in [`api/app/errors.py`](api/app/errors.py).
7. **Security first.** Hashed API keys, Bearer auth, TLS at the proxy, rate
   limit per key. Any change touching auth/network/input validation →
   note in [SECURITY.md](SECURITY.md) + dedicated test.

## Repository Structure

```text
archivist/
├── CLAUDE.md              # this file
├── README.md              # product presentation + quick start
├── CONTRIBUTING.md        # dev setup, tests, conventions
├── SECURITY.md            # threat model + controls
├── RUNBOOK.md             # operations: incident response
├── CHANGELOG.md
├── docs/                  # architecture, concepts, mcp-tools, self-hosting
├── api/
│   ├── app/               # FastAPI + MCP server, services, admin console
│   ├── alembic/           # migrations
│   └── tests/
├── docker-compose.yml     # dev
├── docker-compose.prod.yml
├── Caddyfile.prod
├── .env.example
└── Makefile
```

## Testing & tooling

Run tooling via `uv` from `api/` (the Makefile wraps it): `make test`
(pytest + testcontainers), `make lint` (ruff), `make up` (dev stack). See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Agent Workflow Rules (MANDATORY)

1. **Never push to remote branches.** The agent never runs `git push` —
   not on `main`, not on any other remote branch. Local commits are made
   only when explicitly requested; pushing is always the user's action.
2. **Never appear as author or co-author.** Do not add `Co-Authored-By`
   trailers or otherwise attribute commits to the coding agent.
3. **Brainstorm when the requirement is unclear.** If a prompt is
   ambiguous or underspecified, run the brainstorming skill (or ask
   clarifying questions) before writing code — never guess silently.
4. **Always review the session's work.** Before finishing, review what
   changed during the session using the most suitable review tools and
   skills (e.g. `/code-review`, the code-reviewer agent) and report the
   outcome.
5. **Always propose the next-session prompt.** When there is genuinely
   more work left, end the session by proposing a ready-to-use prompt for
   the next task in a fresh session. If nothing remains, say so explicitly.
