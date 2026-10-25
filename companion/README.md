# archivist-companion

Optional Claude Code plugin for the [Archivist](../README.md) shared-memory
MCP server. Archivist's manual protocol (search/append/upsert via the MCP
tools) works standalone; this plugin automates the parts of that protocol
that a human would otherwise have to remember to do. Every component is
**fail-silent**: if the Archivist server is down, the API key is missing, or
`node` can't be found, the hook exits quietly and the session is unaffected.

## One-command setup

```bash
npx archivist-connect --server https://<domain>/mcp/ --key arc_xxx
```

| Flag | Default | Meaning |
|---|---|---|
| `--server <url>` | prompted | Archivist MCP endpoint |
| `--key <arc_...>` | prompted | Team API key |
| `--scope user\|project\|local` | `user` | Claude Code registration scope |
| `--no-companion` | off | Register MCP only; skip companion install + config |
| `--no-rules` | off | Skip writing the Archivist rule into agent rules files |
| `--yes` | off | Non-interactive (CI): fail instead of prompting |

It verifies the token first and writes nothing if verification fails. Re-running
is safe (idempotent). The manual steps below remain available if you prefer them.

- **Shared machines:** omit `--key` and type the key at the interactive prompt.
  A key passed as an argument is visible to other local processes and lands in
  your shell history.
- **`--scope project`:** Claude Code writes that registration into the
  project's `.mcp.json`, which is meant to be committed. So for this scope the
  header is registered as `Authorization: Bearer ${ARCHIVIST_TOKEN}`, a
  reference Claude Code expands from the environment, never the key itself.
  Every teammate sets `ARCHIVIST_TOKEN` before starting Claude Code
  (`export ARCHIVIST_TOKEN=arc_xxx`, or `setx ARCHIVIST_TOKEN "arc_xxx"` on
  Windows). Scopes `user` and `local` store the key in Claude Code's per-user
  config.
- **`ARCHIVIST_TOKEN` on Windows:** `setx` stores the key persistently in
  `HKCU\Environment`, readable by any process running as your user.
- **Cursor:** `~/.cursor/mcp.json` holds the key, so on macOS and Linux it is
  set owner-only (`0600`) after every write.
- **`--server`** must be an `http://` or `https://` URL without spaces, quotes
  or control characters; anything else is refused before verification.

## What it does

- **C1 — SessionEnd auto-capture.** At the end of a session, refines
  whatever was substantive into at most 3 memories via a headless
  `claude -p` run (default model `haiku`). Gated so trivial sessions cost
  nothing: it only fires when the transcript is at least `gateMinChars`
  characters (default 2000) or at least one file was edited during the
  session.
- **C2 — sweep.** Compacts keywords that are `compaction_due` *and* older
  than `sweepGraceDays` (default 3) — entries younger than the grace window
  are left for an in-session reader to compact for free. Zero LLM calls when
  nothing is due. Available two ways:
  - in-session: `/archivist-companion:sweep`
  - out-of-session, for cron / Task Scheduler: `node companion/scripts/sweep-cli.js`
- **C3 — SessionStart index.** Injects an LLM-free keyword index for the
  current project — one cheap MCP `memory_list` call, no headless run. Each
  line is a keyword, flagged with `⚠ compaction due` when applicable.

> **Deviation from a content-preview index:** the C3 index lists keywords and
> a compaction-due flag only, not content previews. `memory_list` does not
> return content, so a preview would require an additive change to the
> Archivist server; out of scope for this plugin.

## Requirements

- **Node.js 20 or newer** (`node` on `PATH`; the package declares
  `engines.node >=20`).
- **Claude Code** (the `claude` CLI on `PATH`) for the plugin, the hooks and
  the MCP registration. Without it `npx archivist-connect` still configures
  Codex and Cursor, but skips the Claude Code registration and the plugin.
- A running Archivist server and a team API key.

**Windows notes:**

- Every `claude` and `git` process the companion starts is resolved from
  `PATH` only, never from the current directory, so an executable committed to
  the repository you run it in is never picked up.
- `npx archivist-connect` first spawns `claude` directly. If that fails as
  not found, it retries `claude.cmd` (the npm shim) through `cmd.exe` as a single
  quoted command string. Arguments holding a character `cmd.exe` would interpret
  (`% & | ^ < > " !` or a newline) are refused, and it prints the command to run
  by hand.
- The capture (C1) and sweep (C2) runs spawn `claude` through Node's
  `child_process.spawn` with no shell and no `.cmd` fallback. If `claude` is
  installed only as a `.cmd`/npm shim, those runs fail silently, by design, so
  they never crash your session, but they also do nothing. If auto-capture or
  sweep appear to do nothing on Windows, verify `claude` is invocable as a bare
  command from a non-shell process. (The C3 index is unaffected, since it never
  spawns `claude`.)

## Install

**Marketplace:**

```bash
/plugin marketplace add CODEPRESS-SRL/archivist
/plugin install archivist-companion@archivist-tools
```

**Local (no marketplace):**

```bash
claude --plugin-dir companion
```

## Configuration

Config lives under the Archivist root (see
[Where things are written](#where-things-are-written)):

| Platform | Config file |
|---|---|
| Windows | `%LOCALAPPDATA%\Archivist\companion.json` |
| macOS | `~/Library/Application Support/Archivist/companion.json` |
| Linux | `$XDG_DATA_HOME/archivist/companion.json` (fallback `~/.local/share/archivist/companion.json`) |

A missing or malformed file simply falls back to defaults — nothing throws.
See [`.archivist-companion.example.json`](./.archivist-companion.example.json)
for a starting point.

> **Upgrading from 1.0:** the config used to live at
> `~/.archivist-companion.json`. That file is still read as a fallback, and the
> next `npx archivist-connect` migrates its contents to the new location and
> deletes it. Nothing to do by hand. `ARCHIVIST_COMPANION_CONFIG` is gone —
> use `ARCHIVIST_HOME` to relocate the whole root instead.

| Key | Default | Meaning |
|---|---|---|
| `serverUrl` | `null` | Archivist MCP endpoint, e.g. `https://archivist.example.com/mcp/` |
| `apiKey` | `null` | Team API key (`arc_...`) |
| `capture` | `true` | Enable C1 SessionEnd auto-capture |
| `index` | `true` | Enable C3 SessionStart index injection |
| `captureModel` | `"haiku"` | Model used for the C1 capture run |
| `captureMaxTurns` | `12` | Max turns for the C1 headless run |
| `gateMinChars` | `2000` | Transcript size (chars) above which C1 fires even without a file edit |
| `sweepGraceDays` | `3` | Age (days) a `compaction_due` keyword must reach before the sweep compacts it |
| `sweepModel` | `"sonnet"` | Model used for each C2 merge run |
| `sweepMaxTurns` | `12` | Max turns for each C2 merge run |

`serverUrl` and `apiKey` can also be set via environment variables, which
take precedence over the file:

- `ARCHIVIST_SERVER_URL`
- `ARCHIVIST_API_KEY`

The plugin considers itself configured only when both `serverUrl` and
`apiKey` are set (from file or env); otherwise C1, C2, and C3 all no-op.

Three more variables control the footprint, not the behaviour:

| Variable | Effect |
|---|---|
| `ARCHIVIST_HOME` | Relocates the entire Archivist directory (cache and temp included). The only path override there is. |
| `ARCHIVIST_DEBUG=1` | Print diagnostics to **stderr** (never stdout — a SessionStart hook's stdout is the injected index). Off by default, so the fail-silent contract holds. |
| `ARCHIVIST_LOG_FILE=1` | Also append to `<root>/logs/companion.log`, rotated at 1 MB with one backup — so logging costs at most 2 MB, forever. No log file exists unless you set this. |

## Where things are written

Everything the companion persists lives under **one** directory:

| Platform | Root | Cache |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Archivist` | `%LOCALAPPDATA%\Archivist\Cache` |
| macOS | `~/Library/Application Support/Archivist` | `~/Library/Caches/Archivist` |
| Linux | `$XDG_DATA_HOME/archivist` | `$XDG_CACHE_HOME/archivist` |

Inside it: `companion.json` (the config, `chmod 600` — it holds your API key),
`tmp/` (short-lived `archivist-<pid>-*` directories staging the MCP config for
a headless run), `logs/` (only if you opted in), and `<cache>/companion/`
(bounded by a size cap and a TTL). The companion prunes only its own cache
subdirectory — on Windows the cache root is shared with a locally-run Archivist
server's model cache, which it must never evict.

Nothing is ever written to the working directory, to the root of your home
directory, or next to the sources. Temporary directories are removed when the
run finishes; if the process is killed outright (`SIGKILL`, a hook timeout, a
crashing IDE), the next startup reclaims whatever it left behind — anything
carrying the `archivist-` prefix, older than 12 hours, whose owning process is
gone. That sweep is idempotent and can never block a session from starting.

Inspect it yourself:

```bash
npx --package archivist-connect archivist paths   # what is written, where, how big
npx --package archivist-connect archivist gc      # run the cleanup sweep now
npx --package archivist-connect archivist purge   # delete all of it (asks first)
```

## Uninstalling

`archivist purge` removes the Archivist directory, the API key included. Four
things live **outside** that root, because they belong to other tools, and
`purge` deliberately leaves them alone — remove them by hand if you want a
clean machine:

| What | Where | How to remove |
|---|---|---|
| The companion plugin | `~/.claude/plugins/…` | `claude plugin uninstall archivist-companion@archivist-tools` |
| MCP registration | Claude Code's own config | `claude mcp remove archivist -s user` |
| MCP registration | `~/.codex/config.toml`, `~/.cursor/mcp.json` | delete the `archivist` entry |
| The rule block | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` | delete the block between `<!-- archivist:begin -->` and `<!-- archivist:end -->` |

`archivist paths` prints this same list, with the real paths for your machine.

Note that the capture (C1) and sweep (C2) runs spawn `claude` itself, and each
of those sessions writes its own transcript under `~/.claude/projects/` — that
is Claude Code's own storage, subject to its retention settings, not
Archivist's.

## Scheduling the sweep

Point cron (or Windows Task Scheduler) at:

```bash
node companion/scripts/sweep-cli.js
```

It reads config the same way as the in-session hooks, runs the sweep, prints
a JSON summary (`{ slug, compacted, failed }`), and always exits 0.

**Important:** the scheduler's environment must *not* set
`ARCHIVIST_HEADLESS=1`. That variable is how the plugin's own headless
`claude -p` runs (spawned by C1/C2) signal "don't recurse" to their own
hooks — every entry script checks it first and exits immediately if it's
set. If your cron/Task Scheduler environment happens to already export it
(e.g. inherited from a parent headless run), the sweep will silently no-op.

## Cost model

- **Capture (C1)** spawns at most one headless LLM run per substantive
  session, on the cheap model (`haiku` by default). Trivial sessions never
  reach the spawn.
- **Sweep (C2)** is zero-cost — no LLM call at all — whenever no keyword is
  both `compaction_due` and past its grace window.
- **Index (C3)** is always LLM-free: one `memory_list` read formatted
  locally.
