# -*- coding: utf-8 -*-
"""crawl_jobs 큐 조작 — 원자적 소비(SKIP LOCKED)."""
from __future__ import annotations

from typing import Optional


def enqueue_job(conn, site_id, mode="incremental", limit_n=None, requested_by=None) -> int:
    row = conn.execute(
        """
        INSERT INTO crawl_jobs (site_id, mode, limit_n, requested_by)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (site_id, mode, limit_n, requested_by),
    ).fetchone()
    conn.commit()
    return row["id"]


def claim_next_job(conn) -> Optional[dict]:
    """Atomically claim one queued job (status -> running). None if queue empty."""
    row = conn.execute(
        """
        UPDATE crawl_jobs SET status='running', started_at=now()
         WHERE id = (
            SELECT id FROM crawl_jobs
             WHERE status='queued'
             ORDER BY created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1
         )
        RETURNING id, site_id, mode, limit_n
        """
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return {"id": row["id"], "site_id": row["site_id"], "mode": row["mode"], "limit_n": row["limit_n"]}


def finish_job(conn, job_id, saved_count) -> None:
    conn.execute(
        "UPDATE crawl_jobs SET status='done', saved_count=%s, finished_at=now() WHERE id=%s",
        (int(saved_count or 0), job_id),
    )
    conn.commit()


def fail_job(conn, job_id, error) -> None:
    conn.execute(
        "UPDATE crawl_jobs SET status='failed', error=%s, finished_at=now() WHERE id=%s",
        ((error or "")[:2000], job_id),
    )
    conn.commit()
