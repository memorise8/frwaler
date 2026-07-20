#!/bin/bash
# Waits until current promote_all (PID=$1) exits, then runs cap-uncap promote
# in fixed-size BATCHES (memory-bounded). After all batches complete, kicks
# QA accuracy. Each batch is a fresh Python process — full memory reset.
set -u
ROOT="/data_raid/ruci_workspace/frwaler_job"
WAIT_PID="$1"
TRANSITION_LOG="$ROOT/data/audit/logs/promote_transition.log"
BATCH_SIZE=250
PER_SITE_LIMIT=30000
CONCURRENT_SITES=4
CONCURRENT_PDFS=8

exec >> "$TRANSITION_LOG" 2>&1
echo "==============================================================="
echo "[$(date '+%F %T')] BATCH watcher: waiting for promote PID=$WAIT_PID"
echo "[$(date '+%F %T')] config: BATCH_SIZE=$BATCH_SIZE PER_SITE_LIMIT=$PER_SITE_LIMIT"
while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 60
done
echo "[$(date '+%F %T')] PID=$WAIT_PID exited."

cd "$ROOT" || exit 1

# Archive cap-500 log so cap-uncap promote starts from a fresh canvas
if [ -f data/audit/promoted_all.jsonl ]; then
    BACKUP="data/audit/promoted_all.cap500.$(date +%Y%m%d_%H%M%S).jsonl"
    mv data/audit/promoted_all.jsonl "$BACKUP"
    echo "[$(date '+%F %T')] archived promoted_all.jsonl -> $BACKUP"
fi

# Count total sites (sum of unique entries in the three source dirs)
TOTAL=$(.venv/bin/python -c "
import json, os
seen=set()
for d in ('data/audit/sample_runs','data/audit/codex_required_runs','data/audit/claude_required_runs'):
    if not os.path.isdir(d): continue
    for f in os.listdir(d):
        if not f.endswith('.json'): continue
        try:
            j=json.load(open(os.path.join(d,f)))
            sid=j.get('site_id_generated') or j.get('site_id')
            if sid: seen.add(sid)
        except: pass
print(len(seen))
")
echo "[$(date '+%F %T')] Total unique sites detected: $TOTAL"

if [ -z "$TOTAL" ] || [ "$TOTAL" -le 0 ]; then
    echo "[$(date '+%F %T')] ERROR: site count is 0, aborting." >&2
    exit 1
fi

OFFSET=0
BATCH_IDX=0
while [ "$OFFSET" -lt "$TOTAL" ]; do
    BATCH_IDX=$((BATCH_IDX + 1))
    TS=$(date +%Y%m%d_%H%M%S)
    BATCH_LOG="data/audit/logs/promote_all_capoff_batch${BATCH_IDX}_${TS}.log"
    echo "[$(date '+%F %T')] >>> BATCH $BATCH_IDX  offset=$OFFSET  size=$BATCH_SIZE  log=$BATCH_LOG"
    .venv/bin/python scripts/promote_all.py \
        --per-site-limit "$PER_SITE_LIMIT" \
        --concurrent-sites "$CONCURRENT_SITES" \
        --concurrent-pdfs "$CONCURRENT_PDFS" \
        --site-offset "$OFFSET" \
        --site-limit-count "$BATCH_SIZE" \
        > "$BATCH_LOG" 2>&1
    EXIT=$?
    echo "[$(date '+%F %T')] <<< BATCH $BATCH_IDX exited code=$EXIT"
    if [ "$EXIT" -ne 0 ]; then
        echo "[$(date '+%F %T')] WARN: batch $BATCH_IDX exited non-zero — continuing to next batch"
    fi
    OFFSET=$((OFFSET + BATCH_SIZE))
done

echo "[$(date '+%F %T')] All $BATCH_IDX cap-30000 batches done. Running QA #1."
QA1_TS=$(date +%Y%m%d_%H%M%S)
QA1_XLSX="data/audit/qa_crawl_accuracy_cap30k_${QA1_TS}.xlsx"
QA1_LOG="data/audit/logs/qa_crawl_accuracy_cap30k_${QA1_TS}.log"
.venv/bin/python scripts/qa_crawl_accuracy.py \
    --limit 500 \
    --xlsx "$QA1_XLSX" \
    > "$QA1_LOG" 2>&1
echo "[$(date '+%F %T')] QA #1 done. xlsx=$QA1_XLSX"

# ================================================================
# Step: identify cap-reached sites (COUNT == 30000) and recollect
# at cap 100000 with batch processing again.
# ================================================================
RECOL_TS=$(date +%Y%m%d_%H%M%S)
CAP_REACHED_FILE="data/audit/cap_reached_${RECOL_TS}.txt"
.venv/bin/python -c "
import sqlite3
con = sqlite3.connect('data/libertree.db')
rows = con.execute('SELECT site_id, COUNT(*) FROM documents GROUP BY site_id HAVING COUNT(*) >= 30000 ORDER BY 2 DESC').fetchall()
with open('$CAP_REACHED_FILE','w') as f:
    for sid, n in rows:
        f.write(f'{sid}\n')
print(f'cap_reached_sites={len(rows)}')
"
N_RECOL=$(wc -l < "$CAP_REACHED_FILE" 2>/dev/null || echo 0)
echo "[$(date '+%F %T')] cap-reached sites detected: $N_RECOL (list=$CAP_REACHED_FILE)"

if [ "$N_RECOL" -gt 0 ]; then
    RECOL_PER_SITE_LIMIT=100000
    OFFSET2=0
    BATCH_IDX2=0
    while [ "$OFFSET2" -lt "$N_RECOL" ]; do
        BATCH_IDX2=$((BATCH_IDX2 + 1))
        TS=$(date +%Y%m%d_%H%M%S)
        BATCH_LOG="data/audit/logs/promote_all_cap100k_batch${BATCH_IDX2}_${TS}.log"
        echo "[$(date '+%F %T')] >>> CAP100K BATCH $BATCH_IDX2  offset=$OFFSET2  size=$BATCH_SIZE  log=$BATCH_LOG"
        .venv/bin/python scripts/promote_all.py \
            --per-site-limit "$RECOL_PER_SITE_LIMIT" \
            --concurrent-sites "$CONCURRENT_SITES" \
            --concurrent-pdfs "$CONCURRENT_PDFS" \
            --only-site-ids-file "$CAP_REACHED_FILE" \
            --site-offset "$OFFSET2" \
            --site-limit-count "$BATCH_SIZE" \
            > "$BATCH_LOG" 2>&1
        EXIT=$?
        echo "[$(date '+%F %T')] <<< CAP100K BATCH $BATCH_IDX2 exited code=$EXIT"
        OFFSET2=$((OFFSET2 + BATCH_SIZE))
    done
    echo "[$(date '+%F %T')] cap-100000 recollection done across $BATCH_IDX2 batch(es)."

    QA2_TS=$(date +%Y%m%d_%H%M%S)
    QA2_XLSX="data/audit/qa_crawl_accuracy_cap100k_${QA2_TS}.xlsx"
    QA2_LOG="data/audit/logs/qa_crawl_accuracy_cap100k_${QA2_TS}.log"
    .venv/bin/python scripts/qa_crawl_accuracy.py \
        --limit 500 \
        --xlsx "$QA2_XLSX" \
        > "$QA2_LOG" 2>&1
    echo "[$(date '+%F %T')] QA #2 (post cap-100k) done. xlsx=$QA2_XLSX"
else
    echo "[$(date '+%F %T')] No cap-reached sites — skipping cap-100k recollection."
fi

echo "[$(date '+%F %T')] BATCH watcher complete (full chain)."
