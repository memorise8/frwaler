"""Read-only operational evidence. Never connect to crawled sites on GET."""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from delivery.be.site_names import display_site_name

from .inventory import inventory
from .results import read_result, report_dir, utc_now


def crawler_overview(root: Path | None = None) -> dict:
    data = inventory()
    now = datetime.now(timezone.utc)
    stale_hours = max(1, float(os.environ.get("CRAWLER_HEALTH_STALE_HOURS", "48")))
    sites = [{**site, "site_name": display_site_name(site["site_name"]),
              "probe": read_result(root or report_dir(), site, now=now, stale_hours=stale_hours)}
             for site in data["sites"]]
    return {"sites": sites, "registry_errors": data["registry_errors"], "measured_at": now.isoformat(),
            "summary": {**data["summary"], "probes": dict(Counter(r["probe"]["state"] for r in sites))},
            "scope": "listing_page1_only", "stale_hours": stale_hours}


def worker_rows(conn) -> list[dict]:
    # A long crawl refreshes its lease on a separate connection, whereas idle
    # workers update the heartbeat. Neither proves the source is yielding data.
    return [dict(row) for row in conn.execute("""
        SELECT h.worker_id, h.last_seen_at,
               (h.last_seen_at > clock_timestamp() - interval '120 seconds'
                OR EXISTS (SELECT 1 FROM crawl_jobs j WHERE j.worker_id=h.worker_id
                  AND j.status IN ('running','cancelling') AND j.lease_expires_at>clock_timestamp())) AS alive,
               (SELECT min(j.started_at) FROM crawl_jobs j WHERE j.worker_id=h.worker_id
                  AND j.status IN ('running','cancelling')) AS crawl_started_at
        FROM translation_worker_heartbeats h ORDER BY h.worker_id
    """).fetchall()]


def system_overview(conn, *, blob_root: Path | None = None) -> dict:
    from delivery.db.schema import verify_required_schema
    from crawler.blob_storage import get_blob_path

    result = {"measured_at": utc_now(), "database": "reachable", "schema": "ready",
              "schema_issue": None, "workers": [], "queue": {}, "blob_sample": {"state": "unchecked"}}
    # All queries are SELECTs; report an old schema without applying migrations.
    try:
        verify_required_schema(conn)
    except RuntimeError as exc:
        result.update(schema="migration_required", schema_issue=str(exc))
    tables = {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
    cols = {(r["table_name"], r["column_name"]) for r in conn.execute(
        "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public'").fetchall()}
    worker_columns = {("translation_worker_heartbeats", "last_seen_at"),
                      ("crawl_jobs", "lease_expires_at"), ("crawl_jobs", "worker_id"), ("crawl_jobs", "started_at")}
    if worker_columns <= cols:
        result["workers"] = worker_rows(conn)
    if "file_worker_heartbeat" in tables:
        row = conn.execute("SELECT last_seen_at,last_seen_at>now()-interval '120 seconds' AS alive FROM file_worker_heartbeat WHERE id=1").fetchone()
        result["workers"].append({"worker_id": "file-worker", "last_seen_at": row['last_seen_at'] if row else None,
                                  "alive": bool(row and row['alive']), "crawl_started_at": None})
    if "crawl_jobs" in tables:
        result["queue"] = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status,count(*) AS n FROM crawl_jobs GROUP BY status").fetchall()}
    if "crawl_schedules" in tables:
        result["enabled_schedules"] = conn.execute(
            "SELECT count(*) AS n FROM crawl_schedules WHERE enabled").fetchone()["n"]
    if {("documents", "pdf_downloaded"), ("documents", "seq_id")} <= cols:
        rows = conn.execute("SELECT seq_id FROM documents WHERE pdf_downloaded=1 ORDER BY seq_id DESC LIMIT 20").fetchall()
        root = blob_root or Path(os.environ.get("LIBERTREE_BLOB_ROOT", "/data/blob"))
        missing = invalid = 0
        for row in rows:
            path = get_blob_path(row["seq_id"], "pdf", root=root)
            try:
                with path.open("rb") as handle:
                    if b"%PDF-" not in handle.read(1024):
                        invalid += 1
            except OSError:
                missing += 1
        state = "no_sample" if not rows else "missing" if missing else "invalid" if invalid else "sample_ok"
        result["blob_sample"] = {"state": state, "checked": len(rows), "missing": missing,
                                 "invalid": invalid, "scope": "latest_20_flagged_pdfs_only"}
    return result
