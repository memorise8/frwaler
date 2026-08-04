# -*- coding: utf-8 -*-
"""Collect Government Offices of Sweden (government.se) publications into
``libertree-app/data/libertree.db``.

Runs the custom ``government-se-publications`` crawler
(``crawler/sites/custom/government-se-publications.py``), which pages
through all 50 uncollected Swedish sections (topic/document-type listing
pages + ministry pages) via ``StealthSession`` (Cloudflare bypass), then
downloads any PDF attachments found via ``download_pdfs_for_site`` (which
uses curl_cffi + a Referer header — government.se's contentassets/
globalassets PDF host 403s plain ``requests`` even with correct headers).

Usage:
    python -m scripts.collect_government_se [--limit N] [--no-pdf-download]
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
_CRAWLER_PY = _ROOT / "crawler" / "sites" / "custom" / "government-se-publications.py"
_spec = importlib.util.spec_from_file_location("government_se_publications", _CRAWLER_PY)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
GovernmentSePublicationsCrawler = _mod.GovernmentSePublicationsCrawler
download_pdfs_for_site = _mod.download_pdfs_for_site

DB_PATH = _ROOT / "libertree-app" / "data" / "libertree.db"
SHEET = "스웨덴 완료"
SITE_ID = GovernmentSePublicationsCrawler.site_id
SITE_URL = GovernmentSePublicationsCrawler.base_url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max documents to save (default: all)")
    ap.add_argument("--no-pdf-download", action="store_true",
                    help="Skip the post-crawl PDF download pass")
    args = ap.parse_args()

    conn = _ldb.open_db(DB_PATH)
    _ldb.init_db(conn)

    _ldb.upsert_site(
        conn,
        site_id=SITE_ID,
        site_name=GovernmentSePublicationsCrawler.site_name,
        site_url=SITE_URL,
        sheet=SHEET,
    )

    pre = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (SITE_ID,)
    ).fetchone()[0]

    print(f"=== {SITE_ID} collection start -> {DB_PATH} "
          f"(existing={pre}, sections={len(GovernmentSePublicationsCrawler.SECTIONS)}) ===",
          flush=True)
    t0 = time.time()
    try:
        crawler = GovernmentSePublicationsCrawler(db_conn=conn, delay=1.5)
        saved = crawler.crawl(limit=args.limit)
    except Exception as exc:
        print(f"--- ERROR: {type(exc).__name__}: {exc}", flush=True)
        saved = 0

    post = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (SITE_ID,)
    ).fetchone()[0]
    added = post - pre

    pdf_ok = pdf_failed = 0
    if not args.no_pdf_download:
        pdf_ok, pdf_failed = download_pdfs_for_site(conn, site_id=SITE_ID)

    conn.close()
    print(f"\n=== DONE. saved={saved} added={added} total={post} "
          f"pdf_ok={pdf_ok} pdf_failed={pdf_failed} "
          f"({time.time() - t0:.0f}s) ===", flush=True)


if __name__ == "__main__":
    main()
