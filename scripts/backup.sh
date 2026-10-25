#!/usr/bin/env bash
#
# Daily Postgres backup for Archivist.
#
# Dumps the database in pg_dump custom format (-Fc: compressed, restorable with
# pg_restore) to $BACKUP_DIR/arc_YYYYMMDD.dump, then prunes dumps older than
# $RETENTION_DAYS days. Idempotent: re-running on the same day overwrites that
# day's dump. Exits non-zero if the dump fails, so cron surfaces the error.
#
# Cron (03:00 UTC daily) — see README "Backups":
#   0 3 * * * cd /opt/archivist && ./scripts/backup.sh >> /var/log/archivist-backup.log 2>&1
#
# Overridable via env (defaults target the prod stack):
#   COMPOSE_FILE    compose file for the db service   (docker-compose.prod.yml)
#   DB_EXEC         command prefix running the psql-family tools against the db
#                   (default: docker compose -f $COMPOSE_FILE exec -T db)
#   BACKUP_DIR      directory dumps are written to     (/var/backups/archivist)
#   RETENTION_DAYS  age in days past which to prune     (14)
#   POSTGRES_USER   db role                             (archivist)
#   POSTGRES_DB     db name                             (archivist)
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DB_EXEC="${DB_EXEC:-docker compose -f ${COMPOSE_FILE} exec -T db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/archivist}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
POSTGRES_USER="${POSTGRES_USER:-archivist}"
POSTGRES_DB="${POSTGRES_DB:-archivist}"

mkdir -p "${BACKUP_DIR}"

stamp="$(date -u +%Y%m%d)"
dest="${BACKUP_DIR}/arc_${stamp}.dump"

# Dump to a temp file first, then atomically move into place, so an interrupted
# run never leaves a truncated arc_*.dump that looks like a valid backup.
tmp="$(mktemp "${BACKUP_DIR}/.arc_${stamp}.XXXXXX")"
trap 'rm -f "${tmp}"' EXIT

# shellcheck disable=SC2086  # DB_EXEC is a command prefix; word-splitting is intended.
${DB_EXEC} pg_dump -Fc -U "${POSTGRES_USER}" "${POSTGRES_DB}" > "${tmp}"
mv "${tmp}" "${dest}"

# Retention: drop dumps older than RETENTION_DAYS days (by mtime).
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'arc_*.dump' -mtime +"${RETENTION_DAYS}" -delete

echo "backup: wrote ${dest} ($(du -h "${dest}" | cut -f1)); pruned dumps older than ${RETENTION_DAYS}d"
