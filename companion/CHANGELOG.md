# Changelog

All notable changes to the Archivist companion (`archivist-connect` on npm and
the `archivist-companion` Claude Code plugin) are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions 1.0.0 through 1.0.2 were released before the public history began, so
they have no tags or release pages.

## [Unreleased]

## [1.1.0] — 2026-10-25

### Added

- **`archivist` CLI** (`paths`, `gc`, `purge`) — see what is written on disk,
  sweep leftovers, or remove everything including the stored API key.
- **Managed "use Archivist" rule.** `connect` writes a short rule into detected
  user-level agent rules files (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`).
  Idempotent; skip with `--no-rules`.
- **Windows `claude.cmd` fallback.** When `claude` cannot be spawned directly,
  `connect` retries the npm `.cmd` shim. Arguments holding characters that
  `cmd.exe` would interpret are refused with a manual-registration hint
  instead of being passed on.
- **Codex and Cursor config creation.** When `~/.codex` or `~/.cursor` exists
  but its config file does not, `connect` creates it. A `~/.cursor/mcp.json`
  that is not a valid JSON object is left untouched and reported.

### Changed

- **Single filesystem root.** Config, cache, temp dirs and (opt-in) logs all
  live under `%LOCALAPPDATA%\Archivist` / `~/Library/Application Support/Archivist`
  / `$XDG_DATA_HOME/archivist`. `ARCHIVIST_HOME` is the only override;
  `ARCHIVIST_COMPANION_CONFIG` is removed. The pre-1.1
  `~/.archivist-companion.json` is still read and is migrated automatically on
  the next `archivist-connect` run.
- **Diagnostics on stderr, never stdout** (a SessionStart hook's stdout is the
  injected index). Off unless `ARCHIVIST_DEBUG=1`; `ARCHIVIST_LOG_FILE=1` adds a
  rotated log file capped at 2 MB total.
- The npm package ships an explicit `files` whitelist and declares
  `engines.node >=20`.
- **`--scope project` registers `Authorization: Bearer ${ARCHIVIST_TOKEN}`**
  instead of the key, and prints how to set `ARCHIVIST_TOKEN`. Scopes `user`
  and `local` are unchanged.
- **`--server` is validated** with `new URL()` before use: only `http:` and
  `https:`, no control characters, quotes or whitespace.
- The MCP client reports the package version in `clientInfo.version`.

### Fixed

- **Cleanup that survives a kill.** Temp dirs are removed by a context manager
  plus `atexit`/SIGINT/SIGTERM handlers; whatever a SIGKILL strands is reclaimed
  by a startup sweep (Archivist-prefixed, >12 h old, owning PID gone). The sweep
  is idempotent and never blocks a session from starting.

### Security

- **The API key no longer appears in a child process's command line** — the MCP
  config for headless runs is staged in a `chmod 600` file instead of being
  passed inline.
- **`~/.cursor/mcp.json` is owner-only (`0600`) after every write**, not only
  when `connect` creates it. It holds the Bearer key.
- **Executables are resolved from `PATH` only.** Every `claude` and `git` spawn
  sets `NoDefaultCurrentDirectoryInExePath`, so on Windows a `claude.exe`,
  `git.exe` or `claude.cmd` planted in the current directory is never run and
  never receives the key.
- **No key in a committed `.mcp.json`.** `--scope project` stores a
  `${ARCHIVIST_TOKEN}` reference, so the team key stays out of the project's
  shared config.
- **`--server` validation** keeps a malformed URL out of the Claude Code, Codex
  and Cursor configs; the Codex `url` is written as a quoted TOML string.

## [1.0.2] — 2026-07-08

### Changed

- The merge/refine prompt follows the server's three-tier entry size model: it
  compresses toward ~2 000 tokens with an 8 000-token ceiling, exceeded only
  for genuinely irreducible content.

## [1.0.1] — 2026-07-04

### Fixed

- **Windows exit code 127 in hook scripts.** The session-start and sweep
  scripts forced `process.exit(0)` right after an MCP call, which raced the
  teardown of the pooled keep-alive socket and aborted the process after the
  correct output had been written, so the host treated the hook as failed.
  They now let the event loop drain, with an unref'd watchdog as a hang
  fallback.

## [1.0.0] — 2026-07-04

- Initial release of the archivist-companion Claude Code plugin.
- C1 SessionEnd auto-capture: refines a substantive session into ≤3 memories via
  a headless `claude -p` run, gated so trivial sessions cost nothing.
- C2 sweep: `/archivist-companion:sweep` and `scripts/sweep-cli.js` compact
  overdue keywords; zero LLM calls when nothing is due.
- C3 SessionStart index: injects a keyword index (LLM-free) for the project.
- Single config file `~/.archivist-companion.json`; all components fail-silent.
