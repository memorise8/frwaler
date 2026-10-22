"""Idempotent crawl schedule management and due enqueue.

Modes: ``incremental``/``full`` run a registered crawler as a crawl_jobs row (delivery worker).
``enum_incremental`` is the plan-driven enumeration collector's incremental mode
(crawler/enumeration/incremental.py): it is not a crawl_jobs row but a request to the host
collect runner (deploy/collect-runner, mode "incremental"), sent through ``runner_submit``.
Its documents reach the delivery DB through the existing run-folder import, unchanged.
"""
from __future__ import annotations

import json

MODES = ("incremental", "full", "enum_incremental")
RUNNER_MODES = {"enum_incremental": "incremental"}


def upsert(conn,*,site_id:str,interval_hours:int,mode:str="incremental",limit_n:int|None=None,
           enabled:bool=True,created_by:str|None=None) -> dict:
    if not 1<=interval_hours<=8760 or mode not in MODES or (limit_n is not None and not 1<=limit_n<=1000):
        raise ValueError("invalid schedule configuration")
    if mode in RUNNER_MODES and limit_n is not None:
        # Bounded by the plan's incremental.max_pages / known_run, never by a document count.
        raise ValueError("enum_incremental schedules take no limit_n")
    row=conn.execute("""INSERT INTO crawl_schedules(site_id,interval_hours,mode,limit_n,enabled,next_run_at,created_by)
      VALUES(%s,%s,%s,%s,%s,now()+make_interval(hours=>%s),%s)
      ON CONFLICT(site_id) DO UPDATE SET interval_hours=EXCLUDED.interval_hours,mode=EXCLUDED.mode,
      limit_n=EXCLUDED.limit_n,enabled=EXCLUDED.enabled,next_run_at=CASE WHEN NOT crawl_schedules.enabled AND EXCLUDED.enabled THEN now() ELSE crawl_schedules.next_run_at END,
      updated_at=now() RETURNING *""",(site_id,interval_hours,mode,limit_n,enabled,interval_hours,created_by)).fetchone();conn.commit();return dict(row)


def list_schedules(conn)->list[dict]:return [dict(r) for r in conn.execute("SELECT * FROM crawl_schedules ORDER BY next_run_at,site_id").fetchall()]


def delete(conn, site_id: str) -> bool:
    """Remove a schedule. Returns False when there was nothing to remove.

    Nothing references crawl_schedules, and jobs it created carry only the
    string requested_by='scheduler', so deleting the row cannot disturb work
    already queued or running -- the schedule stops repeating, the current
    collection finishes.
    """
    row = conn.execute(
        "DELETE FROM crawl_schedules WHERE site_id=%s RETURNING id", (site_id,)
    ).fetchone()
    conn.commit()
    return row is not None


ADVANCE = ("UPDATE crawl_schedules SET last_run_at=now(),next_run_at=now()+make_interval(hours=>interval_hours),"
           "updated_at=now() WHERE id=%s")


def _log(**fields):
    print(json.dumps({"event": "schedule_runner_request", **fields}, ensure_ascii=False, separators=(",", ":")),
          flush=True)


def enqueue_due(conn, runner_submit=None)->int:
    """Create due work. ``runner_submit(site_id, runner_mode) -> (http_status, payload)`` sends
    enum_incremental schedules to the collect runner; without it they stay due (not lost,
    not advanced) until a worker with runner access picks them up.

    Runner requests are blocking HTTP, so they are never sent inside the row-locking
    transaction: due runner rows are advanced (claimed) and committed first, then sent.
    A transport error (timeout, connection loss) is logged as outcome "unknown": the runner
    may have accepted the run, so it is never resubmitted for this interval (a duplicate
    would be refused by the runner as site_busy anyway). Rows not yet sent when the runner
    turns out unreachable are put back as due, so one cycle costs at most one timeout."""
    rows=conn.execute("""SELECT * FROM crawl_schedules WHERE enabled AND next_run_at<=now()
      ORDER BY next_run_at FOR UPDATE SKIP LOCKED""").fetchall();created=0
    to_send=[]
    for row in rows:
        if row["mode"] in RUNNER_MODES:
            if runner_submit is not None:
                conn.execute(ADVANCE,(row["id"],))
                to_send.append(row)
            continue
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(row["site_id"],))
        active=conn.execute("SELECT 1 FROM crawl_jobs WHERE site_id=%s AND status IN('queued','running','cancelling') LIMIT 1",(row["site_id"],)).fetchone()
        if not active:
            job=conn.execute("""INSERT INTO crawl_jobs(site_id,mode,limit_n,requested_by)
              VALUES(%s,%s,%s,'scheduler') RETURNING id""",(row["site_id"],row["mode"],row["limit_n"])).fetchone()
            conn.execute("INSERT INTO crawl_job_logs(job_id,level,event,message) VALUES(%s,'info','scheduled','예약된 증분 수집이 등록되었습니다.')",(job["id"],));created+=1
        conn.execute(ADVANCE,(row["id"],))
    conn.commit()
    for n, row in enumerate(to_send):
        try:
            code, payload = runner_submit(row["site_id"], RUNNER_MODES[row["mode"]])
        except Exception as exc:  # no answer: the request may or may not have reached the runner
            _log(site_id=row["site_id"], outcome="unknown", error=type(exc).__name__)
            rest=[r["id"] for r in to_send[n + 1:]]
            for rid in rest:
                conn.execute("UPDATE crawl_schedules SET next_run_at=now(),updated_at=now() WHERE id=%s",(rid,))
            if rest:
                conn.commit()
                _log(outcome="deferred_runner_unreachable", schedule_ids=rest)
            break
        payload = payload if isinstance(payload, dict) else {}
        # The runner owns admission (one site/unit at a time, plan ready): a refusal such as
        # site_busy is recorded and the schedule waits for its next interval, like an active job.
        _log(site_id=row["site_id"], outcome="accepted" if code == 201 else "refused", status=code,
             reason=payload.get("reason"), run_id=payload.get("run_id"))
        created += code == 201
    return created
