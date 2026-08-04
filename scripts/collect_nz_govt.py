# -*- coding: utf-8 -*-
"""Collect all three New Zealand government-publication crawlers into
``libertree-app/data/libertree.db``.

Runs, in sequence:

- ``treasury-govt-nz-publications`` (The Treasury New Zealand)
- ``health-govt-nz-publications``   (Ministry of Health NZ)
- ``justice-govt-nz-publications``  (Ministry of Justice — "Find a
  publication"; currently blocked upstream by an AWS WAF captcha rule on
  every ``/about/publication-finder/*`` URL — see that crawler module's
  docstring. Included for completeness/consistency; expect 0 saved until
  the block lifts.)

Each crawler pages through its hard-coded ``SECTIONS`` list via
``StealthSession``, then downloads any PDF attachments found via its own
``download_pdfs_for_site`` (plain ``requests`` — none of these three
asset hosts need special headers, unlike government.se's Cloudflare-
protected contentassets host).

Usage:
    python -m scripts.collect_nz_govt [--limit N] [--no-pdf-download]
    python -m scripts.collect_nz_govt --site treasury [--limit N]
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

DB_PATH = _ROOT / "libertree-app" / "data" / "libertree.db"
SHEET = "뉴질랜드완료"


def _load_module(filename: str, mod_name: str):
    """Load a custom crawler module by path (filename has hyphens)."""
    py_path = _ROOT / "crawler" / "sites" / "custom" / filename
    spec = importlib.util.spec_from_file_location(mod_name, py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_treasury_mod = _load_module("treasury-govt-nz-publications.py", "treasury_govt_nz_publications")
_health_mod = _load_module("health-govt-nz-publications.py", "health_govt_nz_publications")
_justice_mod = _load_module("justice-govt-nz-publications.py", "justice_govt_nz_publications")

SITES = {
    "treasury": {
        "crawler_cls": _treasury_mod.TreasuryGovtNzPublicationsCrawler,
        "download_pdfs": _treasury_mod.download_pdfs_for_site,
    },
    "health": {
        "crawler_cls": _health_mod.HealthGovtNzPublicationsCrawler,
        "download_pdfs": _health_mod.download_pdfs_for_site,
    },
    "justice": {
        "crawler_cls": _justice_mod.JusticeGovtNzPublicationsCrawler,
        "download_pdfs": _justice_mod.download_pdfs_for_site,
    },
}


def _run_site(conn, key: str, spec: dict, limit, skip_pdf_download: bool) -> None:
    crawler_cls = spec["crawler_cls"]
    site_id = crawler_cls.site_id
    site_url = crawler_cls.base_url

    _ldb.upsert_site(
        conn,
        site_id=site_id,
        site_name=crawler_cls.site_name,
        site_url=site_url,
        sheet=SHEET,
    )

    pre = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
    ).fetchone()[0]

    print(f"\n=== {site_id} collection start -> {DB_PATH} "
          f"(existing={pre}, sections={len(crawler_cls.SECTIONS)}) ===", flush=True)
    t0 = time.time()
    try:
        crawler = crawler_cls(db_conn=conn, delay=1.5)
        saved = crawler.crawl(limit=limit)
    except Exception as exc:
        print(f"--- ERROR: {type(exc).__name__}: {exc}", flush=True)
        saved = 0

    post = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
    ).fetchone()[0]
    added = post - pre

    pdf_ok = pdf_failed = 0
    if not skip_pdf_download:
        pdf_ok, pdf_failed = spec["download_pdfs"](conn, site_id=site_id)

    print(f"=== {site_id} DONE. saved={saved} added={added} total={post} "
          f"pdf_ok={pdf_ok} pdf_failed={pdf_failed} "
          f"({time.time() - t0:.0f}s) ===", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max documents to save per site (default: all)")
    ap.add_argument("--site", choices=sorted(SITES.keys()), default=None,
                    help="Run only this site (default: all three)")
    ap.add_argument("--no-pdf-download", action="store_true",
                    help="Skip the post-crawl PDF download pass")
    args = ap.parse_args()

    conn = _ldb.open_db(DB_PATH)
    _ldb.init_db(conn)

    keys = [args.site] if args.site else list(SITES.keys())
    for key in keys:
        _run_site(conn, key, SITES[key], args.limit, args.no_pdf_download)

    conn.close()


if __name__ == "__main__":
    main()
