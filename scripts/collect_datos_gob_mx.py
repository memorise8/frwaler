# -*- coding: utf-8 -*-
"""Collect datos.gob.mx CKAN datasets, one CKAN group/organization per site.

Reuses the existing ``DatosGobMxDatasetCrawler`` (CKAN package_search API)
but parametrises the ``GROUP_FILTER`` / ``START_URL`` / ``site_id`` so each
Mexican ministry's dataset collection lands as its own libertree ``sites``
row. Writes directly into ``libertree-app/data/libertree.db``.

Usage:
    python -m scripts.collect_datos_gob_mx [--limit N] [--only key1,key2]
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
_CRAWLER_PY = _ROOT / "crawler" / "sites" / "custom" / "datos-gob-mx-dataset.py"
_spec = importlib.util.spec_from_file_location("datos_gob_mx_dataset", _CRAWLER_PY)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DatosGobMxDatasetCrawler = _mod.DatosGobMxDatasetCrawler

DB_PATH = _ROOT / "libertree-app" / "data" / "libertree.db"
SHEET = "Mexico Ministries"

# (key, ckan_fq_filter, human ministry label, original xlsx URL)
# ciencia_tecnologia is intentionally excluded — already collected.
GROUPS = [
    ("agricultura", "groups:agricultura", "Agriculture",
     "https://datos.gob.mx/dataset/?groups=agricultura"),
    ("cultura", "groups:cultura", "Culture",
     "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=cultura"),
    ("territorio", "groups:territorio", "Territorial development",
     "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=territorio"),
    ("presupuesto", "groups:presupuesto", "Finance and Public Credit",
     "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=presupuesto"),
    ("seguridad", "groups:seguridad", "Security and Citizen Protection",
     "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=seguridad"),
    ("turismo", "groups:turismo", "Tourism",
     "https://datos.gob.mx/dataset/?_groups_limit=0&groups=turismo"),
    ("secretaria_salud", "organization:secretaria_salud", "Health",
     "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&organization=secretaria_salud"),
    ("secretaria_trabajo", "organization:secretaria_trabajo", "Labor / Welfare",
     "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&organization=secretaria_trabajo"),
]


def _make_crawler_cls(key: str, fq: str, url: str):
    """Subclass the datos crawler with per-group site_id / filter overrides."""
    return type(
        f"DatosGobMx_{key}",
        (DatosGobMxDatasetCrawler,),
        {
            "site_id": f"datos-gob-mx-{key.replace('_', '-')}",
            "site_name": f"datos.gob.mx — {key}",
            "base_url": "https://datos.gob.mx",
            "START_URL": url,
            "GROUP_FILTER": fq,
        },
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Max datasets per group (default: all)")
    ap.add_argument("--only", type=str, default=None,
                    help="Comma-separated group keys to run (default: all)")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None
    groups = [g for g in GROUPS if not only or g[0] in only]

    conn = _ldb.open_db(DB_PATH)
    _ldb.init_db(conn)

    grand_total = 0
    print(f"=== datos.gob.mx collection start: {len(groups)} groups -> {DB_PATH}",
          flush=True)
    for key, fq, label, url in groups:
        cls = _make_crawler_cls(key, fq, url)
        site_id = cls.site_id
        _ldb.upsert_site(conn, site_id=site_id,
                         site_name=f"Custom: {site_id}", site_url=url, sheet=SHEET)
        pre = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
        ).fetchone()[0]

        print(f"\n--- [{key}] {label}  (fq={fq}) start; existing={pre} ---",
              flush=True)
        t0 = time.time()
        try:
            crawler = cls(db_conn=conn, delay=1.0)
            saved = crawler.crawl(limit=args.limit)
        except Exception as exc:  # keep going to the next group
            print(f"--- [{key}] ERROR: {type(exc).__name__}: {exc}", flush=True)
            saved = 0

        post = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE site_id = ?", (site_id,)
        ).fetchone()[0]
        added = post - pre
        grand_total += added
        print(f"--- [{key}] done: saved={saved} added={added} "
              f"total={post} ({time.time() - t0:.0f}s) ---", flush=True)

    conn.close()
    print(f"\n=== ALL DONE. Newly added documents: {grand_total} ===", flush=True)


if __name__ == "__main__":
    main()
