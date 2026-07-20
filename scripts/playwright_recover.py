# -*- coding: utf-8 -*-
"""Sanity test for playwright_fetcher: fetch first N distinct hosts from
playwright_required.csv and validate CF-challenge bypass.

Usage:
    .venv/bin/python scripts/playwright_recover.py \
        [--input data/audit/playwright_required.csv] \
        [--out-dir data/audit/playwright_runs] \
        [--limit 5] \
        [--timeout 30]
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crawler.playwright_fetcher import fetch_html, is_cf_challenge


def extract_title(html: str) -> Optional[str]:
    """Extract <title> text from HTML."""
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:200]
    return None


def count_anchors(html: str) -> int:
    """Count <a href=...> anchors in HTML."""
    import re
    return len(re.findall(r"<a\s+[^>]*href", html, re.IGNORECASE))


def pick_sample_rows(csv_path: str, limit: int, *, distinct_hosts: bool = False) -> list:
    """Read CSV, return rows.

    - distinct_hosts=True: first `limit` rows with distinct hosts (sanity mode).
    - distinct_hosts=False (default): all rows up to `limit` (batch mode).
    """
    if distinct_hosts:
        seen_hosts: dict = {}
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                host = row["host"]
                if host not in seen_hosts:
                    seen_hosts[host] = row
                if len(seen_hosts) >= limit:
                    break
        return list(seen_hosts.values())

    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def classify(html: Optional[str]) -> str:
    if html is None:
        return "error"
    if is_cf_challenge(html):
        return "cf_still_blocked"
    if len(html) > 1000:
        return "success"
    return "error"


def process_row(row: dict, out_dir: str, timeout: int) -> dict:
    entry_id = row["entry_id"]
    url = row["url"]
    host = row["host"]

    print(f"  -> {host}  {url}", file=sys.stderr)
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    html: Optional[str] = None
    error_msg: Optional[str] = None
    try:
        html = fetch_html(url, timeout_seconds=timeout, extra_wait_seconds=3.0)
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
    parser = argparse.ArgumentParser(description="Playwright sanity fetch test")
    parser.add_argument("--input", default="data/audit/playwright_required.csv")
    parser.add_argument("--out-dir", default="data/audit/playwright_runs")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--distinct-hosts",
        action="store_true",
        help="sanity mode — pick first N distinct hosts (one row per host)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip entries whose JSON already exists in out-dir",
    )
    args = parser.parse_args()

    # Resolve paths relative to project root (script lives in scripts/)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    csv_path = args.input if os.path.isabs(args.input) else os.path.join(base, args.input)
    out_dir = args.out_dir if os.path.isabs(args.out_dir) else os.path.join(base, args.out_dir)

    os.makedirs(out_dir, exist_ok=True)

    if not os.path.exists(csv_path):
        print(f"ERROR: input CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    rows = pick_sample_rows(csv_path, args.limit, distinct_hosts=args.distinct_hosts)
    mode = "distinct hosts" if args.distinct_hosts else "rows"
    print(f"\n[playwright_recover] Processing {len(rows)} {mode} "
          f"(limit={args.limit}, timeout={args.timeout}s, resume={args.resume})\n",
          file=sys.stderr)

    results = []
    skipped = 0
    for i, row in enumerate(rows, 1):
        if args.resume:
            existing = os.path.join(out_dir, f"{row['entry_id']}.json")
            if os.path.exists(existing):
                skipped += 1
                continue
        print(f"[{i}/{len(rows)}]", file=sys.stderr)
        r = process_row(row, out_dir, args.timeout)
        results.append(r)
    if skipped:
        print(f"\n[playwright_recover] resumed; skipped {skipped} existing entries\n",
              file=sys.stderr)

    # --- Summary ---
    counts = {"success": 0, "cf_still_blocked": 0, "error": 0}
    for r in results:
        counts[r["status"]] += 1

    elapsed_vals = [r["elapsed_seconds"] for r in results]
    avg_elapsed = round(sum(elapsed_vals) / len(elapsed_vals), 2) if elapsed_vals else 0

    print("\n" + "=" * 60, file=sys.stderr)
    print("[playwright_recover] SUMMARY", file=sys.stderr)
    print(f"  Total     : {len(results)}", file=sys.stderr)
    print(f"  success   : {counts['success']}", file=sys.stderr)
    print(f"  cf_blocked: {counts['cf_still_blocked']}", file=sys.stderr)
    print(f"  error     : {counts['error']}", file=sys.stderr)
    print(f"  avg time  : {avg_elapsed}s", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    if counts["success"] >= 3:
        print("\nTrack-B infra: PASS (>=3 success). Ready for full 194-URL batch.",
              file=sys.stderr)
    elif counts["success"] >= 1:
        print("\nTrack-B infra: PARTIAL. CF bypass needs reinforcement "
              "(consider stealth plugins or longer wait).", file=sys.stderr)
    else:
        print("\nTrack-B infra: FAIL. All requests blocked or errored. "
              "Consider playwright-stealth or residential proxy.", file=sys.stderr)

    # Machine-readable summary to stdout
    summary = {
        "total": len(results),
        "counts": counts,
        "avg_elapsed_seconds": avg_elapsed,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
