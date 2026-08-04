# -*- coding: utf-8 -*-
"""Collect Italy uncollected research-repository sources into libertree.db.

Runs the custom crawlers built for the "Italy Research centers" /
"Italy Ministries" uncollected rows of ``scripts/audit/uncollected_probe.csv``:

  API (reliable)                        approach
  ------------------------------------  --------------------------------------
  openaccess-inaf-it   OA@INAF          DSpace 7 REST (configuration=researchoutputs)
  earth-prints-org     Earth-Prints     DSpace 7 REST (configuration=researchoutputs)
  iris-unitn-it        IRIS Trento      OAI-PMH DIDL (REST/HTML Cloudflare-blocked)
  flore-unifi-it       FLORE Firenze    OAI-PMH DIDL (REST/HTML Cloudflare-blocked)

Dead / deferred (not run here):
  openstarts-units-it  OpenstarTS Trieste  DEAD — host connection times out
                                            (curl (28), verified 2026-08). Skip.

PDF download: INAF/Earth-Prints bitstreams download via plain ``requests``
(the generic ``crawler.pdf_downloader``). IRIS/FLORE bitstreams sit behind the
same Cloudflare WAF as their HTML/REST layer, so those PDFs stay pending
(``pdf_url`` recorded) for a later browser-based pass.

Usage:
    python -m scripts.collect_italy --limit 4                 # test all
    python -m scripts.collect_italy --site earth-prints-org --limit 4
    python -m scripts.collect_italy --limit 4 --no-pdf-download

NOTE: intended for SMALL test runs. Do NOT launch an unbounded full run while
background collections/surveys share libertree.db.
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
from crawler import pdf_downloader as _pdf  # noqa: E402

DB_PATH = _ROOT / "libertree-app" / "data" / "libertree.db"
_CUSTOM = _ROOT / "crawler" / "sites" / "custom"

# site_id -> (crawler .py filename, crawler class name, site_url, sheet)
SITES = {
    "openaccess-inaf-it": ("openaccess-inaf-it.py", "OpenaccessInafItCrawler",
                           "https://openaccess.inaf.it", "Italy Research centers"),
    "earth-prints-org": ("earth-prints-org.py", "EarthPrintsOrgCrawler",
                         "https://www.earth-prints.org", "Italy Research centers"),
    "iris-unitn-it": ("iris-unitn-it.py", "IrisUnitnItCrawler",
                      "https://iris.unitn.it", "Italy Research centers"),
    "flore-unifi-it": ("flore-unifi-it.py", "FloreUnifiItCrawler",
                       "https://flore.unifi.it", "Italy Research centers"),
}

DEFAULT_ORDER = ["earth-prints-org", "openaccess-inaf-it", "iris-unitn-it", "flore-unifi-it"]


def _load_crawler(filename, class_name):
    path = _CUSTOM / filename
    spec = importlib.util.spec_from_file_location(f"italy_{class_name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, class_name)


def _open_db_retry(retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            conn = _ldb.open_db(DB_PATH)
            _ldb.init_db(conn)
            return conn
        except Exception as exc:  # sqlite locked etc.
            last = exc
            if attempt < retries:
                print(f"[collect_italy] DB open failed ({exc}); retrying in 5s...")
                time.sleep(5)
    raise last


def _download_pdfs(conn, site_id, limit=None):
    query = (
        "SELECT seq_id, pdf_url FROM documents "
        "WHERE site_id = ? AND pdf_url IS NOT NULL AND pdf_url != '' "
        "AND (pdf_downloaded IS NULL OR pdf_downloaded = 0) ORDER BY seq_id"
    )
    params = [site_id]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    print(f"[{site_id}] pdf download: {len(rows)} pending")
    ok = failed = 0
    for row in rows:
        seq_id = row["seq_id"] if hasattr(row, "keys") else row[0]
        pdf_url = row["pdf_url"] if hasattr(row, "keys") else row[1]
        res = _pdf.download_pdf_for(conn, seq_id, pdf_url)
        if res.get("success"):
            ok += 1
            print(f"[{site_id}] pdf ok seq_id={seq_id} ({res.get('size_bytes')} bytes)")
        else:
            failed += 1
            print(f"[{site_id}] pdf FAIL seq_id={seq_id}: {res.get('error')}")
        time.sleep(0.5)
    print(f"[{site_id}] pdf download done: {ok} ok, {failed} failed")
    return ok, failed


def run_site(conn, site_id, limit, do_pdf):
    filename, class_name, site_url, sheet = SITES[site_id]
    crawler_cls = _load_crawler(filename, class_name)
    _ldb.upsert_site(conn, site_id=site_id, site_name=crawler_cls.site_name,
                     site_url=site_url, sheet=sheet)

    pre = conn.execute("SELECT COUNT(*) FROM documents WHERE site_id = ?",
                       (site_id,)).fetchone()[0]
    print(f"\n=== {site_id} start (existing={pre}, sheet='{sheet}') ===", flush=True)
    t0 = time.time()
    try:
        crawler = crawler_cls(db_conn=conn, delay=1.0)
        saved = crawler.crawl(limit=limit)
    except Exception as exc:
        print(f"--- ERROR {type(exc).__name__}: {exc}", flush=True)
        saved = 0
    post = conn.execute("SELECT COUNT(*) FROM documents WHERE site_id = ?",
                        (site_id,)).fetchone()[0]

    pdf_ok = pdf_failed = 0
    if do_pdf:
        pdf_ok, pdf_failed = _download_pdfs(conn, site_id, limit=limit)

    print(f"=== {site_id} done. saved={saved} added={post - pre} total={post} "
          f"pdf_ok={pdf_ok} pdf_failed={pdf_failed} ({time.time() - t0:.0f}s) ===",
          flush=True)
    return saved, post - pre, pdf_ok, pdf_failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max documents to save per site (default: all)")
    ap.add_argument("--site", choices=list(SITES), default=None,
                    help="Run only one site (default: all)")
    ap.add_argument("--no-pdf-download", action="store_true",
                    help="Skip the post-crawl PDF download pass")
    args = ap.parse_args()

    conn = _open_db_retry()
    sites = [args.site] if args.site else DEFAULT_ORDER
    grand = {"saved": 0, "added": 0, "pdf_ok": 0, "pdf_failed": 0}
    for site_id in sites:
        s, a, po, pf = run_site(conn, site_id, args.limit, not args.no_pdf_download)
        grand["saved"] += s
        grand["added"] += a
        grand["pdf_ok"] += po
        grand["pdf_failed"] += pf
    conn.close()
    print(f"\n=== ALL DONE. saved={grand['saved']} added={grand['added']} "
          f"pdf_ok={grand['pdf_ok']} pdf_failed={grand['pdf_failed']} ===", flush=True)


if __name__ == "__main__":
    main()
