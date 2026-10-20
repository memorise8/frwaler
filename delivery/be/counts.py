"""Pre-computed document counts for catalogue facets, totals and /stats.

``document_counts`` holds one row per (site_id, lang, has_pdf, has_text).
country and doc_type are not stored: both are a pure function of site_id via
the site taxonomy file, so they are derived at query time and a taxonomy edit
never requires a recount. lang comes from document_lang (``unknown`` when the
document has no row), has_pdf/has_text mirror ``pdf_downloaded <> 0`` and
``text_extracted <> 0`` (NULL stays NULL, exactly like the live filters).
At ~900 sites x a handful of languages x 4 file states the table stays at a
few thousand rows regardless of how many documents exist.

Refresh (full or per site) runs in one transaction: the GROUP BY result is
staged in a temporary table and then swapped in with DELETE + INSERT, so
readers keep seeing the previous counts until COMMIT and never an empty
table. An advisory lock serialises concurrent refreshes.

    python -m delivery.be.counts refresh              # all sites
    python -m delivery.be.counts refresh --site a --site b

Callers that already hold a connection (an import pipeline finishing a site)
use ``refresh_counts(conn, [site_id])`` directly.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from delivery.db.schema import COUNTS_TABLE_DDL

_REFRESH_LOCK = "delivery.document_counts.refresh"


def counts_available(conn) -> bool:
    """True when document_counts exists and holds at least one row."""
    row = conn.execute("SELECT to_regclass('public.document_counts') AS relation").fetchone()
    if row is None or row["relation"] is None:
        return False
    return conn.execute("SELECT EXISTS(SELECT 1 FROM document_counts) AS present").fetchone()["present"]


def refresh_counts(conn, sites: list[str] | None = None) -> dict:
    """Recompute counts for ``sites`` (all sites when None) and commit."""
    where = "WHERE d.site_id = ANY(%(sites)s)" if sites else ""
    params = {"sites": list(sites) if sites else None, "lock": _REFRESH_LOCK}
    started = time.monotonic()
    conn.execute(COUNTS_TABLE_DDL)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%(lock)s))", params)
    conn.execute("DROP TABLE IF EXISTS pg_temp.document_counts_stage")
    conn.execute(f"""CREATE TEMP TABLE document_counts_stage ON COMMIT DROP AS
      SELECT d.site_id, COALESCE(dl.lang, 'unknown') AS lang,
             d.pdf_downloaded <> 0 AS has_pdf, d.text_extracted <> 0 AS has_text,
             count(*) AS n,
             COALESCE(sum(d.pdf_size_bytes) FILTER (WHERE d.pdf_downloaded <> 0), 0)::bigint AS pdf_bytes,
             count(*) FILTER (WHERE d.pdf_downloaded <> 0
                               AND (d.pdf_size_bytes IS NULL OR d.pdf_sha256 IS NULL)) AS missing_pdf_metadata
      FROM documents d LEFT JOIN document_lang dl USING (seq_id)
      {where}
      GROUP BY 1, 2, 3, 4""", params)
    if sites:
        deleted = conn.execute("DELETE FROM document_counts WHERE site_id = ANY(%(sites)s)", params).rowcount
    else:
        deleted = conn.execute("DELETE FROM document_counts").rowcount
    inserted = conn.execute("""INSERT INTO document_counts(site_id, lang, has_pdf, has_text, n,
        pdf_bytes, missing_pdf_metadata, refreshed_at)
      SELECT site_id, lang, has_pdf, has_text, n, pdf_bytes, missing_pdf_metadata, now()
      FROM document_counts_stage""").rowcount
    conn.commit()
    return {"sites": list(sites) if sites else "all", "deleted_rows": deleted,
            "inserted_rows": inserted, "seconds": round(time.monotonic() - started, 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m delivery.be.counts")
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="recompute document_counts")
    refresh.add_argument("--site", action="append", dest="sites", metavar="SITE_ID",
                         help="recompute only this site (repeatable)")
    args = parser.parse_args(argv)
    dsn = os.environ.get("LIBERTREE_PG_DSN")
    if not dsn:
        raise SystemExit("LIBERTREE_PG_DSN is required")
    from crawler import db_pg
    conn = db_pg.open_db(dsn)
    try:
        result = refresh_counts(conn, args.sites)
    finally:
        conn.close()
    print(f"document_counts refresh: sites={result['sites']} rows={result['inserted_rows']} "
          f"seconds={result['seconds']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
