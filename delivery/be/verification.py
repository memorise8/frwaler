"""Per-site crawl outcomes, read from this deployment's own job history.

The crawler catalogue's 정상/실패 verdict is a snapshot taken on one network at
one moment (2026-08-06 for the delivered CSV) and nothing ever updates it: no
code writes that file. Several of its failures are recorded as "우리 IP
차단(403/WAF) — 클라이언트 egress에서 재확인 필요, 코드문제 아님", which is an
explicit instruction to re-check from the customer's network -- and until now
there was nowhere to put the answer.

This aggregate is that answer. It says only what this database can prove: how
many jobs a site has run here, how the latest one ended, and whether any run
ever saved a document. It does not re-classify the catalogue; the console shows
the two side by side so a disagreement is visible rather than silently resolved
in favour of either.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def collect_verification(conn, *, measured_at: datetime | None = None) -> dict:
    """Return each site's job history summary. Read-only, one query."""
    as_of = measured_at or datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT DISTINCT ON (site_id)
               site_id,
               status        AS last_status,
               saved_count   AS last_saved_count,
               error         AS last_error,
               finished_at   AS last_finished_at,
               count(*)      OVER (PARTITION BY site_id) AS jobs,
               -- The strongest claim this deployment can make: a run that
               -- actually stored a document proves the crawler reaches the
               -- source from here, whatever the catalogue snapshot says.
               max(saved_count) OVER (PARTITION BY site_id) AS best_saved_count
          FROM crawl_jobs
         ORDER BY site_id, created_at DESC, id DESC
        """
    ).fetchall()

    sites = []
    for row in rows:
        item = dict(row)
        item["last_finished_at"] = _utc(item["last_finished_at"])
        item["last_saved_count"] = int(item["last_saved_count"] or 0)
        item["best_saved_count"] = int(item["best_saved_count"] or 0)
        item["jobs"] = int(item["jobs"] or 0)
        sites.append(item)

    return {
        "sites": sites,
        "summary": {
            "sites_with_jobs": len(sites),
            "sites_with_saved_documents": sum(1 for s in sites if s["best_saved_count"] > 0),
        },
        "measured_at": _utc(as_of),
    }
