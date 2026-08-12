#!/bin/sh
# Apply Delivery schema only to an explicitly named snapshot/rehearsal PostgreSQL.
set -eu
[ "${SNAPSHOT_REHEARSAL_CONFIRM:-}" = "APPLY_TO_DISPOSABLE_SNAPSHOT" ] || { echo "confirmation token required" >&2;exit 2; }
[ -n "${SOURCE_READONLY_DSN:-}" ] && [ -n "${SNAPSHOT_TARGET_DSN:-}" ] || { echo "source and target DSNs required" >&2;exit 2; }
[ "$SOURCE_READONLY_DSN" != "$SNAPSHOT_TARGET_DSN" ] || { echo "source and target must differ" >&2;exit 2; }
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
IMAGE=${DELIVERY_SCHEMA_IMAGE:-libertree-delivery-be:schema-rehearsal}
docker build -f "$ROOT/delivery/Dockerfile.be" -t "$IMAGE" "$ROOT" >/dev/null
inspect() { docker run --rm --network host -e DSN="$1" "$IMAGE" python -c '
import os,psycopg
with psycopg.connect(os.environ["DSN"]) as c:
 print(c.execute("select current_database(),count(*) from documents").fetchone()[0])
'; }
SOURCE_NAME=$(inspect "$SOURCE_READONLY_DSN")
TARGET_NAME=$(inspect "$SNAPSHOT_TARGET_DSN")
case "$TARGET_NAME" in *snapshot*|*rehearsal*|*staging*) ;; *) echo "target database name must contain snapshot, rehearsal, or staging" >&2;exit 2;; esac
case "$SOURCE_NAME" in "$TARGET_NAME") echo "database identity collision" >&2;exit 2;; esac

# Force the source transaction read-only and collect only aggregate evidence.
SOURCE_COUNTS=$(docker run --rm --network host -e DSN="$SOURCE_READONLY_DSN" "$IMAGE" python -c '
import os,psycopg
with psycopg.connect(os.environ["DSN"]) as c:
 c.execute("set transaction read only")
 r=c.execute("select (select count(*) from sites),(select count(*) from documents),coalesce(max(seq_id),0) from documents").fetchone()
 print("|".join(map(str,r)))
')
TARGET_COUNTS=$(docker run --rm --network host -e DSN="$SNAPSHOT_TARGET_DSN" "$IMAGE" python -c '
import os,psycopg
from delivery.db.schema import init_delivery_schema
with psycopg.connect(os.environ["DSN"]) as c:
 before=c.execute("select (select count(*) from sites),(select count(*) from documents),coalesce(max(seq_id),0) from documents").fetchone()
 init_delivery_schema(c)
 after=c.execute("select (select count(*) from sites),(select count(*) from documents),coalesce(max(seq_id),0) from documents").fetchone()
 assert before==after,"core data changed during schema apply"
 required={"crawl_jobs","crawl_job_logs","crawl_schedules","translation_batches","translation_jobs","translation_job_attempts","document_summaries","document_summary_quality"}
 found={r[0] for r in c.execute("select tablename from pg_tables where schemaname=current_schema()")}
 assert required<=found,"delivery tables missing"
 print("|".join(map(str,after)))
')
[ "$SOURCE_COUNTS" = "$TARGET_COUNTS" ] || { echo "source/snapshot aggregate mismatch" >&2;exit 1; }
echo "snapshot schema rehearsal: PASS (sites|documents|max_seq=$TARGET_COUNTS)"
