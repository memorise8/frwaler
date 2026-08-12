"""Read-only site freshness queries for the delivery dashboard."""
from __future__ import annotations

from datetime import datetime, timezone


def _utc(value: datetime) -> datetime:
    """Normalize database timestamps for deterministic age calculations."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bucket(age_days: int | None) -> str:
    if age_days is None:
        return "never"
    if age_days <= 7:
        return "within_7_days"
    if age_days <= 30:
        return "8_to_30_days"
    if age_days <= 90:
        return "31_to_90_days"
    return "over_90_days"


def collect_freshness(conn, *, measured_at: datetime | None = None) -> dict:
    """Return each site's latest collection time and an exclusive age distribution.

    This is intentionally a single read-only aggregate query.  In particular, it
    does not initialize schemas or enqueue an incremental crawl.
    """
    as_of = _utc(measured_at or datetime.now(timezone.utc))
    rows = conn.execute(
        """
        SELECT s.site_id, s.site_name, s.sheet,
               count(d.seq_id) AS documents,
               max(d.collected_at) AS last_collected_at
        FROM sites s
        LEFT JOIN documents d ON d.site_id = s.site_id
        GROUP BY s.site_id, s.site_name, s.sheet
        ORDER BY max(d.collected_at) DESC NULLS LAST, s.site_id
        """
    ).fetchall()

    distribution = {
        "within_7_days": 0,
        "8_to_30_days": 0,
        "31_to_90_days": 0,
        "over_90_days": 0,
        "never": 0,
    }
    sites = []
    for row in rows:
        item = dict(row)
        last_collected_at = item["last_collected_at"]
        age_days = None
        if last_collected_at is not None:
            # Future timestamps (clock skew) are current rather than a negative age.
            age_days = max(0, (as_of - _utc(last_collected_at)).days)
        bucket = _bucket(age_days)
        distribution[bucket] += 1
        sites.append({**item, "age_days": age_days, "freshness_bucket": bucket})

    total_sites = len(sites)
    return {
        "summary": {
            "total_sites": total_sites,
            "with_documents": total_sites - distribution["never"],
            "never_collected": distribution["never"],
            "distribution": distribution,
        },
        "sites": sites,
        "measured_at": as_of,
    }
