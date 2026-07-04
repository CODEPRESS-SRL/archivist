#!/usr/bin/env bash
#
# Restore an Archivist Postgres backup.
#
# Usage: ./scripts/restore.sh <dumpfile>
#
# Reads a pg_dump custom-format file (as produced by backup.sh) and restores it
# with pg_restore into $POSTGRES_DB. Restoring is destructive (it drops and
# recreates the schema), so it asks for interactive confirmation (type the db
# name) unless FORCE=1 is set. Works both onto an empty database and onto the
# schema the prod `api` container auto-creates on boot (alembic upgrade head),
# so a disaster-recovery restore needs no manual "empty the DB first" step.
#
# Overridable via env (same defaults as backup.sh):
#   COMPOSE_FILE, DB_EXEC, POSTGRES_USER, POSTGRES_DB
#   FORCE=1   skip the confirmation prompt (for automated / tested restores)
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DB_EXEC="${DB_EXEC:-docker compose -f ${COMPOSE_FILE} exec -T db}"
POSTGRES_USER="${POSTGRES_USER:-archivist}"
POSTGRES_DB="${POSTGRES_DB:-archivist}"

dump="${1:-}"
if [ -z "${dump}" ]; then
    echo "usage: $0 <dumpfile>" >&2
    exit 2
fi
if [ ! -f "${dump}" ]; then
    echo "restore: dump file not found: ${dump}" >&2
    exit 2
fi

if [ "${FORCE:-}" != "1" ]; then
    printf 'This OVERWRITES database "%s" with %s.\n' "${POSTGRES_DB}" "${dump}" >&2
    printf 'Type the database name to confirm: ' >&2
    read -r reply
    if [ "${reply}" != "${POSTGRES_DB}" ]; then
        echo "restore: aborted (confirmation did not match)." >&2
        exit 1
    fi
fi

# --single-transaction: all-or-nothing (implies --exit-on-error) — a failure
#   rolls back and returns non-zero, leaving the target untouched (correct exit
#   codes; never a half-restored DB).
# --clean --if-exists: drop existing objects first, so the restore also succeeds
#   onto a non-empty target such as the auto-migrated prod schema; the drops are
#   harmless no-ops on a truly empty database.
# --no-owner: the dump's role may not exist on the restore target.
# shellcheck disable=SC2086  # DB_EXEC is a command prefix; word-splitting is intended.
${DB_EXEC} pg_restore --no-owner --clean --if-exists --single-transaction \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < "${dump}"

echo "restore: ${dump} restored into ${POSTGRES_DB}"
