# Contributing to Archivist

Thanks for your interest in improving Archivist. This guide covers the dev
setup, the test and lint gates, and the conventions the project follows.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Prerequisites

- **Docker** with Compose v2.24+ (the test suite uses testcontainers, and local
  dev runs Postgres in a container; the compose files use `env_file.required`).
- **[uv](https://docs.astral.sh/uv/)** — manages the Python 3.12 environment for
  the `api/` and `benchmark/` projects.
- **Node.js 20 or newer** — for the `companion/` package (`node --test`).
- **make** — thin wrappers around the common commands (optional; every target
  has a raw command below, which is what you use on Windows without make).

## Getting started

```bash
git clone <your-fork-url> archivist
cd archivist
cp .env.example .env          # optional: dev defaults work without a .env file
make up                       # build + start db + api (or: docker compose up -d --build)
curl http://localhost:8000/healthz   # → {"status":"ok","db":"ok"}
```

`.env` is optional in dev. If you set `POSTGRES_PASSWORD`, set `DATABASE_URL` to
match: the dev default hardcodes `archivist:archivist`, so changing only the
password leaves the api unable to log in. Do it before the first start, because
the `pgdata` volume keeps the password it was created with (`docker compose down
-v` resets it).

The api migrates the database on start. Once it is healthy, the MCP endpoint is
`http://localhost:8000/mcp/` (keep the trailing slash) and the admin console is
`http://localhost:8000/admin`. Dev ports are bound to `127.0.0.1` only.

Create a team and a key to exercise the MCP endpoint locally:

```bash
make team NAME=dev
make apikey TEAM=dev LABEL="dev/laptop"      # prints the key once
```

Without make:

```bash
docker compose exec api python scripts/create_api_key.py team-create dev
docker compose exec api python scripts/create_api_key.py key-issue --team dev --label "dev/laptop"
```

The `api/` project is managed by uv and is **not** a distributable package
(`package = false`); uv only manages its environment. Run tooling with
`uv --directory api run <cmd>` (the Makefile does this for you).

## Tests and linting — required before a PR

All new or changed code must be covered by tests, and the full suite must pass.

| Project | With make | Without make |
|---|---|---|
| `api/` tests (needs Docker running for testcontainers) | `make test` | `uv --directory api run pytest` |
| `api/` lint | `make lint` | `uv --directory api run ruff check .` and `uv --directory api run ruff format --check .` |
| `companion/` tests | `make test-companion` | `cd companion && node --test` |
| `benchmark/` tests | `make test-benchmark` | `uv --directory benchmark run pytest` |
| Everything | `make test-all` | run the three test commands above |

Run the gates for every project your change touches.

- Tests live in `api/tests/`. Integration tests spin up a real Postgres via
  testcontainers and drive the full search → append → get → compaction cycle.
- Ruff is configured in `api/pyproject.toml` (line length 100, target py312).

## Database migrations

Schema changes go through Alembic. The api container runs `alembic upgrade head`
on start in both the dev and prod compose files (idempotent), so a rebuild or a
plain restart self-migrates.

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
- **Commits follow [Conventional Commits](https://www.conventionalcommits.org/)**
  (`feat:`, `fix:`, `docs:`, `chore:`, ...), written in English, like the rest of
  the code, comments and docs.

## Submitting changes

1. Fork and branch from `main`.
2. Make your change with tests; keep the gates above green.
3. Update docs (`docs/`, `README.md`, `SECURITY.md`, `CHANGELOG.md`) when
   behavior, tools, or configuration change.
4. Open a pull request describing the change and its rationale.

**Licensing: inbound = outbound.** Contributions are accepted under the project's
[MIT license](LICENSE). There is no CLA to sign.

## Reporting bugs and security issues

- **Bugs / feature requests:** open a GitHub issue.
- **Security vulnerabilities:** do **not** open a public issue — follow the
  private disclosure process in [SECURITY.md](SECURITY.md).
