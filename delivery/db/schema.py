# -*- coding: utf-8 -*-
"""납품 전용 Postgres 스키마 (documents/sites 는 crawler.db_pg 담당)."""
from __future__ import annotations


def init_delivery_schema(conn) -> None:
    """Create delivery control tables if absent (idempotent). Phase 0: crawl_jobs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_jobs (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            site_id       TEXT NOT NULL,
            mode          TEXT NOT NULL DEFAULT 'incremental',
            status        TEXT NOT NULL DEFAULT 'queued',
            limit_n       INTEGER,
            saved_count   INTEGER NOT NULL DEFAULT 0,
            error         TEXT,
            requested_by  TEXT,
            created_at    TIMESTAMPTZ DEFAULT now(),
            started_at    TIMESTAMPTZ,
            finished_at   TIMESTAMPTZ,
            retry_of      BIGINT REFERENCES crawl_jobs(id),
            retried_by    BIGINT REFERENCES crawl_jobs(id)
        )
        """
    )
    # Upgrade Phase 0 databases without requiring a destructive migration.
    conn.execute("ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS retry_of BIGINT REFERENCES crawl_jobs(id)")
    conn.execute("ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS retried_by BIGINT REFERENCES crawl_jobs(id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_retry_of ON crawl_jobs(retry_of) WHERE retry_of IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON crawl_jobs(status, created_at)"
    )
    conn.commit()
