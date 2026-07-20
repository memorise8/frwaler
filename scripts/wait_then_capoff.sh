#!/bin/bash
# Waits until promote_all (PID=$1) exits, then launches cap-uncapped rerun.
set -u
ROOT="/data_raid/ruci_workspace/frwaler_job"
WAIT_PID="$1"
TRANSITION_LOG="$ROOT/data/audit/logs/promote_transition.log"
exec >> "$TRANSITION_LOG" 2>&1
echo "[$(date '+%F %T')] waiting for promote_all PID=$WAIT_PID to exit..."
while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 60
done
echo "[$(date '+%F %T')] PID=$WAIT_PID exited. starting cap-uncap promote."
cd "$ROOT" || exit 1
BACKUP="data/audit/promoted_all.cap500.$(date +%Y%m%d_%H%M%S).jsonl"
if [ -f data/audit/promoted_all.jsonl ]; then
    mv data/audit/promoted_all.jsonl "$BACKUP"
    echo "[$(date '+%F %T')] archived promoted_all.jsonl -> $BACKUP"
fi
TS=$(date +%Y%m%d_%H%M%S)
NEW_LOG="data/audit/logs/promote_all_capoff_${TS}.log"
nohup .venv/bin/python scripts/promote_all.py \
    --per-site-limit 30000 \
    --concurrent-sites 4 \
    --concurrent-pdfs 8 \
    > "$NEW_LOG" 2>&1 &
NEW_PID=$!
echo "[$(date '+%F %T')] launched cap-uncap promote PID=$NEW_PID log=$NEW_LOG (per-site-limit=30000)"

echo "[$(date '+%F %T')] now waiting for cap-uncap promote PID=$NEW_PID to exit..."
while kill -0 "$NEW_PID" 2>/dev/null; do
    sleep 60
done
echo "[$(date '+%F %T')] cap-uncap promote PID=$NEW_PID exited. starting QA accuracy."
QA_TS=$(date +%Y%m%d_%H%M%S)
QA_XLSX="data/audit/qa_crawl_accuracy_${QA_TS}.xlsx"
QA_LOG="data/audit/logs/qa_crawl_accuracy_${QA_TS}.log"
nohup .venv/bin/python scripts/qa_crawl_accuracy.py \
    --limit 500 \
    --xlsx "$QA_XLSX" \
    > "$QA_LOG" 2>&1 &
QA_PID=$!
echo "[$(date '+%F %T')] launched QA PID=$QA_PID log=$QA_LOG xlsx=$QA_XLSX"
