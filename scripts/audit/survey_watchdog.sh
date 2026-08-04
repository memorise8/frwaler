#!/usr/bin/env bash
# Watchdog for the full capacity survey. PID-file based liveness (no string
# matching → no self-match). Auto-restarts (resume) on death, stall-detects,
# exits when all sites recorded.
set -u
ROOT=/data_raid/ruci_workspace/frwaler_job
cd "$ROOT"
source .venv/bin/activate
export PYTHONPATH="$ROOT"

CSV="$ROOT/scripts/audit/full_survey_totals.csv"
RUNLOG="$ROOT/scripts/audit/out/full_survey_run.log"
WLOG="$ROOT/scripts/audit/out/watchdog.log"
PIDFILE="$ROOT/scripts/audit/out/full_survey.pid"
TOTAL=789
MAX_RESTARTS=40
STALL_S=2900          # > site-timeout(2400): no new row this long ⇒ stuck
POLL_S=60

mkdir -p "$ROOT/scripts/audit/out"
log(){ echo "[$(date +%FT%T)] $*" >> "$WLOG"; }

done_count(){ [ -f "$CSV" ] && { tail -n +2 "$CSV" 2>/dev/null | cut -d, -f1 | sort -u | wc -l | tr -d ' '; } || echo 0; }
alive(){ [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }
kill_survey(){ [ -f "$PIDFILE" ] && kill -9 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; pkill -9 -P "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

restarts=0; last_done=-1; last_change=$(date +%s)
log "watchdog START total=$TOTAL max_restarts=$MAX_RESTARTS stall=${STALL_S}s"

while true; do
  d=$(done_count); now=$(date +%s)
  if [ "$d" -ge "$TOTAL" ]; then log "COMPLETE done=$d/$TOTAL"; kill_survey; break; fi
  if [ "$d" -ne "$last_done" ]; then last_done=$d; last_change=$now; fi

  if alive; then
    stalled=$(( now - last_change ))
    if [ "$stalled" -ge "$STALL_S" ]; then
      log "STALL: done=$d unchanged ${stalled}s -> kill+resume"; kill_survey; sleep 5; last_change=$now
    else
      log "alive done=$d/$TOTAL stall_timer=${stalled}s restarts=$restarts"
    fi
  else
    if [ "$restarts" -ge "$MAX_RESTARTS" ]; then log "GIVE UP MAX_RESTARTS at done=$d/$TOTAL"; break; fi
    restarts=$((restarts+1))
    log "survey not running (done=$d/$TOTAL) -> launch #$restarts"
    nohup python3 scripts/audit/full_survey.py --resume --workers 6 --site-timeout 1200 >> "$RUNLOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 25; last_change=$(date +%s)
  fi
  sleep "$POLL_S"
done
log "watchdog EXIT done=$(done_count)/$TOTAL restarts=$restarts"
