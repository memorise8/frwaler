# -*- coding: utf-8 -*-
"""Collect www.gov.il Israeli ministry news + publications.

Runs the single ``gov-il-collectors`` custom crawler (see
``crawler/sites/custom/gov-il-collectors.py``), which iterates every
Israeli ministry officeId across both the ``news`` and ``publications``
collectors, pulling each item's title / date / body text via the gov.il
JSON API and downloading attached documents where present. Writes directly
into ``libertree-app/data/libertree.db`` under a single ``sites`` row.

Usage:
    python -m scripts.collect_gov_il [--limit N]

``--limit`` caps the *total* number of documents saved this run (handy for
smoke tests); omit it for a full collection.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler import db_libertree as _ldb  # noqa: E402

# Load the custom crawler module by path (filename has hyphens).
_CRAWLER_PY = _ROOT / "crawler" / "sites" / "custom" / "gov-il-collectors.py"
_spec = importlib.util.spec_from_file_location("gov_il_collectors", _CRAWLER_PY)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
GovIlCollectorsCrawler = _mod.GovIlCollectorsCrawler

DB_PATH = _ROOT / "libertree-app" / "data" / "libertree.db"
SHEET = "Israel Ministries"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max documents to save this run (default: all)")
    args = ap.parse_args()

    conn = _ldb.open_db(DB_PATH)
    _ldb.init_db(conn)

    site_id = GovIlCollectorsCrawler.site_id
    _ldb.upsert_site(
        conn,
        site_id=site_id,
        site_name=GovIlCollectorsCrawler.site_name,
        site_url=GovIlCollectorsCrawler.base_url,
        sheet=SHEET,
    )
    pre = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
    ).fetchone()[0]

    print(f"=== gov.il collection start -> {DB_PATH} (existing={pre}) ===",
          flush=True)
    t0 = time.time()
    try:
        crawler = GovIlCollectorsCrawler(db_conn=conn, delay=1.0)
        saved = crawler.crawl(limit=args.limit)
    except Exception as exc:
        print(f"=== ERROR: {type(exc).__name__}: {exc}", flush=True)
        saved = 0

    post = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
    ).fetchone()[0]
    conn.close()
    print(f"=== DONE: saved={saved} added={post - pre} total={post} "
          f"({time.time() - t0:.0f}s) ===", flush=True)


if __name__ == "__main__":
    main()
