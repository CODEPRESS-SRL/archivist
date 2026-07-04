# Changelog

## Unreleased

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
