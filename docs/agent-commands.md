# Agent commands reference

Everything a developer invokes on their coding agent once connected to Archivist.

## Setup

| Command | What it does |
|---|---|
| `npx archivist-connect --server <url> --key arc_xxx` | Verify the token, register the MCP in Claude Code (auto-detect Codex/Cursor), install + configure the companion. See flags in [companion/README](../companion/README.md#one-command-setup). |

## Housekeeping

| Command | What it does |
|---|---|
| `npx --package archivist-connect archivist paths` | Print every path Archivist writes to on this machine, with sizes, plus what lives outside its root. |
| `npx --package archivist-connect archivist gc` | Run the startup cleanup sweep now (stale `archivist-*` temp dirs owned by dead processes). |
| `npx --package archivist-connect archivist purge` | Delete the whole Archivist directory, API key included. Asks for confirmation unless `--yes`. |
| `docker compose exec api python -m app.paths [--gc\|--purge]` | The same three, for a self-hosted API server (add `-f docker-compose.prod.yml` on a VPS). |

See [Where things are written](../companion/README.md#where-things-are-written)
and [Uninstalling](../companion/README.md#uninstalling).

## MCP tools (called by the agent during a session)

The agent calls these automatically per the protocol shipped in the MCP
handshake; you rarely call them by hand. Full contract: [docs/mcp-tools.md](mcp-tools.md).

| Tool | Purpose |
|---|---|
| `memory_search` | Exact-keyword + full-text search; `intent="read"\|"write"`, `scope="all"` for cross-project. |
| `memory_get` | Fetch one keyword's full value (+ pending fragments). |
| `memory_append` | Queue a refined fragment onto an existing keyword. Never blocks. |
| `memory_upsert` | Create a keyword, or write a compacted value (optimistic lock). |
| `memory_list` | List a project's keywords to orient. |
| `memory_delete` | Remove a keyword. |
| `memory_history` | Read an entry's recorded versions; `include_deleted` reaches a pruned entry. |

## Claude Code companion

| Command | What it does |
|---|---|
| `/archivist-companion:sweep [project-slug]` | Compact all overdue keywords for the project (in-session). |
| `node companion/scripts/sweep-cli.js` | Same sweep, out-of-session, for cron / Task Scheduler. Prints `{ slug, compacted, failed }`. |

The companion also runs two hooks automatically: **SessionStart** injects a
keyword index for the current project, and **SessionEnd** auto-captures
substantive learnings. Configure via `companion.json` under the Archivist root (`%LOCALAPPDATA%\Archivist` on Windows) — see the
[companion README](../companion/README.md#configuration).
