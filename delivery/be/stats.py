"""Read-only aggregate queries for the delivery database dashboard."""
from __future__ import annotations

from datetime import datetime, timezone


def collect_stats(conn) -> dict:
    """Return measured DB totals and taxonomy inputs without scanning blob files."""
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
        "measured_at": datetime.now(timezone.utc),
    }

