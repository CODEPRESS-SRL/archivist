# Design — `archivist-connect`: one-command client setup

**Date:** 2026-07-04
**Status:** Approved (brainstorming) → ready for implementation plan

## Problem

Connecting a developer's coding agent to Archivist today takes several manual,
easy-to-get-wrong steps, split across two concerns:

1. **MCP registration** in the agent — different per client (Claude Code
   `claude mcp add`, Codex `~/.codex/config.toml`, Cursor `~/.cursor/mcp.json`).
   This is what gives the agent the `memory_*` tools.
2. **Companion plugin** (`archivist-companion`, Claude Code only) — must be
   installed *and* configured in `~/.archivist-companion.json` with `serverUrl`
   + `apiKey` for auto-capture / sweep / session-index to work.

We want a single command a developer runs on their machine that installs the
client side, wires the agent to the remote MCP server (address supplied), and
confirms the team the token authenticates as — so onboarding is one line, not a
checklist.

## Scope decisions (locked during brainstorming)

- **Token flow: consume an existing token.** The admin issues the team key once
  via the server-side CLI (`create_api_key.py key-issue`), as today. The
  installer takes `serverUrl` + an already-issued token and wires everything
  up. **No new server work, no new HTTP surface.** "Return the API token for the
  team" is satisfied by the installer *verifying and echoing back the team the
  token authenticates as* — not by minting a token.
- **Target agent: Claude Code first-class + auto-detect.** Full experience for
  Claude Code (MCP registration + companion). If `~/.codex/config.toml` or
  `~/.cursor/mcp.json` already exist, also write their `archivist` MCP block.
  The companion is Claude-Code-only and is never installed for the others.
- **Command form: `npx` (Node), arg-driven with interactive fallback.**
  Cross-platform (the team is mixed; the maintainer is on Windows, where
  `curl | bash` is a poor fit). Reuses the existing Node companion code.

## Approach: installer lives inside the companion package (Approach A)

The connect CLI ships *as part of* the `archivist-companion` package. The
package is therefore both the plugin and its installer, so `npx` already has the
plugin files on hand — no marketplace, no dependency on the (currently empty)
GitHub repo.

- `companion/package.json`: remove `"private": true`; add
  `"bin": { "archivist-connect": "./scripts/connect.js" }`.
- Distribution supports **both** a published npm package (`npx archivist-connect`)
  and `npx <git-url>` for an internal team that does not want to publish. The
  script makes no assumption about its provenance.
- Companion install (step 4 below) uses the CLI's own location
  (`import.meta.url`) to find the plugin files it ships alongside.

Rejected — **Approach B** (separate installer that registers a Claude Code
marketplace and runs `claude plugin install`): depends on the GitHub repo being
populated as a marketplace (it is not yet), and splits maintenance across two
artifacts.

## The command

```bash
npx archivist-connect --server https://archivist.example.com/mcp/ --key arc_xxx
```

Flags:

| Flag | Required | Default | Meaning |
|---|---|---|---|
| `--server <url>` | yes (prompted if absent) | — | Archivist MCP endpoint |
| `--key <arc_...>` | yes (prompted if absent) | — | Team API key |
| `--scope user\|project` | no | `user` | Claude Code MCP scope (machine-global vs current repo) |
| `--no-companion` | no | off | Register MCP only; skip companion install + config |
| `--yes` | no | off | Non-interactive (CI): never prompt; fail if a required flag is missing |

## Execution steps (in order)

1. **Verify (fail fast, before writing anything).** Perform the MCP
   `initialize` handshake against `<server>` with the Bearer token, reusing
   `companion/lib/mcp-client.js`. On 401 / unreachable / non-Archivist response,
   print an actionable error and exit non-zero **without touching any config**.
   On success, surface the authenticated **team / attribution** if the handshake
   or a cheap follow-up tool call (`memory_list`) exposes it; otherwise report
   connectivity confirmed. This step is the "return the API token for the team"
   confirmation.
2. **Register MCP in Claude Code.** Run
   `claude mcp add --transport http archivist <server> --header "Authorization: Bearer <key>"`,
   honoring `--scope`. Idempotent: if an `archivist` server is already
   registered, remove and re-add so the command is safe to re-run. If the
   `claude` CLI is not found, warn and continue (companion + other agents can
   still be configured).
3. **Auto-detect Codex / Cursor.** Only if the file already exists: add the
   `archivist` block to `~/.codex/config.toml`
   (`[mcp_servers.archivist]` + `bearer_token_env_var`) and/or
   `~/.cursor/mcp.json` (`mcpServers.archivist`). Never create these files from
   scratch; never install the companion for them.
4. **Install the companion (Claude Code).** Register the plugin files the CLI
   ships alongside (located via `import.meta.url`) with Claude Code's plugin
   config. Skipped under `--no-companion`.
5. **Write `~/.archivist-companion.json`.** Merge `serverUrl` + `apiKey` into
   the existing config (or create it), leaving other keys and their defaults
   intact. Honor `ARCHIVIST_COMPANION_CONFIG` if set. Skipped under
   `--no-companion`.
6. **Summary.** Print what was configured (MCP for which agents, companion
   yes/no), the authenticated team, and the verification hint `claude mcp list`.

## New / changed files

- **New:** `companion/scripts/connect.js` — the CLI. Reuses `lib/mcp-client.js`
  (verify), `lib/config.js` (companion config read/merge), `lib/slug.js` as
  needed. No new runtime dependencies.
- **Changed:** `companion/package.json` — drop `private`, add `bin`.
- **New:** `companion/test/connect.test.js` — node `--test`.

## Documentation deliverables

Explicit requirement: clear install/config docs **and** a reference of every
command a developer uses on their agent.

1. **`README.md` (root) → Quick start.** Replace the three manual MCP snippets
   with the single `npx archivist-connect …`, keeping the manual per-client
   snippets as an "Advanced / manual" fallback.
2. **`companion/README.md`.** Add a "One-command setup" section at the top plus
   the flag table.
3. **New `docs/agent-commands.md`.** Reference of everything a developer invokes
   on the agent: the `/archivist-companion:sweep` slash command, the six
   `memory_*` MCP tools (what each does and when the agent calls it), the
   `sweep-cli.js` scheduler entry point, and `archivist-connect` itself. Linked
   from `README.md` and `CLAUDE.md`.
4. **`docs/self-hosting.md`.** Note that after `key-issue`, the admin simply
   hands the developer `server + key` to pass to `archivist-connect`.

## Testing

- **Unit** (`node --test`, matching the rest of the companion suite),
  `companion/test/connect.test.js`:
  - flag parsing (required-flag handling, `--yes`, `--no-companion`, `--scope`);
  - verify failure → **no config writes** (assert filesystem untouched);
  - idempotent MCP block in Codex/Cursor config (re-run does not duplicate);
  - auto-detect is a no-op when `~/.codex` / `~/.cursor` files are absent;
  - companion config merge preserves unrelated keys.
  - `claude mcp add` invocation and all network calls are mocked — no real CLI
    call, no real HTTP.
- **Manual end-to-end** against the live server
  (`https://archivist.schengatto.cloud/mcp/`) before closing: run the command,
  confirm `claude mcp list` shows `archivist`, confirm the companion index
  injects at next session start.

## Non-goals

- No server-side changes; no HTTP key-issuance endpoint.
- No token minting/rotation from the client.
- No companion support for Codex/Cursor (Claude Code only, by design).
- No uninstall command (out of scope for this pass).
