#!/usr/bin/env bash
#
# Backup pianificato e VERIFICATO del database di Archivist su *nova* (WSP-85).
#
# Dopo il taglio questo database e' l'UNICA copia delle scritture recenti di
# memoria: un backup che punta al compose sbagliato, si fida di un dump vuoto o
# fallisce in silenzio e' peggio di nessun backup, perche' lascia credere di
# essere coperti.
#
# Per giro, in ordine:
#
#   1. `scripts/backup.sh` (pg_dump -Fc, scrittura su file temporaneo + mv,
#      retention) con le impostazioni di *nova*: compose file nova, progetto
#      `archivist`, cartella /var/backups/archivist-nova, 14 giorni.
#   2. RIAPERTURA del dump con `pg_restore -l` dentro il container db: un dump
#      che non si legge, o che non elenca nessun oggetto, e' un giro FALLITO
#      (stessa classe del tar vuoto di WSP-28).
#   3. Mirror `rsync --delete` verso una seconda macchina. Anche l'offsite
#      irraggiungibile e' un fallimento: una copia che vive solo sulla macchina
#      che proteggiamo non protegge da niente.
#
# Il fallimento e' rumoroso (*nova* non ha un MTA): unit `failed`, `OnFailure=`
# che scrive ${STATE_DIR}/last-failure e logga a priorita' `err`. Il file
# ${STATE_DIR}/last-success porta l'istante dell'ultimo giro riuscito: se e'
# stantio il timer non sta partendo, caso che `OnFailure=` non puo' vedere.
#
# Schedulazione: archivist-backup.timer (04:40 UTC). Esecuzione manuale:
#   sudo systemctl start archivist-backup.service && journalctl -u archivist-backup -n 30
#
# Override via env (EnvironmentFile=-/etc/default/archivist-backup nella unit):
#   APP_DIR          cartella dello stack            (due livelli sopra questo file)
#   BACKUP_DIR       cartella dei backup locali      (/var/backups/archivist-nova)
#   RETENTION_DAYS   eta' oltre la quale si pota     (14)
#   OFFSITE_DEST     destinazione rsync, OBBLIGATORIA (es. archivistbackup@host:/)
#   OFFSITE_KEY      chiave SSH dedicata, confinata a rrsync
#                    (/root/.ssh/id_ed25519_archivist_backup)
#   STATE_DIR        stato per il segnale di allarme (/var/lib/archivist-backup)
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STATE_DIR="${STATE_DIR:-/var/lib/archivist-backup}"
OFFSITE_KEY="${OFFSITE_KEY:-/root/.ssh/id_ed25519_archivist_backup}"
LOCK_FILE="${LOCK_FILE:-/var/lock/archivist-backup.lock}"

# Le variabili che `scripts/backup.sh` legge. Il DB_EXEC porta sempre progetto e
# file: un comando compose senza questi due flag, da una cartella diversa,
# parlerebbe con lo stack sbagliato (o con nessuno).
export COMPOSE_FILE="docker-compose.nova.yml"
export DB_EXEC="docker compose -p archivist -f docker-compose.nova.yml exec -T db"
export BACKUP_DIR="${BACKUP_DIR:-/var/backups/archivist-nova}"
export RETENTION_DAYS="${RETENTION_DAYS:-14}"

# Un solo giro alla volta.
exec 9>"${LOCK_FILE}"
flock -n 9 || { echo "backup: un altro giro e' gia' in corso (${LOCK_FILE})" >&2; exit 1; }

install -d -m 700 "${BACKUP_DIR}"
install -d -m 755 "${STATE_DIR}"

# I percorsi relativi del compose file si risolvono da qui.
cd "${APP_DIR}"

stamp="$(date -u +%Y%m%d)"
dump="${BACKUP_DIR}/arc_${stamp}.dump"

./scripts/backup.sh

# Riapertura: `pg_restore -l` senza argomento legge il dump da stdin (non
# accetta "-" come nome di file) ed e' eseguito DENTRO il container db, dove
# esiste la versione giusta del client. Un file mancante, vuoto o corrotto
# fa fallire il giro.
[ -s "${dump}" ] || { echo "backup: ${dump} assente o vuoto" >&2; exit 1; }
# shellcheck disable=SC2086  # DB_EXEC e' un prefisso di comando: lo split e' voluto.
toc="$(${DB_EXEC} pg_restore -l < "${dump}")" || {
	echo "backup: pg_restore -l non riesce a leggere ${dump}" >&2
	exit 1
}
voci="$(printf '%s\n' "${toc}" | grep -c -v -e '^;' -e '^[[:space:]]*$' || true)"
if [ "${voci}" -lt 1 ]; then
	echo "backup: ${dump} non elenca nessun oggetto (dump vuoto)" >&2
	exit 1
fi

# Offsite. Il ricevente NON e' root (chiave confinata a `rrsync`, nessuna
# shell): --no-o --no-g perche' rsync non puo' chiedergli un chown. BatchMode:
# un problema di chiave e' un fallimento subito, non un prompt appeso in
# systemd. Il mirror esclude i file temporanei di `backup.sh` (.arc_*) e ogni
# .partial: un file a meta' non deve mai sembrare un backup.
OFFSITE_DEST="${OFFSITE_DEST:?OFFSITE_DEST e' obbligatoria (es. utente@host:/): senza copia offsite il giro non e' un backup}"
rsync -a --no-o --no-g --delete --exclude '.arc_*' --exclude '*.partial' \
	-e "ssh -i ${OFFSITE_KEY} -o BatchMode=yes -o ConnectTimeout=30" \
	"${BACKUP_DIR}/" "${OFFSITE_DEST}"

date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE_DIR}/last-success"
rm -f "${STATE_DIR}/last-failure"

echo "backup: ${dump} ok (${voci} oggetti nel sommario), specchiato su ${OFFSITE_DEST%%:*}; potati i dump oltre ${RETENTION_DAYS}g"
