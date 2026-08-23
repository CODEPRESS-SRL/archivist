# Changelog

## Unreleased

- **Single filesystem root.** Config, cache, temp dirs and (opt-in) logs all
  live under `%LOCALAPPDATA%\Archivist` / `~/Library/Application Support/Archivist`
  / `$XDG_DATA_HOME/archivist`. `ARCHIVIST_HOME` is the only override;
  `ARCHIVIST_COMPANION_CONFIG` is removed. The pre-1.1
  `~/.archivist-companion.json` is still read and is migrated automatically on
  the next `archivist-connect` run.
- **`archivist` CLI** (`paths`, `gc`, `purge`) — see what is written on disk,
  sweep leftovers, or remove everything including the stored API key.
- **Cleanup that survives a kill.** Temp dirs are removed by a context manager
  plus `atexit`/SIGINT/SIGTERM handlers; whatever a SIGKILL strands is reclaimed
  by a startup sweep (Archivist-prefixed, >12 h old, owning PID gone). The sweep
  is idempotent and never blocks a session from starting.
- **The API key no longer appears in a child process's command line** — the MCP
  config for headless runs is staged in a `chmod 600` file instead of being
  passed inline.
- **Diagnostics on stderr, never stdout** (a SessionStart hook's stdout is the
  injected index). Off unless `ARCHIVIST_DEBUG=1`; `ARCHIVIST_LOG_FILE=1` adds a
  rotated log file capped at 2 MB total.
- `connect` now writes a short, managed "use Archivist" rule into detected
  user-level agent rules files (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`).
  Idempotent; skip with `--no-rules`.

## 1.0.0 — 2026-07-04

- Initial release of the archivist-companion Claude Code plugin.
- C1 SessionEnd auto-capture: refines a substantive session into ≤3 memories via
  a headless `claude -p` run, gated so trivial sessions cost nothing.
- C2 sweep: `/archivist-companion:sweep` and `scripts/sweep-cli.js` compact
  overdue keywords; zero LLM calls when nothing is due.
- C3 SessionStart index: injects a keyword index (LLM-free) for the project.
- Single config file `~/.archivist-companion.json`; all components fail-silent.
