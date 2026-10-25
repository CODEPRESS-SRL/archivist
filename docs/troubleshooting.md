# Troubleshooting

Fixes for the problems people hit most often when setting Archivist up. Commands
assume the dev stack from the [laptop quickstart](../README.md#run-it-on-your-laptop-in-5-minutes);
for a VPS add `-f docker-compose.prod.yml` to every `docker compose` command.

## First, check the basics

```bash
curl http://localhost:8000/healthz     # {"status":"ok","db":"ok"} when the server is healthy
docker compose ps                      # api and db should be "running" / "healthy"
docker compose logs api                # the last startup lines show migration and bind errors
```

## The agent calls fail with 401

The API key is wrong, mistyped, or revoked. Keys start with `arc_` and are shown
only once, when issued.

1. Issue a new key and use it:
   ```bash
   docker compose exec api python scripts/create_api_key.py key-issue --team <team-name> --label "<member/laptop>"
   ```
2. Re-run `npx archivist-connect --server <url> --key <new key>`. Re-running is
   safe and replaces the old registration.
3. To see which keys exist and whether one was revoked:
   ```bash
   docker compose exec db psql -U archivist -d archivist \
     -c "SELECT id, team_id, owner_name, revoked FROM api_keys ORDER BY id;"
   ```

## The server answers 307 (redirect)

The MCP URL is missing its trailing slash. Use `http://localhost:8000/mcp/` (or
`https://<domain>/mcp/`), never `.../mcp`. Fix it in `archivist-connect --server`,
in `~/.codex/config.toml` (`url = ...`), in `~/.cursor/mcp.json`, or re-register
with `claude mcp add`.

## `relation "..." does not exist`

The database schema is missing or older than the code. The api container runs
`alembic upgrade head` on every start, so restart it and watch the log:

```bash
docker compose restart api
docker compose logs api
```

If the migration itself fails, the log shows why. You can also apply it by hand:

```bash
docker compose exec api alembic upgrade head
```

## Port 5432 or 8000 is already in use

`docker compose up` fails with `port is already allocated` or `address already in
use`. Either stop what holds the port (often a local PostgreSQL on 5432) or
change the host side of the mapping in `docker-compose.yml`:

```yaml
    ports:
      - "127.0.0.1:5433:5432"   # db: host port 5433
      - "127.0.0.1:8001:8000"   # api: host port 8001
```

If you move the api port, use it in the MCP URL (`http://localhost:8001/mcp/`).
The db port only matters if you connect to Postgres from the host; the api
reaches it over the Docker network and is not affected.

## `docker compose up` rejects the compose file

An error such as `additional properties 'required' not allowed` means Docker
Compose is older than v2.24. Check with `docker compose version` and update
Docker Desktop or the Compose plugin.

## "claude CLI not found" from `archivist-connect`

`archivist-connect` needs the Claude Code CLI to register the MCP. When it
cannot find `claude` it skips that step (it prints `claude CLI not found —
skipped Claude Code MCP registration`), still configures Codex and Cursor if
they are present, and skips the companion plugin install (the companion config
file is still written).

1. Install Claude Code and make sure `claude --version` works in a new terminal.
2. Re-run `npx archivist-connect ...`.

On Windows, Claude Code installed through npm is a `claude.cmd` shim. The
connector tries `claude` first and falls back to `claude.cmd` by itself. If the
server URL or key contains one of `% & | ^ < > " !` (or a newline), it refuses
to pass them through `cmd.exe` and prints the command to run manually:

```bash
claude mcp add --transport http archivist <url> -H "Authorization: Bearer <key>" -s user
```

## Codex does not authenticate

Codex reads the key from the `ARCHIVIST_TOKEN` environment variable
(`bearer_token_env_var = "ARCHIVIST_TOKEN"` in `~/.codex/config.toml`). It must
be set in the shell that starts Codex.

```bash
export ARCHIVIST_TOKEN="arc_xxx"           # macOS / Linux
```

```powershell
setx ARCHIVIST_TOKEN "arc_xxx"             # Windows: open a NEW terminal afterwards
```

Check with `echo $ARCHIVIST_TOKEN` (PowerShell: `$env:ARCHIVIST_TOKEN`) in the
same terminal, then start Codex from it.

## Admin login does not work on Safari

The `/admin` session cookie is marked `Secure`. Chrome and Firefox accept it over
`http://localhost`; Safari does not, so the login appears to succeed and then
drops you back on the login page. Use Chrome or Firefox for a local stack, or
serve the console over HTTPS (as the production stack does).

## How do I check that my agent sees the tools?

- Claude Code: `claude mcp list` should show `archivist` as connected. Inside a
  session, `/mcp` lists the server and its tools (`memory_search`, `memory_get`,
  `memory_append`, `memory_upsert`, `memory_list`, `memory_delete`,
  `memory_history`).
- Codex and Cursor: open the MCP/tools view of the client, or ask the agent to
  call `memory_list` for the current project.
- If the server shows as failed, check that the URL ends in `/mcp/`, that the
  key is valid (see the 401 entry), and that `curl http://localhost:8000/healthz`
  answers.

Still stuck? Open an issue at
<https://github.com/CODEPRESS-SRL/archivist/issues> with the output of
`docker compose logs api` (remove any key first).
