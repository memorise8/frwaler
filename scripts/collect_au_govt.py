# -*- coding: utf-8 -*-
"""Collect Australian government publication sources into
``libertree-app/data/libertree.db``.

Runs all 6 custom AU government crawlers built for the "호주완료"
uncollected rows in ``scripts/audit/uncollected_probe.csv``
(``crawler/sites/custom/*-gov-au-publications.py``), each covering one
domain:

  - ag-gov-au-publications       (Attorney-General's Department)
  - defence-gov-au-publications  (Department of Defence)
  - acma-gov-au-publications     (Australian Communications and Media Authority)
  - nhmrc-gov-au-publications    (National Health and Medical Research Council)
  - dfat-gov-au-publications     (Dept. of Foreign Affairs and Trade)
  - health-gov-au-publications   (Dept. of Health, Disability and Ageing)

Each crawler paginates its own hard-coded ``SECTIONS`` list via
``crawler.stealth_fetcher.StealthSession``, then PDF attachments are
downloaded via each module's own ``download_pdfs_for_site`` (curl_cffi
with a Chrome TLS fingerprint — plain ``requests`` is unreliable against
these gov.au ``sites/default/files`` hosts in testing).

Deferred (not built in this batch — see crawler docstrings / handoff
notes for why): aihw.gov.au (403s across all 3 StealthSession layers —
stronger bot protection than Cloudflare's usual challenge page),
finance.gov.au (Akamai interstitial JS challenge + HTTP2 protocol error
under playwright), agriculture.gov.au (listing is a generic SharePoint
site-search results page fanning out to heterogeneous, non-uniform
target pages), and the 8 AU singleton domains from the probe (1 URL
each): dataverse.ada.edu.au, dataexplorer.abs.gov.au, apo.ansto.gov.au,
industry.gov.au, dss.gov.au, dva.gov.au, search.abs.gov.au, aims.gov.au.

Usage:
    python -m scripts.collect_au_govt [--limit N] [--no-pdf-download]
    python -m scripts.collect_au_govt --sites ag,defence  # subset

``--limit`` caps the number of documents saved *per crawler* this run
(handy for smoke tests); omit it for a full collection.
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
SHEET = "호주완료"
CUSTOM_DIR = _ROOT / "crawler" / "sites" / "custom"

# (module filename stem, crawler class name, short key for --sites)
_CRAWLER_SPECS = [
    ("ag-gov-au-publications", "AgGovAuPublicationsCrawler", "ag"),
    ("defence-gov-au-publications", "DefenceGovAuPublicationsCrawler", "defence"),
    ("acma-gov-au-publications", "AcmaGovAuPublicationsCrawler", "acma"),
    ("nhmrc-gov-au-publications", "NhmrcGovAuPublicationsCrawler", "nhmrc"),
    ("dfat-gov-au-publications", "DfatGovAuPublicationsCrawler", "dfat"),
    ("health-gov-au-publications", "HealthGovAuPublicationsCrawler", "health"),
]


def _load_module(file_stem: str):
    path = CUSTOM_DIR / f"{file_stem}.py"
    spec = importlib.util.spec_from_file_location(file_stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max documents to save per crawler this run (default: all)")
    ap.add_argument("--no-pdf-download", action="store_true",
                    help="Skip the post-crawl PDF download pass")
    ap.add_argument("--sites", type=str, default=None,
                    help="Comma-separated subset of crawler keys to run "
                         "(ag,defence,acma,nhmrc,dfat,health). Default: all")
    args = ap.parse_args()

    wanted = None
    if args.sites:
        wanted = {s.strip().lower() for s in args.sites.split(",") if s.strip()}

    conn = _ldb.open_db(DB_PATH)
    _ldb.init_db(conn)

    totals = {"saved": 0, "added": 0, "pdf_ok": 0, "pdf_failed": 0}
    t_start = time.time()

    for file_stem, class_name, key in _CRAWLER_SPECS:
        if wanted is not None and key not in wanted:
            continue

        mod = _load_module(file_stem)
        crawler_cls = getattr(mod, class_name)
        site_id = crawler_cls.site_id

        _ldb.upsert_site(
            conn,
            site_id=site_id,
            site_name=crawler_cls.site_name,
            site_url=crawler_cls.base_url,
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
            saved = crawler.crawl(limit=args.limit)
        except Exception as exc:
            print(f"--- ERROR: {type(exc).__name__}: {exc}", flush=True)
            saved = 0

        post = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
        ).fetchone()[0]
        added = post - pre

        pdf_ok = pdf_failed = 0
        if not args.no_pdf_download:
            pdf_ok, pdf_failed = mod.download_pdfs_for_site(conn, site_id=site_id)

        totals["saved"] += saved
        totals["added"] += added
        totals["pdf_ok"] += pdf_ok
        totals["pdf_failed"] += pdf_failed

        print(f"=== {site_id} DONE. saved={saved} added={added} total={post} "
              f"pdf_ok={pdf_ok} pdf_failed={pdf_failed} "
              f"({time.time() - t0:.0f}s) ===", flush=True)

    conn.close()
    print(f"\n=== ALL DONE. saved={totals['saved']} added={totals['added']} "
          f"pdf_ok={totals['pdf_ok']} pdf_failed={totals['pdf_failed']} "
          f"({time.time() - t_start:.0f}s) ===", flush=True)


if __name__ == "__main__":
    main()
