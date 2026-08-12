#!/bin/sh
# Dump the delivery PG and prove it restores into a disposable database.
set -eu
[ "${DELIVERY_BACKUP_CONFIRM:-}" = "DUMP_READONLY_RESTORE_EPHEMERAL" ] || {
  echo "refusing: set DELIVERY_BACKUP_CONFIRM=DUMP_READONLY_RESTORE_EPHEMERAL" >&2; exit 2;
}
[ -n "${BACKUP_DIR:-}" ] && [ -d "$BACKUP_DIR" ] || { echo "BACKUP_DIR must exist" >&2; exit 2; }
case "$(cd "$BACKUP_DIR" && pwd -P)" in /|"$HOME"|"$PWD") echo "unsafe BACKUP_DIR" >&2; exit 2;; esac

SOURCE_DB=${POSTGRES_DB:-libertree}
RESTORE_DB="libertree_restore_verify_$$"
DUMP="$BACKUP_DIR/libertree-$(date +%Y%m%dT%H%M%S).dump"
cd "$(dirname "$0")/.."
cleanup() { docker compose exec -T postgres dropdb -U "${POSTGRES_USER:-libertree}" --if-exists "$RESTORE_DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT HUP INT TERM

docker compose exec -T postgres pg_dump -Fc -U "${POSTGRES_USER:-libertree}" -d "$SOURCE_DB" > "$DUMP"
docker compose exec -T postgres createdb -U "${POSTGRES_USER:-libertree}" "$RESTORE_DB"
docker compose exec -T postgres pg_restore -U "${POSTGRES_USER:-libertree}" -d "$RESTORE_DB" --exit-on-error < "$DUMP"
for table in sites documents; do
  source=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "$SOURCE_DB" -c "SELECT count(*) FROM $table")
  restored=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "$RESTORE_DB" -c "SELECT count(*) FROM $table")
  [ "$source" = "$restored" ] || { echo "$table count mismatch" >&2; exit 1; }
done
for table in document_lang document_translations document_anomaly; do
  exists=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "$SOURCE_DB" -c "SELECT to_regclass('public.$table') IS NOT NULL")
  [ "$exists" = "t" ] || continue
  source=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "$SOURCE_DB" -c "SELECT count(*) FROM $table")
  restored=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "$RESTORE_DB" -c "SELECT count(*) FROM $table")
  [ "$source" = "$restored" ] || { echo "$table count mismatch" >&2; exit 1; }
done
src_sum=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "$SOURCE_DB" -c 'SELECT max(seq_id)||'"'|'"'||sum(seq_id) FROM documents')
dst_sum=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "$RESTORE_DB" -c 'SELECT max(seq_id)||'"'|'"'||sum(seq_id) FROM documents')
[ "$src_sum" = "$dst_sum" ] || { echo "seq_id checksum mismatch" >&2; exit 1; }
sha256sum "$DUMP" > "$DUMP.sha256"
echo "backup restore verification: PASS ($DUMP)"
