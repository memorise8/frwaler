"""Backfill `metadata.datasheet_url` for Infineon products.

Walks every Infineon row in the products DB whose metadata is missing a
``datasheet_url``, fetches the part page, extracts the PDF URL with the
crawler's helper, and updates ONLY the ``metadata`` column. Other columns are
never touched, so this is safe to run repeatedly and concurrently with reads.

Usage:
    python -m scripts.backfill_infineon_datasheet \\
        --db data/products.db --limit 5 --delay 1.5

Run with ``--limit 5`` first, inspect, then drop ``--limit`` for the full sweep.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time

import httpx

# Same regex/heuristic as crawler.sites.infineon.extract_datasheet_url —
# inlined to avoid importing the crawler package (which pulls in playwright).
_PDF_URL_RE = re.compile(
    r'https?://www\.infineon\.com/assets/[^\s"\'<>]+?\.pdf',
    re.IGNORECASE,
)


def extract_datasheet_url(html: str, opn: str) -> str | None:
    urls = _PDF_URL_RE.findall(html or "")
    if not urls:
        return None
    seen, deduped = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    opn_slug = opn.lower().replace("-", "").replace("_", "")
    for u in deduped:
        slug = u.lower().split("/")[-1].replace("-", "").replace("_", "")
        if opn_slug and opn_slug in slug:
            return u
    return deduped[0]


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def select_rows(conn: sqlite3.Connection, limit: int | None):
    cur = conn.execute(
        "SELECT external_id, url, metadata FROM products "
        "WHERE site_id = 'infineon' "
        "ORDER BY external_id"
    )
    out = []
    for opn, url, meta_raw in cur:
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            meta = {}
        if meta.get("datasheet_url"):
            continue
        out.append((opn, url, meta))
        if limit is not None and len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/products.db")
    ap.add_argument("--limit", type=int, default=None,
                    help="Max rows to update (default: all missing)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="Seconds between requests")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan, don't write to DB")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows = select_rows(conn, args.limit)
    print(f"[backfill] {len(rows)} Infineon rows missing datasheet_url"
          + (" (limited)" if args.limit else ""))

    updated = miss = err = 0
    with httpx.Client(headers={"User-Agent": UA},
                      follow_redirects=True,
                      timeout=args.timeout) as client:
        for i, (opn, url, meta) in enumerate(rows, 1):
            try:
                r = client.get(url)
                if r.status_code != 200:
                    err += 1
                    print(f"  [{i}/{len(rows)}] {opn}: HTTP {r.status_code}")
                    continue
                ds = extract_datasheet_url(r.text, opn)
                if not ds:
                    miss += 1
                    print(f"  [{i}/{len(rows)}] {opn}: no PDF link in page")
                    continue
                meta["datasheet_url"] = ds
                if not args.dry_run:
                    conn.execute(
                        "UPDATE products SET metadata = ? "
                        "WHERE site_id = 'infineon' AND external_id = ?",
                        (json.dumps(meta), opn),
                    )
                    conn.commit()
                updated += 1
                if updated <= 10 or updated % 500 == 0:
                    print(f"  [{i}/{len(rows)}] {opn} -> {ds[:90]}...")
            except Exception as e:
                err += 1
                print(f"  [{i}/{len(rows)}] {opn}: {type(e).__name__}: {e}")
            time.sleep(args.delay)

    conn.close()
    print(f"\n[backfill] done: updated={updated} no_pdf={miss} errors={err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
