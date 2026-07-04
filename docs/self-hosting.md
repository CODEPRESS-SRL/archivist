# Self-hosting guide

Everything an operator needs to stand up and run Archivist on a VPS: deploy,
configure, administer teams and keys, and back up. For **incident response**
(service down, disk full, compromised key, restore, upgrade) see
[../RUNBOOK.md](../RUNBOOK.md). For the security model see
[../SECURITY.md](../SECURITY.md).

## Deployment topologies

The repository ships a **self-contained stack** — proxy (TLS) → **api**
(FastAPI + MCP) → **db** (PostgreSQL) — that stands up with a single
`docker compose up`. The api and db publish no host ports and talk over an
internal Docker network.

There are two ways to terminate TLS:

- **[Option A — dedicated host](#option-a--dedicated-host):** the simple case.
  A bundled **Caddy** obtains and renews the certificate itself.
- **[Option B — behind a shared reverse proxy](#option-b--behind-a-shared-reverse-proxy):**
  an existing nginx/certbot already owns ports 80/443. Run **api + db only**.

## Prerequisites

- A VPS with **Docker** and the Compose plugin installed.
- A **DNS A record** for your domain (e.g. `archivist.example.com`) pointing at
  the VPS public IP, resolvable before you request a certificate.
- **Option A:** ports **80** and **443** reachable from the internet (80 is
  required for the ACME HTTP challenge and the HTTP→HTTPS redirect).
  **Option B:** the existing proxy already owns 80/443 — this stack does not
  publish them.

## Configuration

Create `.env` on the server (never commit it — it is git-ignored). At minimum:

```bash
POSTGRES_PASSWORD=<a strong, URL-safe secret>   # mandatory in prod, no default
ADMIN_SESSION_SECRET=<a stable secret>          # mandatory in prod, no default
ARCHIVIST_DOMAIN=archivist.example.com          # your FQDN (drives Caddy's cert in Option A)
```

`DATABASE_URL` is assembled from `POSTGRES_*` in the prod compose, so you do not
set it by hand. See [`.env.example`](../.env.example) for the full list. The
main tunables:

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_PASSWORD` | — (mandatory in prod) | DB password; no default in prod. |
| `ADMIN_SESSION_SECRET` | — (mandatory in prod) | Signs `/admin` session cookies; a stable value keeps sessions valid across restarts. |
| `ARCHIVIST_DOMAIN` | `archivist.example.com` | FQDN Caddy issues the cert for (Option A). |
| `FORWARDED_ALLOW_IPS` | loopback + private ranges | Trusted proxy hosts for `X-Forwarded-For`. **Never `*`** — that trusts a client-supplied header and lets a spoofed IP evade the admin throttle. |
| `ENTRY_TOKEN_CAP` | `2000` | Max tokens per entry. |
| `FRAGMENT_TOKEN_CAP` | `500` | Max tokens per fragment. |
| `COMPACTION_FRAGMENT_THRESHOLD` | `3` | Fragments before `compaction_due`. |
| `HARD_LIMIT_FRAGMENTS` | `10` | Pending fragments before append backpressure. |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per-key request cap → `RATE_LIMITED`. |
| `ENABLE_HYBRID_SEARCH` | `true` | Pgvector hybrid retrieval (lexical + vector, RRF-merged); on by default, set `false` for pure lexical. |
| `NEAR_DUPLICATE_THRESHOLD` | `0.80` | Cosine similarity above which `memory_upsert` flags a keyword as a near-duplicate. |
| `STALE_REPORT_DAYS` | `90` | Entry age (days) after which it is reported as stale. |

## Option A — dedicated host

Expose only SSH and the web ports, then bring the whole stack up:

```bash
ufw allow 22/tcp     # SSH
ufw allow 80/tcp     # ACME challenge + HTTP→HTTPS redirect
ufw allow 443/tcp    # HTTPS
ufw --force enable

docker compose -f docker-compose.prod.yml up -d --build
```

The api container runs `alembic upgrade head` on start (idempotent), then
serves; Caddy obtains the certificate on the first request to your domain.

## Option B — behind a shared reverse proxy

Use this when the VPS already runs an nginx/certbot that owns 80/443 for other
vhosts. Archivist runs **api + db only** (a second service can't bind 80/443)
and lets the existing nginx reach it over a shared Docker network.

1. **Drop Caddy from the stack on the server.** Remove the `caddy` service from
   `docker-compose.prod.yml`, then bring up only api + db:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build db api
   ```
2. **Put api on the proxy's Docker network** so nginx resolves it by container
   name durably. Declare the proxy's network as `external` in the compose and
   add it to `api.networks`; the api container is then reachable as
   `archivist-api-1:8000`.
3. **Issue a certificate** with the shared certbot, using the webroot the
   existing nginx serves for ACME:
   ```bash
   certbot certonly --webroot -w /root/certbot/www -d archivist.example.com
   ```
4. **Add an nginx vhost** (back up `default.conf` first, then
   `nginx -t && nginx -s reload`): 80 → 301 to https; 443 `ssl` →
   `proxy_pass http://archivist-api-1:8000`. The MCP endpoint is long-lived SSE,
   so under `location /mcp` set `proxy_buffering off` and generous read/send
   timeouts (e.g. 3600s), and forward the caller identity:
   ```nginx
   proxy_set_header Host              $host;
   proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
   proxy_set_header X-Forwarded-Proto $scheme;
   client_max_body_size 16m;
   ```
   Set `FORWARDED_ALLOW_IPS` in `.env` to the proxy/private range so the app
   trusts that `X-Forwarded-For` (never `*`).
5. **Ship the code.** If the server has no git clone, push a snapshot over SSH:
   ```bash
   tar czf - --exclude .git --exclude .venv . \
     | ssh <vps> 'mkdir -p /opt/archivist && tar xzf - -C /opt/archivist'
   ```
   Then run the compose commands above from `/opt/archivist`.

TLS, cert renewal, and the vhost are owned by the shared nginx/certbot and are
independent of the app rebuild — a later `docker compose … up -d --build api`
never touches them.

## Verify

```bash
curl -I http://<domain>        # → 301/308 redirect to https://
curl https://<domain>/healthz  # → {"status":"ok","db":"ok"} over valid TLS
```

The MCP endpoint is served at `/mcp/` (**with** the trailing slash — `/mcp`
answers 307). Unauthenticated it returns 401; with a valid Bearer token an
`initialize` call returns 200.

## Administration

Run every command below on the VPS, from the directory holding
`docker-compose.prod.yml`.

### Teams and keys

Archivist is team-scoped: every key belongs to exactly one **team**, and a
team's archive is only reachable through a key issued for that team (see
[../SECURITY.md](../SECURITY.md#team-tenancy)). A key is shared by every member
of the team — there is no per-person key.

Create a team once (first onboarding for that team):

```bash
docker compose -f docker-compose.prod.yml exec api \
  python scripts/create_api_key.py team-create <team-name>
```

Issue a key for a member. The plaintext key is printed **once** — copy it and
hand it over out-of-band; only its SHA-256 hash is stored. `--team` accepts the
numeric id or the name; `--label` identifies the member/device (e.g.
`"alice/laptop"`) for audit, not authorization:

```bash
docker compose -f docker-compose.prod.yml exec api \
  python scripts/create_api_key.py key-issue --team <id|name> --label "<member/laptop>"
```

Hand the developer just two values — the server URL and the issued key. They run
`npx archivist-connect --server <url> --key <arc_...>` to wire up their agent in
one step (see [agent commands](agent-commands.md)).

List keys (and their team) to find an id to revoke:

```bash
docker compose -f docker-compose.prod.yml exec db \
  psql -U archivist -d archivist \
  -c "SELECT id, team_id, owner_name, revoked, created_at, last_used_at FROM api_keys ORDER BY id;"
```

Revoke a key — it stops working on its next request:

```bash
docker compose -f docker-compose.prod.yml exec api \
  python scripts/create_api_key.py key-revoke --key-id <id>
```

Rotate a key (issue a replacement on the **same team**, so the archive is
untouched). Pass `--revoke-old` to also revoke every previously-active key on
that team once clients have switched, or omit it for a grace window:

```bash
docker compose -f docker-compose.prod.yml exec api \
  python scripts/create_api_key.py key-rotate --team <id|name> --label "<label>" --revoke-old
```

> In local development, `make team NAME=<name>` and
> `make apikey TEAM=<id|name> LABEL="<label>"` wrap the same script.

### Admin console

Browse to `https://<host>/admin` and sign in with a team API key (the key **is**
the login credential — same validation as the MCP path). From there you can
inspect a team's archive — projects → entries → entry detail (content, pending
fragments, version history) — and delete or run a naïve compaction on an entry.
All views and actions are scoped to the signed-in team's own data.

The session cookie is `https_only`, so the console must be reached over HTTPS
(the front proxy provides TLS in production). Set `ADMIN_SESSION_SECRET` in
`.env` so admin sessions survive a restart.

## Backups

`scripts/backup.sh` dumps the database in `pg_dump` custom format to
`/var/backups/archivist/arc_YYYYMMDD.dump` and prunes dumps older than 14 days.
It is idempotent (re-running the same day overwrites that day's dump) and exits
non-zero on failure so cron surfaces the error. **Dumps contain plaintext data —
protect the backup directory like the database itself** (see
[../SECURITY.md](../SECURITY.md)).

Run one manually to verify the stack:

```bash
cd /opt/archivist && ./scripts/backup.sh
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore --list < /var/backups/archivist/arc_$(date -u +%Y%m%d).dump   # sanity check
```

Schedule it daily at **03:00 UTC** via the operator's crontab (`crontab -e`).
Cron has a minimal `PATH`, so `cd` into the repo first and keep `docker`
reachable:

```cron
# Archivist: daily DB backup at 03:00 UTC (14-day retention)
0 3 * * * cd /opt/archivist && ./scripts/backup.sh >> /var/log/archivist-backup.log 2>&1
```

Tunables are environment variables with prod defaults: `BACKUP_DIR`,
`RETENTION_DAYS`, `POSTGRES_USER`, `POSTGRES_DB`, `COMPOSE_FILE`.

### Restore

`scripts/restore.sh <dumpfile>` restores a dump into `$POSTGRES_DB`. It is
**destructive** (drops and recreates the schema in one transaction —
all-or-nothing), so it asks you to type the database name to confirm (set
`FORCE=1` to skip the prompt in automation):

```bash
./scripts/restore.sh /var/backups/archivist/arc_20260702.dump
```

It works both onto an empty database and onto the schema the api container
auto-creates on boot (`alembic upgrade head`), so a full disaster-recovery
restore needs no manual "empty the DB first" step — bring the stack up and run
it.

## See also

- [../RUNBOOK.md](../RUNBOOK.md) — incident response (down, disk full, key
  compromise, restore, upgrade)
- [architecture.md](architecture.md) — components and the network model
- [../SECURITY.md](../SECURITY.md) — threat model and controls
