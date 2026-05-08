"""Demo re-crawl: Vishay fixed resistors with full parametric specs.

Pulls /capacitors/.../<cat>.json and /resistors-fixed.json from the public
Next.js data API. The catalog JSON includes res/tol/power/voltage/mounting/temp/
package/automotive/technology — fields the original crawler ignored.

Filters to 10kΩ +/-1% 0603 -55~125°C SMD candidates (the demo query) and
updates products.db.metadata + a normalized 'spec_*' set inside specs JSON.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
API = "https://www.vishay.com/_next/data/vishay-nextjs-ic/en"


def fetch_category(client: httpx.Client, cat: str) -> list[dict]:
    r = client.get(f"{API}/{cat}.json")
    r.raise_for_status()
    return r.json().get("pageProps", {}).get("paramResults", []) or []


def matches_demo_query(item: dict) -> bool:
    """10kΩ ±1% 0603 -55 to +125 SMD."""
    try:
        rmin = float(item.get("res_min_value") or 0)
        rmax = float(item.get("res_max_value") or 0)
    except (TypeError, ValueError):
        rmin = rmax = 0
    if not (rmin <= 10000 <= rmax):
        return False

    tol = str(item.get("tolerance_displ") or "")
    if "1" not in tol and "0.5" not in tol and "0.1" not in tol:
        return False

    size = str(item.get("size_device_style") or "")
    if "0603" not in size:
        return False

    temp = str(item.get("temp") or "")
    if "-55" not in temp:
        return False

    mount = str(item.get("mounting_tech") or "").lower()
    if "surface" not in mount:
        return False

    return True


def normalize_specs(item: dict) -> dict:
    """Pick the human-readable subset for storage."""
    keep = [
        "P1001",  # series
        "res_min_displ",
        "res_max_displ",
        "tolerance_displ",
        "wt_power_rating_displ",
        "wt_max_voltage_displ",
        "TCR_DISPL",
        "mounting_tech",
        "size_device_style",
        "temp",
        "automotive",
        "technology",
        "eseries",
    ]
    return {k: item[k] for k in keep if item.get(k) not in (None, "", [])}


def update_specs(conn: sqlite3.Connection, docid: str, specs: dict) -> bool:
    cur = conn.execute(
        "SELECT specs FROM products WHERE site_id='vishay' AND external_id=?",
        (docid,),
    )
    row = cur.fetchone()
    if row is None:
        return False
    try:
        existing = json.loads(row[0]) if row[0] else {}
    except Exception:
        existing = {}
    merged = {**existing, **specs}
    conn.execute(
        "UPDATE products SET specs=? WHERE site_id='vishay' AND external_id=?",
        (json.dumps(merged, ensure_ascii=False), docid),
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/products.db")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    with httpx.Client(headers={"User-Agent": UA},
                      follow_redirects=True, timeout=30.0) as c:
        print("[fetch] resistors-fixed catalog...")
        all_items = fetch_category(c, "resistors-fixed")
        print(f"[fetch] got {len(all_items)} resistor entries")

    matched_all = [it for it in all_items if matches_demo_query(it)]
    print(f"[filter] {len(matched_all)} parametric rows match 10kΩ ±1% 0603 -55~125 SMD")
    if not matched_all:
        return 1

    # paramResults has multiple rows per docid (one per tol/temp variant).
    # Pick the tightest tolerance per docid as the representative.
    by_docid: dict[str, dict] = {}
    for it in matched_all:
        d = str(it.get("P1000"))
        try:
            tol = float(str(it.get("tolerance_displ") or "99").replace("%", ""))
        except (TypeError, ValueError):
            tol = 99.0
        prev = by_docid.get(d)
        if prev is None or tol < float(str(prev.get("tolerance_displ") or "99").replace("%", "")):
            by_docid[d] = it

    matched = list(by_docid.values())
    print(f"[dedupe] {len(matched)} unique series (docid)")
    matched = matched[: args.limit]

    updated = inserted_only = 0
    for item in matched:
        docid = str(item.get("P1000"))
        specs = normalize_specs(item)
        specs_for_print = {
            "series": specs.get("P1001"),
            "res_range": f"{specs.get('res_min_displ')}~{specs.get('res_max_displ')}",
            "tol": specs.get("tolerance_displ"),
            "pwr": specs.get("wt_power_rating_displ"),
            "size": specs.get("size_device_style"),
            "temp": specs.get("temp"),
            "auto": specs.get("automotive"),
            "tech": specs.get("technology"),
        }
        if not args.dry_run:
            ok = update_specs(conn, docid, specs)
            if ok:
                conn.commit()
                updated += 1
            else:
                inserted_only += 1
        print(f"  docid={docid}  {specs_for_print}")

    print(f"\n[done] updated DB rows: {updated}, "
          f"matched-but-not-in-DB: {inserted_only}, "
          f"dry_run={args.dry_run}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
