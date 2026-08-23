<div align="center">

# 🗄️ Archivist

**Shared, keyword-based memory for coding agents.**

A remote [MCP](https://modelcontextprotocol.io) server that lets a whole team's
AI coding agents — Claude Code, Codex, Cursor, anything that speaks MCP — save
and recall hard-won context across sessions, machines, and people.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
&nbsp;·&nbsp; [Concepts](docs/concepts.md) &nbsp;·&nbsp; [Tools](docs/mcp-tools.md) &nbsp;·&nbsp; [Self-hosting](docs/self-hosting.md) &nbsp;·&nbsp; [Architecture](docs/architecture.md)

</div>

---

## The problem

Coding agents forget everything between sessions. The lesson your agent learned
debugging the auth flow on Monday is gone by Tuesday — and your teammate's agent
never learned it at all. Knowledge that should compound instead evaporates,
over and over, per person.

## What Archivist does

Archivist is a small, self-hosted server that gives your team **one shared
memory**, addressed by keyword. Agents refine what they learn into dense notes
and save them; any teammate's agent can search and reuse them later. The server
is **pure storage and search — it runs no LLM.** Refining and merging content is
the agent's job, so there's no per-request model cost, no latency spike, and no
provider lock-in.

```text
   Your agent learns something          A teammate's agent, next week
   ┌────────────────────────┐           ┌────────────────────────┐
   │ "the deploy needs XFF   │           │  memory_search          │
   │  trust or the throttle  │  ──────▶  │  "deploy proxy"         │
   │  keys on the proxy IP"  │  Archivist│  ──▶ finds the note,    │
   └────────────────────────┘  (shared) │      no rediscovery      │
                                         └────────────────────────┘
```

### How it works — the cycle

1. **Refine** *(agent)* — extract 1–3 keywords, compress the concept into a
   dense, self-contained note.
2. **Write** — new keyword → create it; existing keyword → **append** a
   fragment. Writes are events: they never block and never conflict, even when
   two teammates save at once.
3. **Compaction (merge-on-read)** — when someone reads a keyword with queued
   fragments, their agent merges everything back into one refined note. Lazy,
   safe, fully versioned.
4. **Retrieve** — `memory_search` (exact keyword + Postgres full-text) whenever
   context is needed; `scope="all"` searches across every project.

Read the full model in **[docs/concepts.md](docs/concepts.md)**.

### Highlights

- 🧠 **Zero server-side LLM** — pure SQL in the critical path; the agent does the thinking.
- ✍️ **Append-only writes** — no write races by construction, never a blocked write.
- ♻️ **Merge-on-read compaction** — notes stay dense; every version kept in history.
- 👥 **Team-scoped** — one API key per team; strict cross-team isolation.
- 🔎 **Full-text search** built in, with optional `pgvector` semantic retrieval.
- 🔌 **Any MCP client** — one command to connect; the usage protocol ships in the handshake.
- 🖥️ **Admin console** at `/admin` to browse a team's archive.
- 🐳 **One `docker compose up`** to self-host, TLS included.

---

## Quick start — connect your agent

Your administrator gives you two things: the server URL (`https://<domain>/mcp/`)
and a team **API key** (`arc_…`, a secret — treat it like a password). Then run:

```bash
npx archivist-connect --server https://<domain>/mcp/ --key arc_xxx
```

This verifies the token against the server, registers the MCP in Claude Code,
auto-detects Codex/Cursor and wires them too, installs the companion plugin, and
writes its config. Verify with `claude mcp list`.

- Adds a short "use Archivist" rule to your user-level agent rules
  (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`) for each detected agent.
  Pass `--no-rules` to skip it. The rule is a managed block, so re-running
  updates it in place.

<details><summary>Advanced / manual setup (per client)</summary>

### Claude Code

```bash
claude mcp add --transport http archivist https://<domain>/mcp \
  --header "Authorization: Bearer <key>"
```

Verify: `claude mcp list`.

### Codex CLI

Export the token, then add the server to `~/.codex/config.toml`:

```bash
export ARCHIVIST_TOKEN="<key>"
```

```toml
[mcp_servers.archivist]
url = "https://<domain>/mcp"
bearer_token_env_var = "ARCHIVIST_TOKEN"
```

### Cursor

Add to `~/.cursor/mcp.json` (or a project-local `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "archivist": {
      "url": "https://<domain>/mcp",
      "headers": { "Authorization": "Bearer <key>" }
    }
  }
}
```

That's it — start a session and your agent can `memory_search`,
`memory_append`, and the rest. See the [tools reference](docs/mcp-tools.md).

</details>

---

## Self-hosting

Archivist runs as a Docker Compose stack — proxy (TLS) → api (FastAPI + MCP) →
PostgreSQL — that stands up with a single command on a VPS.

```bash
cp .env.example .env      # set POSTGRES_PASSWORD, ADMIN_SESSION_SECRET, ARCHIVIST_DOMAIN
docker compose -f docker-compose.prod.yml up -d --build
```

Then create a team and issue a key:

```bash
docker compose -f docker-compose.prod.yml exec api \
  python scripts/create_api_key.py team-create <team-name>
docker compose -f docker-compose.prod.yml exec api \
  python scripts/create_api_key.py key-issue --team <team-name> --label "<member/laptop>"
```

The full guide — dedicated-host vs. shared-proxy TLS, configuration, key
management, the admin console, backups and restore — is in
**[docs/self-hosting.md](docs/self-hosting.md)**. For incidents, see the
**[operations runbook](RUNBOOK.md)**.

### Local development

```bash
make up        # build + start db + api (docker compose)
make test      # run the test suite (needs Docker for testcontainers)
make lint      # ruff check + format check
make help      # list all targets
```

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full dev setup.

---

## What Archivist writes to your disk

Memories live in Postgres on the server. On a **client** machine, the only
thing installed locally is the optional companion, and everything it persists
sits under a single directory:

| Platform | Root | Cache |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Archivist` | `%LOCALAPPDATA%\Archivist\Cache` |
| macOS | `~/Library/Application Support/Archivist` | `~/Library/Caches/Archivist` |
| Linux | `$XDG_DATA_HOME/archivist` | `$XDG_CACHE_HOME/archivist` |

`ARCHIVIST_HOME` relocates that root; it is the only override. Nothing is
written to the working directory, to the root of your home, or next to the
sources — and no log file exists unless you ask for one
(`ARCHIVIST_LOG_FILE=1`; diagnostics otherwise go to stderr, as the MCP spec
requires). Temporary directories are cleaned up when a run ends, and a startup
sweep reclaims whatever an abrupt kill left behind.

```bash
npx --package archivist-connect archivist paths   # what is written, where, how big
npx --package archivist-connect archivist purge   # remove all of it
```

**After uninstalling**, `archivist purge` leaves nothing of its own behind. What
stays are the entries Archivist added to *other tools'* files — the plugin under
`~/.claude/plugins/`, the MCP registration in Claude Code / `~/.codex/config.toml`
/ `~/.cursor/mcp.json`, and the `archivist:begin…end` rule block in
`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. `archivist paths` prints the exact
list; removal instructions are in
[companion/README.md](companion/README.md#uninstalling).

On a **server**, `python -m app.paths` prints the same inventory for the API
process (its only local footprint is the pinned fastembed/tiktoken cache; in
Docker both live inside the image).

---

## Documentation

| Guide | What's inside |
|---|---|
| **[Core concepts](docs/concepts.md)** | The memory cycle, namespacing, caps |
| **[MCP tools](docs/mcp-tools.md)** | The seven tools + error codes |
| **[Architecture](docs/architecture.md)** | Components, schema, design decisions |
| **[Self-hosting](docs/self-hosting.md)** | Deploy, configure, administer, back up |
| **[Runbook](RUNBOOK.md)** | Incident response |
| **[Security](SECURITY.md)** | Threat model, controls, vulnerability reporting |

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © 2026 Enrico Schintu
