#!/bin/bash
# Waits until current bulk_summarize PID exits, then iterates
# through 'truly done' sites (sites_finalized.csv category='진짜끝(100%)')
# and summarizes each one via Gemma on port 11436.
set -u
ROOT="/data_raid/ruci_workspace/frwaler_job"
WAIT_PID="$1"
LOG="$ROOT/data/audit/logs/summarize_done_chain.log"
DONE_CSV="$ROOT/data/audit/sites_finalized.csv"
PORT=11436

exec >> "$LOG" 2>&1
echo "==============================================================="
echo "[$(date '+%F %T')] CHAIN: wait for bulk_summarize PID=$WAIT_PID"
while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 60
done
echo "[$(date '+%F %T')] PID=$WAIT_PID exited. starting done-sites chain."

cd "$ROOT" || exit 1

# Extract truly-done site_ids (category exact match) — handle UTF-8 BOM
SITE_IDS=$(.venv/bin/python -c "
import csv
with open('$DONE_CSV', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        if row['category'] == '진짜끝(100%)':
            print(row['site_id'])
")

TOTAL=$(echo "$SITE_IDS" | grep -c .)
echo "[$(date '+%F %T')] truly-done sites: $TOTAL"

IDX=0
for sid in $SITE_IDS; do
    IDX=$((IDX + 1))
    echo "[$(date '+%F %T')] ($IDX/$TOTAL) >>> summarize site=$sid"
    TS=$(date +%Y%m%d_%H%M%S)
    SUB_LOG="data/audit/logs/bulk_summarize_${sid}_${TS}.log"
    .venv/bin/python scripts/bulk_summarize_gemma.py \
        --site "$sid" \
        --port "$PORT" \
        --commit-every 20 \
        > "$SUB_LOG" 2>&1
    EXIT=$?
    echo "[$(date '+%F %T')] ($IDX/$TOTAL) <<< $sid exit=$EXIT log=$SUB_LOG"
done

echo "[$(date '+%F %T')] CHAIN done. Processed $TOTAL truly-done sites."
