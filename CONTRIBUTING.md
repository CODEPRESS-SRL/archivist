# Contributing to Archivist

Thanks for your interest in improving Archivist. This guide covers the dev
setup, the test and lint gates, and the conventions the project follows.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Prerequisites

- **Docker** with the Compose plugin (the test suite uses testcontainers, and
  local dev runs Postgres in a container).
- **[uv](https://docs.astral.sh/uv/)** — manages the Python 3.12 environment for
  the `api/` project.
- **make** — thin wrappers around the common commands (optional but handy).

## Getting started

```bash
git clone <your-fork-url> archivist
cd archivist
cp .env.example .env          # dev defaults work as-is; no secrets needed locally
make up                       # build + start db + api
curl http://localhost:8000/healthz   # → {"status":"ok","db":"ok"}
```

Create a team and a key to exercise the MCP endpoint locally:

```bash
make team NAME=dev
make apikey TEAM=dev LABEL="dev/laptop"      # prints the key once
```

The `api/` project is managed by uv and is **not** a distributable package
(`package = false`); uv only manages its environment. Run tooling with
`uv --directory api run <cmd>` (the Makefile does this for you).

## Tests and linting — required before a PR

All new or changed code must be covered by tests, and the full suite must pass.

```bash
make test      # pytest (needs Docker running for testcontainers)
make lint      # ruff check + ruff format --check
```

Or directly:

```bash
uv --directory api run pytest
uv --directory api run ruff check .
uv --directory api run ruff format .
```

- Tests live in `api/tests/`. Integration tests spin up a real Postgres via
  testcontainers and drive the full search → append → get → compaction cycle.
- Ruff is configured in `api/pyproject.toml` (line length 100, target py312).

## Database migrations

Schema changes go through Alembic. The api container runs `alembic upgrade head`
on start (idempotent), so a rebuild self-migrates.

```bash
make migrate                                        # apply migrations
uv --directory api run alembic revision -m "<msg>"  # author a new migration
```

Migrations are committed to the repo (`api/alembic/versions/`).

## Conventions

- **Zero server-side LLM.** The critical path (search, get, append, upsert,
  list, delete) is pure SQL in a transaction. Refining and merging content is
  the calling agent's job — never add model calls to a tool handler.
- **The write path never blocks.** `memory_append` neither reads nor rewrites;
  the only optimistic lock is `expected_version` on compaction.
- **Actionable errors.** Every rejection must tell the agent what to do next, in
  the message. Error codes and text live in one place —
  [`api/app/errors.py`](api/app/errors.py).
- **Tokens via tiktoken** (`cl100k_base`), never a character heuristic.
- **Security-sensitive changes** (auth, input validation, networking, secrets)
  must update [SECURITY.md](SECURITY.md) and ship a dedicated test.
- Match the surrounding style; keep changes surgical and focused on the task.

## Submitting changes

1. Fork and branch from `main`.
2. Make your change with tests; keep `make test` and `make lint` green.
3. Update docs (`docs/`, `README.md`, `SECURITY.md`, `CHANGELOG.md`) when
   behavior, tools, or configuration change.
4. Open a pull request describing the change and its rationale.

## Reporting bugs and security issues

- **Bugs / feature requests:** open a GitHub issue.
- **Security vulnerabilities:** do **not** open a public issue — follow the
  private disclosure process in [SECURITY.md](SECURITY.md).
