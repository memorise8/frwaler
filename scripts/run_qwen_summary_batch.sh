#!/usr/bin/env bash
set -u

ROOT="/data_raid/ruci_workspace/frwaler_job"
CANONICAL_DB="$ROOT/libertree-app/data/libertree.db"
PORT="11436"
LOG_DIR="$ROOT/data/audit/logs"
RUN_LOG="$LOG_DIR/qwen_summary_batch_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"
exec >> "$RUN_LOG" 2>&1

echo "[$(date '+%F %T')] Qwen3 summary batch started; port=$PORT"
mapfile -t SITE_IDS < <(
  cd "$ROOT" && .venv/bin/python - <<'PY'
from __future__ import annotations

import sqlite3
from pathlib import Path

conn = sqlite3.connect(f"file:{Path('$CANONICAL_DB').resolve()}?mode=ro", uri=True)
try:
    for (site_id,) in conn.execute(
        """
        SELECT DISTINCT site_id
        FROM documents
        WHERE text_extracted = 1 AND COALESCE(summary, '') = ''
        ORDER BY site_id
        """
    ):
        print(site_id)
finally:
    conn.close()
PY
)

total="${#SITE_IDS[@]}"
success=0
failed=0
echo "[$(date '+%F %T')] sites pending=$total"

for index in "${!SITE_IDS[@]}"; do
  site_id="${SITE_IDS[$index]}"
  echo "[$(date '+%F %T')] [$((index + 1))/$total] site=$site_id"
  if cd "$ROOT" && .venv/bin/python scripts/bulk_summarize_gemma.py --site "$site_id" --port "$PORT" --commit-every 20; then
    success=$((success + 1))
  else
    failed=$((failed + 1))
    echo "[$(date '+%F %T')] site failed=$site_id"
  fi
done

echo "[$(date '+%F %T')] Qwen3 summary batch complete; succeeded=$success failed=$failed"
