# Archivist — Operations Runbook

Symptom → action for the operator on call. Run every command on the VPS, from
the directory holding `docker-compose.prod.yml` (e.g. `/opt/archivist`). Setup,
onboarding, administration, and backup/restore basics live in
[docs/self-hosting.md](docs/self-hosting.md); this file is for things going
wrong.

Quick orientation before anything else:

```bash
docker compose -f docker-compose.prod.yml ps          # what is up / restarting / exited
docker compose -f docker-compose.prod.yml logs --tail=100 api   # recent app + audit lines (JSON)
```

The API emits one structured JSON log line per MCP call (`tool`, `project`,
`keyword`, `key_owner`, `duration_ms`, `outcome`) — those are your audit trail.

---

## 1. Service down

**Symptom:** `curl https://<domain>/healthz` fails or hangs; clients cannot
connect; `docker compose ps` shows `api`, `db`, or `caddy` not `Up`.

**Action:**

1. Confirm what is down:
   ```bash
   docker compose -f docker-compose.prod.yml ps
   curl -sS https://<domain>/healthz        # want {"status":"ok","db":"ok"}
   ```
2. Read the logs of the unhealthy service:
   ```bash
   docker compose -f docker-compose.prod.yml logs --tail=200 api
   docker compose -f docker-compose.prod.yml logs --tail=200 db
   docker compose -f docker-compose.prod.yml logs --tail=200 caddy   # dedicated-host deploy only
   ```
   - `healthz` returns `503` with `"db":"error"` → the db is the problem, go to
     scenario 2.
   - TLS/cert errors → check the front proxy. On a **dedicated host** that is
     the `caddy` container (DNS A record must point here, ports 80/443 open per
     `ufw status`; certs persist in the `caddy_data` volume, so a bounce does
     not re-hit Let's Encrypt). In the **shared production deploy** there is no
     `caddy` container — TLS is terminated by the host's nginx/certbot; check
     those on the host (`nginx -t`, `docker logs <nginx-container>`,
     `certbot certificates`), not this compose project.
3. Restart the stack (config and data survive; volumes are not touched):
   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```
   To force-recreate a single wedged service:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --force-recreate api
   ```
4. Re-verify: `curl https://<domain>/healthz` → `{"status":"ok","db":"ok"}`.

If the host itself is out of resources, check `df -h` (disk — see scenario 2)
and `free -m` / `docker stats` (memory) before restarting in a loop.

---

## 2. Database full / disk full

**Symptom:** writes fail; `healthz` reports `"db":"error"`; Postgres logs show
`could not extend file` / `No space left on device`; `df -h` near 100%.

**Action:**

1. Find what is consuming disk:
   ```bash
   df -h
   docker system df                                    # images / containers / volumes
   du -sh /var/backups/archivist/* 2>/dev/null | sort -h  # backup dumps
   ```
2. Reclaim space safely, cheapest first:
   - Prune stale backups (the daily job keeps 14 days; old dumps may remain if
     cron was off):
     ```bash
     find /var/backups/archivist -name 'arc_*.dump' -mtime +14 -delete
     ```
   - Prune dangling Docker images/build cache (does **not** touch named volumes
     like `pgdata`):
     ```bash
     docker image prune -f
     docker builder prune -f
     ```
   - Rotate/truncate oversized logs (`/var/log/archivist-backup.log`, Docker
     json logs under `/var/lib/docker/containers`).
3. If Postgres data itself is the bulk, inspect table sizes:
   ```bash
   docker compose -f docker-compose.prod.yml exec db \
     psql -U archivist -d archivist \
     -c "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC;"
   ```
   `memory_fragments` and `memory_history` grow over time; a `VACUUM (FULL,
   ANALYZE)` reclaims bloat but needs free space and takes a lock — run during a
   quiet window:
   ```bash
   docker compose -f docker-compose.prod.yml exec db \
     psql -U archivist -d archivist -c "VACUUM (ANALYZE);"
   ```
4. Once disk is freed, the db recovers on its own; confirm with
   `curl https://<domain>/healthz`. If it stays down, restart per scenario 1.

Longer term: grow the VPS volume, or move `/var/backups/archivist` to separate
storage.

---

## 3. Compromised API key

**Symptom:** a key leaked (committed to a repo, pasted in chat, lost laptop) or
the audit log shows unexpected `key_owner` activity.

**Action:** revoke immediately — revocation takes effect on the key's next
request.

Because a key is shared by an entire team (see `SECURITY.md` — "Team
tenancy"), a leaked key exposes that team's whole archive until it is
revoked — there is no way to disable access for just the leaker while the
rest of the team keeps working.

1. Identify the key id and its team:
   ```bash
   docker compose -f docker-compose.prod.yml exec db \
     psql -U archivist -d archivist \
     -c "SELECT id, team_id, owner_name, revoked, created_at, last_used_at FROM api_keys ORDER BY id;"
   ```
2. Revoke it:
   ```bash
   docker compose -f docker-compose.prod.yml exec api \
     python scripts/create_api_key.py key-revoke --key-id <id>
   ```
3. Issue a replacement on the **same team** and hand it over out-of-band:
   ```bash
   docker compose -f docker-compose.prod.yml exec api \
     python scripts/create_api_key.py key-issue --team <id|name> --label "<member/laptop>"
   ```
   For a full rotation in one step (issue new + revoke every other
   previously-active key on that team), use:
   ```bash
   docker compose -f docker-compose.prod.yml exec api \
     python scripts/create_api_key.py key-rotate --team <id|name> --label "<label>" --revoke-old
   ```
4. Assess exposure from the audit log — which `project`/`keyword` the key
   touched while compromised (`key_owner` is the compromised key's `--label`,
   e.g. `"alice/laptop"` — not the team name, since a key is shared by the
   whole team but each key can still carry its own label):
   ```bash
   docker compose -f docker-compose.prod.yml logs api | grep '"key_owner":"<label>"'
   ```

Keys are stored only as SHA-256 hashes, so the plaintext cannot be recovered or
re-derived from the database — a leaked key can only be neutralised by revoking
it. See `SECURITY.md`.

---

## 4. Restore from backup

**Symptom:** data loss or corruption; a bad migration; the db volume is gone
and you are rebuilding.

**Action:** `scripts/restore.sh` restores a `pg_dump` custom-format file into
the running db. It is **destructive** (drops and recreates the schema in one
transaction) and prompts you to type the database name to confirm.

1. Pick the dump to restore (backups live in `/var/backups/archivist/`):
   ```bash
   ls -lt /var/backups/archivist/arc_*.dump
   ```
2. Make sure the stack is up (restore targets the running `db`; it works both on
   an empty db and on the auto-migrated schema, so no "empty it first" step):
   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```
3. Restore — you will be asked to type the db name (`archivist`) to confirm:
   ```bash
   ./scripts/restore.sh /var/backups/archivist/arc_YYYYMMDD.dump
   ```
   For an unattended restore, set `FORCE=1` to skip the prompt.
4. Verify the data is back:
   ```bash
   curl https://<domain>/healthz
   docker compose -f docker-compose.prod.yml exec db \
     psql -U archivist -d archivist -c "SELECT count(*) FROM memories;"
   ```

Full details and the backup schedule are in the
[Backups](docs/self-hosting.md#backups) section of the self-hosting guide.

---

## 5. Version upgrade

**Symptom:** deploying a new Archivist version (new image, schema migration, or
config change).

**Action:** the `api` container runs `alembic upgrade head` on start (idempotent
and safe to repeat), so a rebuild self-migrates.

1. **Back up first** — always take a fresh dump before upgrading:
   ```bash
   ./scripts/backup.sh
   ```
2. Get the new code onto the server. The production host has **no git clone**
   (it was provisioned by shipping a snapshot, not `git pull`), so push the
   working tree over SSH from your machine — this overwrites `/opt/archivist`
   with the new sources but leaves `.env` and the `pgdata`/backup volumes
   untouched:
   ```bash
   tar czf - --exclude .git --exclude .venv . \
     | ssh <vps> 'tar xzf - -C /opt/archivist'
   ```
   (If a given host *does* have a clone, `git pull` / `git checkout <tag>` there
   is equivalent.)
3. On the server, from `/opt/archivist`, rebuild and roll **only api** (the
   shared-proxy deployment runs api + db, no Caddy); the api applies pending
   migrations on boot:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build api
   ```
4. Watch the migration run and the app come up:
   ```bash
   docker compose -f docker-compose.prod.yml logs -f api    # look for "alembic upgrade head" then uvicorn startup
   ```
5. Verify:
   ```bash
   curl https://<domain>/healthz        # {"status":"ok","db":"ok"}
   ```

The front proxy (shared nginx + certbot, or Caddy on a dedicated host), its TLS
cert, and the vhost are independent of the app rebuild — this upgrade does not
touch them.

**Rollback:** if the new version misbehaves, ship the previous snapshot (or
`git checkout <prev-tag>` where a clone exists) → `up -d --build api`. If a
migration corrupted data or a downgrade is not clean, restore the pre-upgrade
dump from step 1 using scenario 4.
