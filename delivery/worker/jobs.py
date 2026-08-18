# -*- coding: utf-8 -*-
"""crawl_jobs 큐 조작 — 원자적 소비(SKIP LOCKED)."""
from __future__ import annotations

import json
import os

from typing import Optional


# How long a job keeps the crawler's own captured output. The three lifecycle
# rows (queued/started/completed) are the durable record of what happened and
# are never pruned -- they are 3 rows per job. crawler_output is ~40 rows per
# job, so a single 804-site sweep writes about 32,000 of them; it is diagnostic
# detail with a short useful life, read within days of a run going wrong.
CRAWLER_OUTPUT_RETENTION_DAYS = int(os.environ.get("LIBERTREE_JOB_LOG_RETENTION_DAYS", "14"))


class ActiveJobError(RuntimeError):
    pass


def _log(conn,job_id:int,event:str,message:str,level:str="info") -> None:
    conn.execute(
        "INSERT INTO crawl_job_logs(job_id,level,event,message,created_at)"
        " VALUES(%s,%s,%s,%s,clock_timestamp())",
        (job_id,level,event,(message or "")[:500]))


def log_event(conn, job_id: int, event: str, message: str, level: str = "info") -> None:
    """Public lifecycle-log wrapper for callers outside this module (e.g. worker.py).

    Does not commit -- callers own the transaction boundary, same as ``_log``.
    """
    _log(conn, job_id, event, message, level)


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
        UPDATE crawl_jobs SET status='running', started_at=COALESCE(started_at,clock_timestamp()),
               worker_id=%s, lease_expires_at=clock_timestamp()+make_interval(secs=>%s), attempts=attempts+1
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
        """UPDATE crawl_jobs SET lease_expires_at=clock_timestamp()+make_interval(secs=>%s)
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
                 next_attempt_at=clock_timestamp(),finished_at=CASE WHEN %s IN ('failed','cancelled') THEN clock_timestamp() ELSE NULL END,
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
                  cancel_requested_at=clock_timestamp(),
                  finished_at=CASE WHEN status='queued' THEN clock_timestamp() ELSE finished_at END,
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


def finish_job(conn, job_id, saved_count, *, truncated: bool = False) -> str | None:
    """Terminal transition for a job this call still owns.

    Returns the terminal status the UPDATE actually produced:
    ``"done"`` when the running-row UPDATE matched, ``"cancelled"`` when the
    cancelling-row UPDATE matched instead, or ``None`` when neither matched
    (the row moved out from under the caller -- e.g. recover_stale already
    reclaimed the lease elsewhere). Callers must not persist cursor,
    completion, or requeue state for a job they no longer own, and a
    ``"cancelled"`` result on the normal-return path must be treated exactly
    like the CrawlCancelled handler: persist the advance, skip completion
    and requeue -- the operator's cancel breaks the chain even when it lands
    after the crawler already returned normally.
    """
    row = conn.execute(
        """UPDATE crawl_jobs SET status='done', saved_count=%s, finished_at=clock_timestamp(),
               worker_id=NULL,lease_expires_at=NULL,truncated=%s
            WHERE id=%s AND status='running' RETURNING status""",
        (int(saved_count or 0), bool(truncated), job_id),
    ).fetchone()
    cancelled = None
    if row:
        message = f"수집 완료: 신규 문서 {int(saved_count or 0)}건"
        if truncated:
            message += " (시간 제한으로 남은 페이지를 건너뛰었습니다)"
        _log(conn,job_id,"completed",message)
    else:
        cancelled = conn.execute(
            """UPDATE crawl_jobs SET status='cancelled', saved_count=%s, finished_at=clock_timestamp(),
                  worker_id=NULL,lease_expires_at=NULL
               WHERE id=%s AND status='cancelling' RETURNING status""",
            (int(saved_count or 0), job_id),
        ).fetchone()
        if cancelled:
            _log(conn,job_id,"cancelled",f"취소 요청에 따라 종료되었습니다. 종료 전 신규 문서 {int(saved_count or 0)}건", "warning")
    conn.commit()
    if row is not None:
        return "done"
    if cancelled is not None:
        return "cancelled"
    return None


def fail_job(conn, job_id, error) -> None:
    row = conn.execute(
        """UPDATE crawl_jobs SET status='failed', error=%s, finished_at=clock_timestamp(),
               worker_id=NULL,lease_expires_at=NULL
            WHERE id=%s AND status IN ('running','cancelling') RETURNING id""",
        ((error or "")[:2000], job_id),
    ).fetchone()
    if row:
        _log(conn,job_id,"failed","수집 작업이 실패했습니다.","error")
    conn.commit()


def prune_crawler_output(conn, *, retention_days: int = None) -> int:
    """Drop captured crawler output past its retention window.

    Only ``crawler_output`` rows are removed. The job row itself and its
    lifecycle log survive, so the history that /sites/verification aggregates --
    and the record that a job ran at all -- is never lost to retention.

    A retention of 0 means exactly that: every captured line already written is
    removed, including a run that finished seconds ago. The clamp exists for a
    different reason -- an unclamped negative value would put the cutoff in the
    future and delete output that has not aged at all, so a misconfigured
    ``-5`` behaves as ``0`` rather than as ``+5``.
    """
    days = CRAWLER_OUTPUT_RETENTION_DAYS if retention_days is None else retention_days
    row = conn.execute(
        """WITH removed AS (
             DELETE FROM crawl_job_logs
              WHERE event='crawler_output'
                AND created_at < clock_timestamp()-make_interval(days=>%s)
          RETURNING 1)
           SELECT count(*) AS count FROM removed""",
        (max(0, int(days)),),
    ).fetchone()
    conn.commit()
    return int(row["count"])


def log_crawler_output(conn, job_id: int, lines) -> int:
    """Persist the tail of the crawler's own stdout against one job.

    The crawlers already say what they are doing -- "saved 3/3: ...", "abstract
    too short, skipping", "fetch attempt 1/3 failed: all_layers_failed" -- but
    that narration only ever reached container logs. On screen a fully blocked
    crawl and a successful one both read "완료 · 신규 0건", because saved_count
    (net new rows) is the same 0 for both.

    stdout is the only thing all 804 crawlers share: 703 of them shell out to
    curl through their own private helper and just 14 use BaseCrawler._request,
    so there is no fetch layer to instrument instead. Rows are written in call
    order and read back by job_detail ordered by id, so the tail stays in
    sequence regardless of timestamp resolution.

    Never raises: a job that has already reached a terminal state must not be
    turned into a crash by its own bookkeeping.
    """
    if not lines:
        return 0
    try:
        for line in lines:
            conn.execute(
                "INSERT INTO crawl_job_logs(job_id,level,event,message,created_at)"
                " VALUES(%s,'info','crawler_output',%s,clock_timestamp())",
                (job_id, line[:500]))
        conn.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not mask the job outcome
        conn.rollback()
        return 0
    return len(lines)


JOB_STATUSES = ("queued", "running", "cancelling", "done", "failed", "cancelled")

# 추정을 시작하기 전에 요구하는 완료 표본 수. 1~2건으로 낸 평균은 추정이 아니라
# 추측이고, 화면은 그 차이를 표현할 수 없으므로 여기서 아예 None을 돌려준다.
_MIN_ESTIMATE_SAMPLES = 3
_ESTIMATE_WINDOW = 20


def summarize_queue(conn) -> dict:
    """Queue depth by status plus an honest per-site duration estimate.

    Deliberately returns no all-time total: job history is retained forever
    (verification aggregates are computed from it), so a percentage against it
    would be diluted by months of past runs. No percentage is reported at all --
    a 24-hour window is not the current sweep either, so the caller shows queue
    depth and a remaining-time estimate rather than a fraction.
    """
    counts = {status: 0 for status in JOB_STATUSES}
    for row in conn.execute("SELECT status, count(*) AS n FROM crawl_jobs GROUP BY status").fetchall():
        counts[row["status"]] = int(row["n"])

    finished_24h = int(conn.execute(
        """SELECT count(*) AS n FROM crawl_jobs
            WHERE finished_at IS NOT NULL AND finished_at > now() - interval '24 hours'"""
    ).fetchone()["n"])

    # Only 'done' runs. A job cancelled out of the queue never started, so its
    # duration is null or near zero; letting those in drags the estimate toward
    # "almost finished" precisely when a bulk sweep has just been stopped.
    estimate = conn.execute(
        """SELECT avg(EXTRACT(EPOCH FROM (finished_at - started_at)))::float8 AS avg_seconds,
                  count(*) AS samples
             FROM (SELECT started_at, finished_at FROM crawl_jobs
                    WHERE status='done' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                    ORDER BY finished_at DESC LIMIT %s) recent""",
        (_ESTIMATE_WINDOW,),
    ).fetchone()
    samples = int(estimate["samples"])
    avg_seconds = float(estimate["avg_seconds"]) if samples >= _MIN_ESTIMATE_SAMPLES else None

    return {
        "counts": counts,
        "active": counts["queued"] + counts["running"] + counts["cancelling"],
        "finished_24h": finished_24h,
        "avg_seconds": avg_seconds,
        "samples": samples,
    }


def job_detail(conn,job_id:int) -> dict | None:
    row=conn.execute("SELECT * FROM crawl_jobs WHERE id=%s",(job_id,)).fetchone()
    if not row:return None
    logs=conn.execute("SELECT id,level,event,message,created_at FROM crawl_job_logs WHERE job_id=%s ORDER BY id",(job_id,)).fetchall()
    return {"job":dict(row),"logs":[dict(item) for item in logs]}


# ---------------------------------------------------------------------------
# 사이트별 백필 진도 (crawl_site_progress)
# ---------------------------------------------------------------------------

def load_cursor(conn, site_id) -> Optional[dict]:
    row = conn.execute(
        "SELECT cursor FROM crawl_site_progress WHERE site_id=%s", (site_id,)
    ).fetchone()
    if row is None or row["cursor"] is None:
        return None
    return dict(row["cursor"])


def save_progress(conn, site_id, cursor: dict, items_delta: int = 0) -> None:
    """커서 업서트 + items_done 누적. 워커의 종결 전이와 같은 결로 커밋한다.

    completed_at 을 함께 NULL 로 되돌린다: 커서가 전진했다는 것은 그 사이트가
    아직 끝나지 않았다는 증거다. mark_backfill_complete 뒤에도 oldest_first
    증분이나 뒤늦은 백필 조각이 다시 커서를 전진시킬 수 있고, 그때 완주
    표시가 남아 있으면 화면이 거짓을 말한다.
    """
    conn.execute(
        """
        INSERT INTO crawl_site_progress (site_id, cursor, items_done, updated_at)
        VALUES (%s, %s::jsonb, %s, now())
        ON CONFLICT (site_id) DO UPDATE
           SET cursor = EXCLUDED.cursor,
               items_done = crawl_site_progress.items_done + EXCLUDED.items_done,
               updated_at = now(),
               completed_at = NULL
        """,
        (site_id, json.dumps(cursor), int(items_delta)),
    )
    conn.commit()


def mark_backfill_complete(conn, site_id) -> None:
    conn.execute(
        """
        INSERT INTO crawl_site_progress (site_id, completed_at, updated_at)
        VALUES (%s, now(), now())
        ON CONFLICT (site_id) DO UPDATE
           SET completed_at = now(), updated_at = now()
        """,
        (site_id,),
    )
    conn.commit()
