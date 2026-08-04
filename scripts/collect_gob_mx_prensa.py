# -*- coding: utf-8 -*-
"""Collect gob.mx "Archivo de prensa" press releases, one Mexican ministry per site.

Reuses the abstract ``GobMxArchivoPrensaCrawler`` (HTML scrape of
``https://www.gob.mx/{ministry}/archivo/prensa`` via
``crawler.stealth_fetcher.StealthSession``) and parametrises
``MINISTRY_SLUG`` / ``MINISTRY_LABEL`` / ``site_id`` so each ministry's
press releases land as their own libertree ``sites`` row. Writes directly
into ``libertree-app/data/libertree.db``.

science/technology (secihti) is intentionally excluded — already
collected separately via ``secihti-mx-sala-de-prensa``.

Usage:
    python -m scripts.collect_gob_mx_prensa [--limit N] [--only slug1,slug2]
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
_CRAWLER_PY = _ROOT / "crawler" / "sites" / "custom" / "gob-mx-archivo-prensa.py"
_spec = importlib.util.spec_from_file_location("gob_mx_archivo_prensa", _CRAWLER_PY)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
GobMxArchivoPrensaCrawler = _mod.GobMxArchivoPrensaCrawler

DB_PATH = _ROOT / "libertree-app" / "data" / "libertree.db"
SHEET = "Mexico Ministries"

# (slug, English label) — secihti (Science/Technology) already collected
# separately; do NOT include it here.
MINISTRIES = [
    ("agricultura", "Agriculture"),
    ("bienestar", "Welfare"),
    ("sict", "Communications and Transportation"),
    ("defensa", "Defense"),
    ("sedatu", "Territorial development"),
    ("se", "Economy"),
    ("sep", "Public Education"),
    ("segob", "Interior"),
    ("shcp", "Finance and Public Credit"),
    ("semar", "Navy"),
    ("semarnat", "Environment and Natural Resources"),
    ("mujeres", "Women"),
    ("sre", "Foreign Affairs"),
    ("salud", "Health"),
    ("sspc", "Security and Citizen Protection"),
    ("stps", "Labor and Social Security"),
    ("sectur", "Tourism"),
    ("buengobierno", "Good governance"),
]


def _make_crawler_cls(slug: str, label: str):
    """Subclass the gob.mx archive crawler with per-ministry overrides."""
    site_id = f"gob-mx-{slug}"
    return type(
        f"GobMxArchivoPrensa_{slug}",
        (GobMxArchivoPrensaCrawler,),
        {
            "site_id": site_id,
            "site_name": f"gob.mx Archivo de Prensa — {label}",
            "base_url": f"https://www.gob.mx/{slug}/archivo/prensa",
            "MINISTRY_SLUG": slug,
            "MINISTRY_LABEL": label,
        },
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max articles per ministry (default: all)")
    ap.add_argument("--only", type=str, default=None,
                    help="Comma-separated ministry slugs to run (default: all)")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None
    ministries = [m for m in MINISTRIES if not only or m[0] in only]

    conn = _ldb.open_db(DB_PATH)
    _ldb.init_db(conn)

    grand_total = 0
    print(f"=== gob.mx archivo de prensa collection start: "
          f"{len(ministries)} ministries -> {DB_PATH}", flush=True)
    for slug, label in ministries:
        cls = _make_crawler_cls(slug, label)
        site_id = cls.site_id
        _ldb.upsert_site(conn, site_id=site_id, site_name=cls.site_name,
                          site_url=cls.base_url, sheet=SHEET)
        pre = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
        ).fetchone()[0]

        print(f"\n--- [{slug}] {label} start; existing={pre} ---", flush=True)
        t0 = time.time()
        try:
            crawler = cls(db_conn=conn, delay=1.0)
            saved = crawler.crawl(limit=args.limit)
        except Exception as exc:  # keep going to the next ministry
            print(f"--- [{slug}] ERROR: {type(exc).__name__}: {exc}", flush=True)
            saved = 0

        post = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
        ).fetchone()[0]
        added = post - pre
        grand_total += added
        print(f"--- [{slug}] done: saved={saved} added={added} "
              f"total={post} ({time.time() - t0:.0f}s) ---", flush=True)

    conn.close()
    print(f"\n=== ALL DONE. Newly added documents: {grand_total} ===", flush=True)


if __name__ == "__main__":
    main()
