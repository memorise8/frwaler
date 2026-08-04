# -*- coding: utf-8 -*-
"""Collect HRB Open Research articles into ``libertree-app/data/libertree.db``.

Runs the custom ``hrbopenresearch-org`` crawler
(``crawler/sites/custom/hrbopenresearch-org-articles.py``), which scrapes
article listing + detail pages via ``StealthSession`` (Cloudflare bypass).

Usage:
    python -m scripts.collect_hrbopenresearch [--limit N]
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
_CRAWLER_PY = _ROOT / "crawler" / "sites" / "custom" / "hrbopenresearch-org-articles.py"
_spec = importlib.util.spec_from_file_location("hrbopenresearch_org_articles", _CRAWLER_PY)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
HrbOpenResearchArticlesCrawler = _mod.HrbOpenResearchArticlesCrawler

DB_PATH = _ROOT / "libertree-app" / "data" / "libertree.db"
SHEET = "Irish Research Centers"
SITE_ID = "hrbopenresearch-org"
SITE_URL = "https://hrbopenresearch.org/browse/articles"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max articles to save (default: all)")
    args = ap.parse_args()

    conn = _ldb.open_db(DB_PATH)
    _ldb.init_db(conn)

    _ldb.upsert_site(
        conn,
        site_id=SITE_ID,
        site_name="HRB Open Research",
        site_url=SITE_URL,
        sheet=SHEET,
    )

    pre = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (SITE_ID,)
    ).fetchone()[0]

    print(f"=== hrbopenresearch-org collection start -> {DB_PATH} "
          f"(existing={pre}) ===", flush=True)
    t0 = time.time()
    try:
        crawler = HrbOpenResearchArticlesCrawler(db_conn=conn, delay=1.5)
        saved = crawler.crawl(limit=args.limit)
    except Exception as exc:
        print(f"--- ERROR: {type(exc).__name__}: {exc}", flush=True)
        saved = 0

    post = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (SITE_ID,)
    ).fetchone()[0]
    added = post - pre

    conn.close()
    print(f"\n=== DONE. saved={saved} added={added} total={post} "
          f"({time.time() - t0:.0f}s) ===", flush=True)


if __name__ == "__main__":
    main()
