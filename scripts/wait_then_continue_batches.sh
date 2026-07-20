#!/bin/bash
# Resumes batch chain when an in-flight promote_all batch finishes.
# Args:
#   $1 = in-flight batch PID to wait on
#   $2 = next site_offset to start from
set -u
ROOT="/data_raid/ruci_workspace/frwaler_job"
INFLIGHT_PID="$1"
NEXT_OFFSET="$2"
TRANSITION_LOG="$ROOT/data/audit/logs/promote_transition.log"
BATCH_SIZE=250
PER_SITE_LIMIT=30000
CONCURRENT_SITES=4
CONCURRENT_PDFS=8

exec >> "$TRANSITION_LOG" 2>&1
echo "==============================================================="
echo "[$(date '+%F %T')] CHAIN-v2 watcher: waiting for in-flight batch PID=$INFLIGHT_PID"
echo "[$(date '+%F %T')] next_offset=$NEXT_OFFSET batch_size=$BATCH_SIZE per_site_limit=$PER_SITE_LIMIT"

while kill -0 "$INFLIGHT_PID" 2>/dev/null; do
    sleep 60
done
echo "[$(date '+%F %T')] in-flight PID=$INFLIGHT_PID exited."

cd "$ROOT" || exit 1

# Total unique sites
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
echo "[$(date '+%F %T')] Total unique sites: $TOTAL"

OFFSET="$NEXT_OFFSET"
BATCH_IDX=1  # batch 1 already done by previous watcher
while [ "$OFFSET" -lt "$TOTAL" ]; do
    BATCH_IDX=$((BATCH_IDX + 1))
    TS=$(date +%Y%m%d_%H%M%S)
    BATCH_LOG="data/audit/logs/promote_all_capoff_batch${BATCH_IDX}_${TS}.log"
    echo "[$(date '+%F %T')] >>> BATCH $BATCH_IDX offset=$OFFSET size=$BATCH_SIZE log=$BATCH_LOG"
    .venv/bin/python scripts/promote_all.py \
        --per-site-limit "$PER_SITE_LIMIT" \
        --concurrent-sites "$CONCURRENT_SITES" \
        --concurrent-pdfs "$CONCURRENT_PDFS" \
        --site-offset "$OFFSET" \
        --site-limit-count "$BATCH_SIZE" \
        > "$BATCH_LOG" 2>&1
    echo "[$(date '+%F %T')] <<< BATCH $BATCH_IDX exited code=$?"
    OFFSET=$((OFFSET + BATCH_SIZE))
done

echo "[$(date '+%F %T')] All cap-30k batches done. QA #1."
QA1_TS=$(date +%Y%m%d_%H%M%S)
QA1_XLSX="data/audit/qa_crawl_accuracy_cap30k_${QA1_TS}.xlsx"
QA1_LOG="data/audit/logs/qa_crawl_accuracy_cap30k_${QA1_TS}.log"
.venv/bin/python scripts/qa_crawl_accuracy.py \
    --limit 500 --xlsx "$QA1_XLSX" > "$QA1_LOG" 2>&1
echo "[$(date '+%F %T')] QA #1 done. xlsx=$QA1_XLSX"

# cap-reached identification + recollection
RECOL_TS=$(date +%Y%m%d_%H%M%S)
CAP_REACHED_FILE="data/audit/cap_reached_${RECOL_TS}.txt"
.venv/bin/python -c "
import sqlite3
con=sqlite3.connect('data/libertree.db')
rows=con.execute('SELECT site_id FROM documents GROUP BY site_id HAVING COUNT(*) >= 30000 ORDER BY COUNT(*) DESC').fetchall()
with open('$CAP_REACHED_FILE','w') as f:
    for (sid,) in rows: f.write(sid+'\n')
print('cap_reached_sites='+str(len(rows)))
"
N_RECOL=$(wc -l < "$CAP_REACHED_FILE" 2>/dev/null || echo 0)
echo "[$(date '+%F %T')] cap-reached sites: $N_RECOL"

if [ "$N_RECOL" -gt 0 ]; then
    OFFSET2=0
    BATCH_IDX2=0
    while [ "$OFFSET2" -lt "$N_RECOL" ]; do
        BATCH_IDX2=$((BATCH_IDX2 + 1))
        TS=$(date +%Y%m%d_%H%M%S)
        BATCH_LOG="data/audit/logs/promote_all_cap100k_batch${BATCH_IDX2}_${TS}.log"
        echo "[$(date '+%F %T')] >>> CAP100K BATCH $BATCH_IDX2 offset=$OFFSET2"
        .venv/bin/python scripts/promote_all.py \
            --per-site-limit 100000 \
            --concurrent-sites "$CONCURRENT_SITES" \
            --concurrent-pdfs "$CONCURRENT_PDFS" \
            --only-site-ids-file "$CAP_REACHED_FILE" \
            --site-offset "$OFFSET2" \
            --site-limit-count "$BATCH_SIZE" \
            > "$BATCH_LOG" 2>&1
        echo "[$(date '+%F %T')] <<< CAP100K BATCH $BATCH_IDX2 exited code=$?"
        OFFSET2=$((OFFSET2 + BATCH_SIZE))
    done

    QA2_TS=$(date +%Y%m%d_%H%M%S)
    QA2_XLSX="data/audit/qa_crawl_accuracy_cap100k_${QA2_TS}.xlsx"
    QA2_LOG="data/audit/logs/qa_crawl_accuracy_cap100k_${QA2_TS}.log"
    .venv/bin/python scripts/qa_crawl_accuracy.py \
        --limit 500 --xlsx "$QA2_XLSX" > "$QA2_LOG" 2>&1
    echo "[$(date '+%F %T')] QA #2 done. xlsx=$QA2_XLSX"
else
    echo "[$(date '+%F %T')] No cap-reached sites — skipping recollection."
fi

echo "[$(date '+%F %T')] CHAIN-v2 watcher complete."
