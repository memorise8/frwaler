# -*- coding: utf-8 -*-
"""Register the delivered crawler catalogue in the ``sites`` table.

``POST /jobs`` refuses a site that is not in ``sites`` (delivery/be/app.py), and
nothing else populates that table on a new install: ``upsert_site`` is only ever
called from a crawler that is already saving documents, and the schema migration
creates tables without rows. A customer starting from an empty database
therefore gets ``404 site not found`` for all 804 crawlers -- every single-site
run, every bulk run, every schedule -- with no way to fix it from the browser.
Seeding closes that, and it is the console's whole premise that an operator
never has to reach for a shell.

Seeded from the audit catalogue rather than the full registry: the registry
holds 1,242 keys (804 catalogue entries plus 438 aliases and variants), and the
console presents exactly the 804. Seeding all of them would make /stats report
1,242 data sources while the crawler screen shows 804, and would put 438 rows
into the "수집기 미등록" notice on day one. The registry still supplies
``site_name`` and ``base_url``, which the catalogue CSV does not carry and the
table requires.

Purely additive: ``ON CONFLICT DO NOTHING`` means an existing deployment keeps
every row it already has, and no document is ever written.
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path

CATALOGUE_RELATIVE = ("scripts", "audit", "crawler_status_final.csv")


def _catalogue_candidates() -> list[Path]:
    override = os.environ.get("LIBERTREE_CATALOGUE_CSV")
    if override:
        return [Path(override)]
    here = Path(__file__).resolve()
    # /app/delivery/db/seed.py -> /app, and the repo layout when run from source.
    roots = [here.parent.parent.parent, Path.cwd()]
    return [root.joinpath(*CATALOGUE_RELATIVE) for root in roots]


def find_catalogue() -> Path | None:
    for candidate in _catalogue_candidates():
        if candidate.is_file():
            return candidate
    return None


def read_catalogue(path: Path) -> list[dict]:
    with io.open(path, encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if (row.get("site_id") or "").strip()]


def _registry():
    from crawler.sites import CRAWLERS
    return CRAWLERS


def seed_catalogue_sites(conn, *, catalogue_path=None, registry=None) -> int:
    """Insert every catalogue crawler as a site. Returns the number inserted.

    Missing catalogue file or a catalogue entry with no crawler behind it is not
    an error: the first means this deployment ships without the audit snapshot,
    the second means an id that could never be run anyway. Both are skipped so a
    migration never fails over cosmetic catalogue drift.
    """
    path = Path(catalogue_path) if catalogue_path else find_catalogue()
    if path is None or not path.is_file():
        return 0
    crawlers = _registry() if registry is None else registry

    inserted = 0
    for row in read_catalogue(path):
        site_id = (row.get("site_id") or "").strip()
        crawler = crawlers.get(site_id)
        if crawler is None:
            continue
        site_url = (getattr(crawler, "base_url", "") or "").strip()
        if not site_url:
            continue
        site_name = (row.get("site_name") or "").strip() or (getattr(crawler, "site_name", "") or "").strip() or site_id
        result = conn.execute(
            "INSERT INTO sites(site_id, site_name, site_url) VALUES(%s,%s,%s)"
            " ON CONFLICT (site_id) DO NOTHING RETURNING site_id",
            (site_id, site_name, site_url),
        ).fetchone()
        if result is not None:
            inserted += 1
    conn.commit()
    return inserted
