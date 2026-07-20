#!/bin/bash
# Waits until the main promote chain (v2 watcher) exits, then launches
# promote_all on the 446 "registered-but-never-processed" sites in batches.
set -u
ROOT="/data_raid/ruci_workspace/frwaler_job"
WAIT_PID="$1"
LOG="$ROOT/data/audit/logs/promote_unprocessed_chain.log"
SITES_FILE="$ROOT/data/audit/unprocessed_sites.txt"
BATCH_SIZE=100
PER_SITE_LIMIT=30000
CONCURRENT_SITES=4
CONCURRENT_PDFS=8

exec >> "$LOG" 2>&1
echo "==============================================================="
echo "[$(date '+%F %T')] UNPROCESSED watcher: waiting for PID=$WAIT_PID"
echo "[$(date '+%F %T')] config: BATCH_SIZE=$BATCH_SIZE LIMIT=$PER_SITE_LIMIT"

while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 60
done
echo "[$(date '+%F %T')] PID=$WAIT_PID exited. starting unprocessed-sites promote."

cd "$ROOT" || exit 1

TOTAL=$(wc -l < "$SITES_FILE" 2>/dev/null || echo 0)
echo "[$(date '+%F %T')] unprocessed sites: $TOTAL"

if [ "$TOTAL" -le 0 ]; then
    echo "[$(date '+%F %T')] no sites to process. exiting."
    exit 0
fi

OFFSET=0
BATCH_IDX=0
while [ "$OFFSET" -lt "$TOTAL" ]; do
    BATCH_IDX=$((BATCH_IDX + 1))
    TS=$(date +%Y%m%d_%H%M%S)
    BATCH_LOG="data/audit/logs/promote_unprocessed_batch${BATCH_IDX}_${TS}.log"
    echo "[$(date '+%F %T')] >>> BATCH $BATCH_IDX offset=$OFFSET size=$BATCH_SIZE log=$BATCH_LOG"
    .venv/bin/python scripts/promote_all.py \
        --per-site-limit "$PER_SITE_LIMIT" \
        --concurrent-sites "$CONCURRENT_SITES" \
        --concurrent-pdfs "$CONCURRENT_PDFS" \
        --only-site-ids-file "$SITES_FILE" \
        --site-offset "$OFFSET" \
        --site-limit-count "$BATCH_SIZE" \
        > "$BATCH_LOG" 2>&1
    EXIT=$?
    echo "[$(date '+%F %T')] <<< BATCH $BATCH_IDX exit=$EXIT"
    OFFSET=$((OFFSET + BATCH_SIZE))
done

echo "[$(date '+%F %T')] all unprocessed batches done ($BATCH_IDX batches)."
echo "[$(date '+%F %T')] Running QA #3."
QA_TS=$(date +%Y%m%d_%H%M%S)
QA_XLSX="data/audit/qa_crawl_accuracy_unprocessed_${QA_TS}.xlsx"
QA_LOG="data/audit/logs/qa_crawl_accuracy_unprocessed_${QA_TS}.log"
.venv/bin/python scripts/qa_crawl_accuracy.py \
    --limit 500 --xlsx "$QA_XLSX" > "$QA_LOG" 2>&1
echo "[$(date '+%F %T')] QA #3 done. xlsx=$QA_XLSX"
echo "[$(date '+%F %T')] UNPROCESSED chain complete."
