#!/usr/bin/env bash
# Copia de seguridad nocturna de la base de datos.
#
# Corre en el HOST (cron del LXC), no dentro del bot: una copia que solo se hace cuando la
# aplicación está sana es justo la que falta el día que hace falta.
#
#   0 3 * * *  cd /opt/agenda-escolar-bot && ./scripts/backup.sh >> /var/log/agenda-backup.log 2>&1
#
# Restaurar:  gunzip -c data/backups/agenda-YYYYmmdd-HHMM.sql.gz | \
#               docker compose exec -T postgres psql -U agenda -d agenda

set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-data/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
POSTGRES_USER="${POSTGRES_USER:-agenda}"
POSTGRES_DB="${POSTGRES_DB:-agenda}"
COMPOSE="${COMPOSE:-docker compose}"

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M)"
target="$BACKUP_DIR/agenda-$stamp.sql.gz"

# --clean --if-exists deja el volcado listo para restaurar sobre una base existente.
$COMPOSE exec -T postgres pg_dump \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  --clean --if-exists \
  | gzip > "$target.tmp"

mv "$target.tmp" "$target"

# Un volcado vacío es peor que ninguno: falla ruidosamente si salió sospechosamente pequeño.
size=$(wc -c < "$target")
if [ "$size" -lt 1024 ]; then
  echo "ERROR: el volcado $target pesa $size bytes; algo salió mal" >&2
  exit 1
fi

find "$BACKUP_DIR" -name 'agenda-*.sql.gz' -type f -mtime "+$KEEP_DAYS" -delete

echo "$(date -Is) backup ok: $target ($size bytes), rotación $KEEP_DAYS días"
