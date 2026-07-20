#!/usr/bin/env bash
# cron_crawl.sh — nightly batch chain for libertree.
#
# In compose mode the scheduler container runs `cron_crawl.sh loop` which
# sleeps until the next 03:00 KST and then runs the crawl→download→convert
# →summarize chain for every registered site.
#
# Standalone test on the host:
#   bash scripts/cron_crawl.sh once             # run the chain once and exit
#   SITES="ntrs mohw" bash scripts/cron_crawl.sh once   # subset of sites

set -euo pipefail

cd "$(dirname "$0")/.."

LOG_DIR="${LIBERTREE_LOG_DIR:-./logs}"
mkdir -p "$LOG_DIR"

run_once() {
    local stamp; stamp="$(date '+%Y%m%d-%H%M%S')"
    local log="$LOG_DIR/cron-$stamp.log"
    echo "[cron_crawl] $(date '+%F %T') start → $log"

    local sites="${SITES:-}"
    if [[ -z "$sites" ]]; then
        sites="$(python -m crawler.main list-sites 2>/dev/null \
                 | awk 'NR>2 && NF>=1 && $1 !~ /^-/ {print $1}')"
    fi

    {
        echo "=== sites: $sites ==="
        for site in $sites; do
            echo
            echo "=== [$site] $(date '+%F %T') ==="
            python -m crawler.main crawl "$site" --incremental || true
            python -m crawler.main download "$site" || true
            python -m crawler.main convert "$site" || true
            python -m crawler.main summarize "$site" || true
        done
        echo
        echo "=== stats $(date '+%F %T') ==="
        python -m crawler.main stats || true
    } 2>&1 | tee -a "$log"

    echo "[cron_crawl] $(date '+%F %T') done"
}

loop() {
    while true; do
        # Sleep until next 03:00 (TZ honours $TZ env var)
        local now; now="$(date '+%s')"
        local target; target="$(date -d 'today 03:00' '+%s')"
        if (( target <= now )); then
            target="$(date -d 'tomorrow 03:00' '+%s')"
        fi
        local wait=$(( target - now ))
        echo "[cron_crawl] sleeping ${wait}s until $(date -d "@$target" '+%F %T')"
        sleep "$wait"
        run_once || echo "[cron_crawl] run failed (continuing)" >&2
    done
}

case "${1:-once}" in
    once) run_once ;;
    loop) loop ;;
    *)
        echo "Usage: $0 {once|loop}" >&2
        exit 2
        ;;
esac
