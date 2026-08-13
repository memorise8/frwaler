# -*- coding: utf-8 -*-
"""crawl_jobs 큐 조작 — 원자적 소비(SKIP LOCKED)."""
from __future__ import annotations

from typing import Optional


class ActiveJobError(RuntimeError):
    pass


def _log(conn,job_id:int,event:str,message:str,level:str="info") -> None:
    conn.execute("INSERT INTO crawl_job_logs(job_id,level,event,message) VALUES(%s,%s,%s,%s)",
                 (job_id,level,event,(message or "")[:500]))


def enqueue_job(conn, site_id, mode="incremental", limit_n=None, requested_by=None) -> int:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(site_id,))
    active=conn.execute("""SELECT id FROM crawl_jobs WHERE site_id=%s
      AND status IN('queued','running','cancelling') LIMIT 1""",(site_id,)).fetchone()
    if active:
        conn.rollback()
        raise ActiveJobError(f"active crawl job {active['id']}")
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


def claim_next_job(conn, *, worker_id: str = "crawl-worker-1", lease_seconds: int = 600) -> Optional[dict]:
    """Atomically claim one queued job (status -> running). None if queue empty."""
    row = conn.execute(
        """
        UPDATE crawl_jobs SET status='running', started_at=COALESCE(started_at,now()),
               worker_id=%s, lease_expires_at=now()+make_interval(secs=>%s), attempts=attempts+1
         WHERE id = (
            SELECT id FROM crawl_jobs
             WHERE status='queued' AND next_attempt_at<=now()
             ORDER BY created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1
         )
        RETURNING id, site_id, mode, limit_n, attempts, max_attempts
        """, (worker_id, lease_seconds)
    ).fetchone()
    if row:_log(conn,row["id"],"started","Worker가 수집을 시작했습니다.")
    conn.commit()
    if row is None:
        return None
    return dict(row)


def renew_lease(conn, job_id: int, *, worker_id: str, lease_seconds: int = 600) -> bool:
    row = conn.execute(
        """UPDATE crawl_jobs SET lease_expires_at=now()+make_interval(secs=>%s)
             WHERE id=%s AND worker_id=%s AND status IN ('running','cancelling') RETURNING id""",
        (lease_seconds, job_id, worker_id),
    ).fetchone()
    conn.commit()
    return row is not None


def recover_stale(conn) -> int:
    """Recover expired crawl leases without leaving permanent running rows."""
    rows = conn.execute(
        """SELECT id,status,attempts,max_attempts FROM crawl_jobs
             WHERE status IN ('running','cancelling')
               AND (lease_expires_at IS NULL OR lease_expires_at<now())
             FOR UPDATE SKIP LOCKED"""
    ).fetchall()
    for row in rows:
        if row["status"] == "cancelling":
            status, event, message = "cancelled", "cancelled", "Worker 연결 종료 후 취소가 확정되었습니다."
        elif row["attempts"] < row["max_attempts"]:
            status, event, message = "queued", "lease_recovered", "Worker lease 만료로 작업이 다시 대기열에 등록되었습니다."
        else:
            status, event, message = "failed", "lease_expired", "Worker lease가 반복 만료되어 작업이 실패 처리되었습니다."
        conn.execute(
            """UPDATE crawl_jobs SET status=%s,worker_id=NULL,lease_expires_at=NULL,
                 next_attempt_at=now(),finished_at=CASE WHEN %s IN ('failed','cancelled') THEN now() ELSE NULL END,
                 error=CASE WHEN %s='failed' THEN 'worker lease expired' ELSE error END WHERE id=%s""",
            (status, status, status, row["id"]),
        )
        _log(conn, row["id"], event, message, "warning" if status != "failed" else "error")
    conn.commit()
    return len(rows)


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
    """Cancel queued work or request cooperative cancellation of running work."""
    row = conn.execute(
        """UPDATE crawl_jobs
              SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                  cancel_requested_at=now(),
                  finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END,
                  error=NULL
            WHERE id=%s AND status IN ('queued','running')
        RETURNING *""",
        (job_id,),
    ).fetchone()
    if row:
        event = "cancelled" if row["status"] == "cancelled" else "cancel_requested"
        message = "대기 중인 작업이 취소되었습니다." if row["status"] == "cancelled" else "실행 중인 작업에 취소가 요청되었습니다. 현재 수집 단위가 끝나면 중단됩니다."
        _log(conn,job_id,event,message,"warning")
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
    row = conn.execute(
        """UPDATE crawl_jobs SET status='done', saved_count=%s, finished_at=now(),
               worker_id=NULL,lease_expires_at=NULL
            WHERE id=%s AND status='running' RETURNING status""",
        (int(saved_count or 0), job_id),
    ).fetchone()
    if row:
        _log(conn,job_id,"completed",f"수집 완료: 신규 문서 {int(saved_count or 0)}건")
    else:
        cancelled = conn.execute(
            """UPDATE crawl_jobs SET status='cancelled', saved_count=%s, finished_at=now(),
                  worker_id=NULL,lease_expires_at=NULL
               WHERE id=%s AND status='cancelling' RETURNING status""",
            (int(saved_count or 0), job_id),
        ).fetchone()
        if cancelled:
            _log(conn,job_id,"cancelled",f"취소 요청에 따라 종료되었습니다. 종료 전 신규 문서 {int(saved_count or 0)}건", "warning")
    conn.commit()


def fail_job(conn, job_id, error) -> None:
    row = conn.execute(
        """UPDATE crawl_jobs SET status='failed', error=%s, finished_at=now(),
               worker_id=NULL,lease_expires_at=NULL
            WHERE id=%s AND status IN ('running','cancelling') RETURNING id""",
        ((error or "")[:2000], job_id),
    ).fetchone()
    if row:
        _log(conn,job_id,"failed","수집 작업이 실패했습니다.","error")
    conn.commit()


def job_detail(conn,job_id:int) -> dict | None:
    row=conn.execute("SELECT * FROM crawl_jobs WHERE id=%s",(job_id,)).fetchone()
    if not row:return None
    logs=conn.execute("SELECT id,level,event,message,created_at FROM crawl_job_logs WHERE job_id=%s ORDER BY id",(job_id,)).fetchall()
    return {"job":dict(row),"logs":[dict(item) for item in logs]}
