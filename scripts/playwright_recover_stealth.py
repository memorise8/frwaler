# -*- coding: utf-8 -*-
"""Stealth recovery pass for entries that were cf_still_blocked in standard
Playwright runs.

Reads all JSON files from data/audit/playwright_runs/*.json, collects entries
with status == 'cf_still_blocked', then re-fetches each URL using
fetch_html_stealth() and writes results to
data/audit/playwright_runs_stealth/<entry_id>.json.

Usage:
    .venv/bin/python scripts/playwright_recover_stealth.py [--resume]
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crawler.playwright_fetcher import fetch_html_stealth, is_cf_challenge


def extract_title(html: str) -> Optional[str]:
    """Extract <title> text from HTML."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:200]
    return None


def count_anchors(html: str) -> int:
    """Count <a href=...> anchors in HTML."""
    return len(re.findall(r"<a\s+[^>]*href", html, re.IGNORECASE))


def classify(html: Optional[str]) -> str:
    if html is None:
        return "error"
    if is_cf_challenge(html):
        return "cf_still_blocked"
    if len(html) > 1000:
        return "success"
    return "error"


def load_blocked_entries(runs_dir: str) -> list:
    """Read all JSON files in runs_dir and return those with status == 'cf_still_blocked'."""
    entries = []
    for path in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get("status") == "cf_still_blocked":
            entries.append(d)
    return entries


def process_entry(entry: dict, out_dir: str, timeout: int) -> dict:
    entry_id = entry["entry_id"]
    url = entry["url"]
    host = entry["host"]

    print(f"  -> {host}  {url}", file=sys.stderr)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    html: Optional[str] = None
    error_msg: Optional[str] = None
    try:
        html = fetch_html_stealth(
            url,
            timeout_seconds=timeout,
            extra_wait_seconds=5.0,
            block_resources=False,
        )
    except Exception as exc:
        error_msg = str(exc)

    elapsed = round(time.monotonic() - t0, 2)
    status = classify(html)

    html_length = len(html) if html else 0
    title = extract_title(html) if html else None
    anchor_count = count_anchors(html) if html else 0

    result = {
        "entry_id": entry_id,
        "url": url,
        "host": host,
        "started_at": started_at,
        "elapsed_seconds": elapsed,
        "html_length": html_length,
        "title": title,
        "anchor_count": anchor_count,
        "status": status,
        "error": error_msg,
        "stealth": True,
    }

    out_path = os.path.join(out_dir, f"{entry_id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    icon = {"success": "OK", "cf_still_blocked": "CF", "error": "ERR"}[status]
    print(
        f"  [{icon}] {host}  len={html_length}  t={elapsed}s"
        + (f"  title={title!r}" if title else ""),
        file=sys.stderr,
    )
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Stealth recovery for cf_still_blocked entries"
    )
    parser.add_argument(
        "--runs-dir",
        default="data/audit/playwright_runs",
        help="Directory with prior playwright_runs JSON files (source)",
    )
    parser.add_argument(
        "--out-dir",
        default="data/audit/playwright_runs_stealth",
        help="Output directory for stealth run JSON files",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Per-URL timeout in seconds (default 45)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip entries whose stealth JSON already exists in out-dir",
    )
    args = parser.parse_args()

    # Resolve paths relative to project root
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runs_dir = args.runs_dir if os.path.isabs(args.runs_dir) else os.path.join(base, args.runs_dir)
    out_dir = args.out_dir if os.path.isabs(args.out_dir) else os.path.join(base, args.out_dir)

    os.makedirs(out_dir, exist_ok=True)

    if not os.path.isdir(runs_dir):
        print(f"ERROR: runs-dir not found: {runs_dir}", file=sys.stderr)
        sys.exit(1)

    entries = load_blocked_entries(runs_dir)
    if not entries:
        print("[playwright_recover_stealth] No cf_still_blocked entries found. Nothing to do.",
              file=sys.stderr)
        sys.exit(0)

    print(
        f"\n[playwright_recover_stealth] Found {len(entries)} cf_still_blocked entries. "
        f"Starting stealth recovery (timeout={args.timeout}s, resume={args.resume})\n",
        file=sys.stderr,
    )

    results = []
    skipped = 0
    for i, entry in enumerate(entries, 1):
        if args.resume:
            existing = os.path.join(out_dir, f"{entry['entry_id']}.json")
            if os.path.exists(existing):
                skipped += 1
                continue
        print(f"[{i}/{len(entries)}]", file=sys.stderr)
        r = process_entry(entry, out_dir, args.timeout)
        results.append(r)

    if skipped:
        print(f"\n[playwright_recover_stealth] resumed; skipped {skipped} existing entries\n",
              file=sys.stderr)

    if not results:
        print("[playwright_recover_stealth] All entries skipped (--resume). Nothing new.",
              file=sys.stderr)
        sys.exit(0)

    # --- Summary ---
    counts = {"success": 0, "cf_still_blocked": 0, "error": 0}
    for r in results:
        counts[r["status"]] += 1

    elapsed_vals = [r["elapsed_seconds"] for r in results]
    avg_elapsed = round(sum(elapsed_vals) / len(elapsed_vals), 2) if elapsed_vals else 0
    total = len(results)
    recovery_rate = round(counts["success"] / total * 100, 1) if total else 0.0

    # Per-host breakdown
    from collections import defaultdict
    host_counts: dict = defaultdict(lambda: {"success": 0, "cf_still_blocked": 0, "error": 0})
    for r in results:
        host_counts[r["host"]][r["status"]] += 1

    recovered_hosts = [h for h, c in host_counts.items() if c["success"] > 0]
    still_blocked_hosts = [h for h, c in host_counts.items() if c["cf_still_blocked"] > 0]

    print("\n" + "=" * 60, file=sys.stderr)
    print("[playwright_recover_stealth] SUMMARY", file=sys.stderr)
    print(f"  Total     : {total}", file=sys.stderr)
    print(f"  success   : {counts['success']}  ({recovery_rate}%)", file=sys.stderr)
    print(f"  cf_blocked: {counts['cf_still_blocked']}", file=sys.stderr)
    print(f"  error     : {counts['error']}", file=sys.stderr)
    print(f"  avg time  : {avg_elapsed}s", file=sys.stderr)
    if recovered_hosts:
        print(f"  Recovered hosts  : {', '.join(sorted(recovered_hosts))}", file=sys.stderr)
    if still_blocked_hosts:
        print(f"  Still blocked    : {', '.join(sorted(still_blocked_hosts))}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Machine-readable summary to stdout
    summary = {
        "total": total,
        "skipped_resume": skipped,
        "counts": counts,
        "recovery_rate_pct": recovery_rate,
        "avg_elapsed_seconds": avg_elapsed,
        "recovered_hosts": sorted(recovered_hosts),
        "still_blocked_hosts": sorted(still_blocked_hosts),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
