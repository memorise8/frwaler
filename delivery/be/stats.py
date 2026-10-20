"""Read-only aggregate queries for the delivery database dashboard."""
from __future__ import annotations

from datetime import datetime, timezone


def collect_stats(conn) -> dict:
    """Return DB totals and taxonomy inputs without scanning blob files.

    Totals come from document_counts when it has been refreshed (O(sites)
    instead of O(documents)); otherwise the live aggregate queries run.
    """
    from delivery.be.counts import counts_available
    if counts_available(conn):
        return _stats_from_counts(conn)
    overview = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM sites) AS sites,
          count(*) AS documents,
          count(*) FILTER (WHERE pdf_downloaded <> 0) AS pdf_downloaded,
          count(*) FILTER (WHERE text_extracted <> 0) AS text_extracted,
          COALESCE(sum(pdf_size_bytes) FILTER (WHERE pdf_downloaded <> 0), 0) AS pdf_bytes,
          max(collected_at) AS latest_collected_at
        FROM documents
        """
    ).fetchone()
    by_sheet = conn.execute(
        """
        SELECT COALESCE(s.sheet, '') AS key, count(DISTINCT s.site_id) AS sites,
               count(d.seq_id) AS documents
        FROM sites s LEFT JOIN documents d USING (site_id)
        GROUP BY COALESCE(s.sheet, '') ORDER BY documents DESC, key
        """
    ).fetchall()
    by_site = conn.execute(
        """
        SELECT s.site_id AS key, count(d.seq_id) AS documents
        FROM sites s LEFT JOIN documents d USING (site_id)
        GROUP BY s.site_id ORDER BY documents DESC, key
        """
    ).fetchall()
    integrity = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM documents d LEFT JOIN sites s USING (site_id)
             WHERE s.site_id IS NULL) AS orphan_documents,
          count(*) FILTER (WHERE pdf_downloaded <> 0
                            AND (pdf_size_bytes IS NULL OR pdf_sha256 IS NULL)) AS missing_pdf_metadata
        FROM documents
        """
    ).fetchone()
    return {
        "overview": dict(overview),
        "by_sheet": [dict(row) for row in by_sheet],
        "by_site": [dict(row) for row in by_site],
        "integrity": {
            **dict(integrity),
            "blob_check": {"status": "not_measured"},
        },
        "source": "live",
        "measured_at": datetime.now(timezone.utc),
    }



def _stats_from_counts(conn) -> dict:
    overview = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM sites) AS sites,
          COALESCE(sum(n), 0)::bigint AS documents,
          COALESCE(sum(n) FILTER (WHERE has_pdf), 0)::bigint AS pdf_downloaded,
          COALESCE(sum(n) FILTER (WHERE has_text), 0)::bigint AS text_extracted,
          COALESCE(sum(pdf_bytes), 0)::bigint AS pdf_bytes,
          (SELECT max(collected_at) FROM documents) AS latest_collected_at
        FROM document_counts
        """
    ).fetchone()
    by_sheet = conn.execute(
        """
        SELECT COALESCE(s.sheet, '') AS key, count(DISTINCT s.site_id) AS sites,
               COALESCE(sum(c.n), 0)::bigint AS documents
        FROM sites s LEFT JOIN document_counts c USING (site_id)
        GROUP BY COALESCE(s.sheet, '') ORDER BY documents DESC, key
        """
    ).fetchall()
    by_site = conn.execute(
        """
        SELECT s.site_id AS key, COALESCE(sum(c.n), 0)::bigint AS documents
        FROM sites s LEFT JOIN document_counts c USING (site_id)
        GROUP BY s.site_id ORDER BY documents DESC, key
        """
    ).fetchall()
    integrity = conn.execute(
        """
        SELECT
          COALESCE(sum(c.n) FILTER (WHERE s.site_id IS NULL), 0)::bigint AS orphan_documents,
          COALESCE(sum(c.missing_pdf_metadata), 0)::bigint AS missing_pdf_metadata,
          min(c.refreshed_at) AS counts_refreshed_at
        FROM document_counts c LEFT JOIN sites s USING (site_id)
        """
    ).fetchone()
    integrity = dict(integrity)
    refreshed_at = integrity.pop("counts_refreshed_at")
    return {
        "overview": dict(overview),
        "by_sheet": [dict(row) for row in by_sheet],
        "by_site": [dict(row) for row in by_site],
        "integrity": {
            **integrity,
            "blob_check": {"status": "not_measured"},
        },
        "source": "counts",
        "counts_refreshed_at": refreshed_at,
        "measured_at": datetime.now(timezone.utc),
    }
