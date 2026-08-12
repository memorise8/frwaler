# -*- coding: utf-8 -*-
"""crawl_jobs 큐 조작 — 원자적 소비(SKIP LOCKED)."""
from __future__ import annotations

from typing import Optional


def _log(conn,job_id:int,event:str,message:str,level:str="info") -> None:
    conn.execute("INSERT INTO crawl_job_logs(job_id,level,event,message) VALUES(%s,%s,%s,%s)",
                 (job_id,level,event,(message or "")[:500]))


def enqueue_job(conn, site_id, mode="incremental", limit_n=None, requested_by=None) -> int:
    row = conn.execute(
        """
        INSERT INTO crawl_jobs (site_id, mode, limit_n, requested_by)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (site_id, mode, limit_n, requested_by),
    ).fetchone()
    _log(conn,row["id"],"queued","수집 작업이 등록되었습니다.")
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
    if row:_log(conn,row["id"],"started","Worker가 수집을 시작했습니다.")
    conn.commit()
    if row is None:
        return None
    return {"id": row["id"], "site_id": row["site_id"], "mode": row["mode"], "limit_n": row["limit_n"]}


def list_jobs(conn, *, status=None, limit=50, offset=0) -> list[dict]:
    """Return newest jobs, optionally filtered by an exact status."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    if status is None:
        rows = conn.execute(
            "SELECT * FROM crawl_jobs ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM crawl_jobs WHERE status=%s
               ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
            (status, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def cancel_job(conn, job_id) -> Optional[dict]:
    """Cancel a queued job atomically; return None when it is not queued."""
    row = conn.execute(
        """UPDATE crawl_jobs
              SET status='cancelled', finished_at=now(), error=NULL
            WHERE id=%s AND status='queued'
        RETURNING *""",
        (job_id,),
    ).fetchone()
    if row:_log(conn,job_id,"cancelled","대기 중인 작업이 취소되었습니다.","warning")
    conn.commit()
    return dict(row) if row is not None else None


def retry_job(conn, job_id, *, requested_by=None) -> Optional[dict]:
    """Create a fresh queued job from a failed/cancelled job.

    The original row remains unchanged so operators retain its failure history.
    A row lock prevents two concurrent retry requests from both succeeding.
    """
    source = conn.execute(
        """SELECT id, site_id, mode, limit_n, requested_by
             FROM crawl_jobs
            WHERE id=%s AND status IN ('failed', 'cancelled') AND retried_by IS NULL
            FOR UPDATE""",
        (job_id,),
    ).fetchone()
    if source is None:
        conn.rollback()
        return None
    row = conn.execute(
        """INSERT INTO crawl_jobs (site_id, mode, limit_n, requested_by, retry_of)
             VALUES (%s, %s, %s, %s, %s)
          RETURNING *""",
        (
            source["site_id"], source["mode"], source["limit_n"],
            requested_by if requested_by is not None else source["requested_by"],
            source["id"],
        ),
    ).fetchone()
    # Mark the source as consumed by this retry while preserving its status.
    conn.execute(
        "UPDATE crawl_jobs SET retried_by=%s WHERE id=%s AND retried_by IS NULL",
        (row["id"], source["id"]),
    )
    _log(conn,row["id"],"retried",f"작업 #{source['id']}에서 재시도되었습니다.","warning")
    # If another transaction already consumed it, do not leave a duplicate retry.
    consumed = conn.execute(
        "SELECT retried_by FROM crawl_jobs WHERE id=%s", (source["id"],)
    ).fetchone()
    if consumed["retried_by"] != row["id"]:
        conn.rollback()
        return None
    conn.commit()
    return dict(row)


def finish_job(conn, job_id, saved_count) -> None:
    conn.execute(
        """UPDATE crawl_jobs SET status='done', saved_count=%s, finished_at=now()
            WHERE id=%s AND status='running'""",
        (int(saved_count or 0), job_id),
    )
    _log(conn,job_id,"completed",f"수집 완료: 신규 문서 {int(saved_count or 0)}건")
    conn.commit()


def fail_job(conn, job_id, error) -> None:
    conn.execute(
        """UPDATE crawl_jobs SET status='failed', error=%s, finished_at=now()
            WHERE id=%s AND status='running'""",
        ((error or "")[:2000], job_id),
    )
    _log(conn,job_id,"failed","수집 작업이 실패했습니다.","error")
    conn.commit()


def job_detail(conn,job_id:int) -> dict | None:
    row=conn.execute("SELECT * FROM crawl_jobs WHERE id=%s",(job_id,)).fetchone()
    if not row:return None
    logs=conn.execute("SELECT id,level,event,message,created_at FROM crawl_job_logs WHERE job_id=%s ORDER BY id",(job_id,)).fetchall()
    return {"job":dict(row),"logs":[dict(item) for item in logs]}
