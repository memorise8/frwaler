#!/usr/bin/env python3
"""Dump the full list-API payload for each site into
``reports/list_cache/list_cache_<site_id>.json`` (doc_id → dcm dict).

Used by ``targeted_refetch.py --list-cache-dir`` to fill fields that the
detail API does not return (NTST_TLAW_CL_NM, DCM_RGT_DTM, NTST_DCM_DSCM_CNTN,
NTST_FLE_ID, NTST_DCM_SRCS_ORGN_CL_CD, NTST_DCM_RPLY_CNTN, NTST_DCM_CL_NM).

Usage:
    python scripts/build_list_cache.py --site-id nts-taxlaw-pd
    python scripts/build_list_cache.py --site-id nts-taxlaw-qt --delay 0.5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler import db as dbm  # noqa: E402
from crawler.sites.nts_taxlaw import (NTSTaxlawPdCrawler,  # noqa: E402
                                      NTSTaxlawQtCrawler)

DB_PATH = ROOT / "data" / "papers.db"
SITE_CRAWLERS = {
    "nts-taxlaw-pd": NTSTaxlawPdCrawler,
    "nts-taxlaw-qt": NTSTaxlawQtCrawler,
}


def build(site_id: str, delay: float, out_dir: Path) -> int:
    cls = SITE_CRAWLERS[site_id]
    conn = dbm.get_db(str(DB_PATH))
    dbm.init_db(conn)
    crawler = cls(db_conn=conn, delay=delay)

    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / f".progress_{site_id}.json"

    cache: dict[str, dict] = {}
    page = 1
    total_expected = None
    empty_streak = 0
    while True:
        time.sleep(delay)
        raw = crawler._fetch_list(page)
        if not raw:
            empty_streak += 1
            page += 1
            if empty_streak > 3:
                break
            continue
        try:
            data = json.loads(raw)
            items = data["data"]["ASIPDI002PR01"]["body"] or []
        except Exception:
            page += 1
            continue
        if not items:
            break
        empty_streak = 0
        if page == 1:
            total_expected = data.get("data", {}).get(
                "ASIPDI002PR01", {}).get("totalCount")
            print(f"[{site_id}] total records: {total_expected}", flush=True)
        for it in items:
            dcm = it.get("dcm") or {}
            did = str(dcm.get("DOC_ID") or "")
            if did:
                cache.setdefault(did, dcm)
        if page % 10 == 0:
            progress_path.write_text(json.dumps({
                "site_id": site_id, "page": page,
                "cached": len(cache),
                "total_expected": total_expected,
            }), encoding="utf-8")
        if page % 100 == 0:
            print(f"[{site_id}] page {page}, cached {len(cache)} docs",
                  flush=True)
        page += 1

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"list_cache_{site_id}.json"
    out_path.write_text(json.dumps(cache, ensure_ascii=False),
                        encoding="utf-8")
    print(f"[{site_id}] wrote {out_path.relative_to(ROOT)} "
          f"({len(cache)} docs, {page - 1} pages)")
    conn.close()
    return len(cache)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-id", action="append", required=True,
                    choices=list(SITE_CRAWLERS))
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--out-dir", default="reports/list_cache")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    for site in args.site_id:
        build(site, args.delay, out_dir)


if __name__ == "__main__":
    main()
