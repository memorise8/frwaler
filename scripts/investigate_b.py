#!/usr/bin/env python3
"""Investigate Category B (papers rows whose DOC_ID is not in upstream doc_index).

Samples N rows per site, calls ``_fetch_detail`` for each, and categorises:

    ok      — upstream still returns a full detail
    empty   — upstream returns an empty/null payload
    fail    — curl/network failure (treat as inconclusive)

Usage:
    python scripts/investigate_b.py --per-site 15
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
REPORT_DIR = ROOT / "reports"

sys.path.insert(0, str(ROOT))

from crawler import db as dbm  # noqa: E402
from crawler.sites.nts_taxlaw import (NTSTaxlawPdCrawler,  # noqa: E402
                                        NTSTaxlawQtCrawler)

SITE_CRAWLERS = {
    "nts-taxlaw-pd": NTSTaxlawPdCrawler,
    "nts-taxlaw-qt": NTSTaxlawQtCrawler,
}


def load_b_ids(site_id: str, n: int, seed: int = 42) -> list[str]:
    path = REPORT_DIR / f"gap_{site_id}.B_deleted_upstream.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    ids = [it["external_id"] for it in items if it.get("external_id")]
    rng = random.Random(seed)
    return rng.sample(ids, min(n, len(ids)))


def probe(crawler, doc_ids: list[str]) -> dict:
    stats = {"ok": [], "empty": [], "fail": []}
    for i, did in enumerate(doc_ids, 1):
        detail = crawler._fetch_detail(did)
        if detail is None:
            stats["fail"].append(did)
            label = "FAIL"
        else:
            dvo = (detail or {}).get("dcmDVO") or {}
            title = dvo.get("ntstDcmTtl") or dvo.get("TTL") or ""
            if title:
                stats["ok"].append({"doc_id": did, "title": title[:40]})
                label = f"OK   — {title[:40]}"
            else:
                stats["empty"].append(did)
                label = "EMPTY"
        print(f"  [{i:>3}/{len(doc_ids)}] {did}: {label}")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-site", type=int, default=15,
                    help="Number of samples per site (default: 15)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    conn = dbm.get_db(str(DB_PATH))
    try:
        dbm.init_db(conn)
        for site_id, cls in SITE_CRAWLERS.items():
            print(f"\n=== {site_id} — sampling {args.per_site} ids ===")
            sample = load_b_ids(site_id, args.per_site, args.seed)
            crawler = cls(db_conn=conn, delay=0.5)
            stats = probe(crawler, sample)
            print(f"\n  summary: ok={len(stats['ok'])}, "
                  f"empty={len(stats['empty'])}, fail={len(stats['fail'])}")
            out = REPORT_DIR / f"investigate_b_{site_id}.json"
            out.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            print(f"  wrote {out.relative_to(ROOT)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
