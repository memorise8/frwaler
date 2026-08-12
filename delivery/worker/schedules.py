"""Idempotent crawl schedule management and due enqueue."""
from __future__ import annotations


def upsert(conn,*,site_id:str,interval_hours:int,mode:str="incremental",limit_n:int|None=None,
           enabled:bool=True,created_by:str|None=None) -> dict:
    if not 1<=interval_hours<=8760 or mode not in ("incremental","full") or (limit_n is not None and not 1<=limit_n<=1000):
        raise ValueError("invalid schedule configuration")
    row=conn.execute("""INSERT INTO crawl_schedules(site_id,interval_hours,mode,limit_n,enabled,next_run_at,created_by)
      VALUES(%s,%s,%s,%s,%s,now()+make_interval(hours=>%s),%s)
      ON CONFLICT(site_id) DO UPDATE SET interval_hours=EXCLUDED.interval_hours,mode=EXCLUDED.mode,
      limit_n=EXCLUDED.limit_n,enabled=EXCLUDED.enabled,next_run_at=CASE WHEN NOT crawl_schedules.enabled AND EXCLUDED.enabled THEN now() ELSE crawl_schedules.next_run_at END,
      updated_at=now() RETURNING *""",(site_id,interval_hours,mode,limit_n,enabled,interval_hours,created_by)).fetchone();conn.commit();return dict(row)


def list_schedules(conn)->list[dict]:return [dict(r) for r in conn.execute("SELECT * FROM crawl_schedules ORDER BY next_run_at,site_id").fetchall()]


def enqueue_due(conn)->int:
    rows=conn.execute("""SELECT * FROM crawl_schedules WHERE enabled AND next_run_at<=now()
      ORDER BY next_run_at FOR UPDATE SKIP LOCKED""").fetchall();created=0
    for row in rows:
        active=conn.execute("SELECT 1 FROM crawl_jobs WHERE site_id=%s AND status IN('queued','running') LIMIT 1",(row["site_id"],)).fetchone()
        if not active:
            job=conn.execute("""INSERT INTO crawl_jobs(site_id,mode,limit_n,requested_by)
              VALUES(%s,%s,%s,'scheduler') RETURNING id""",(row["site_id"],row["mode"],row["limit_n"])).fetchone()
            conn.execute("INSERT INTO crawl_job_logs(job_id,level,event,message) VALUES(%s,'info','scheduled','예약된 증분 수집이 등록되었습니다.')",(job["id"],));created+=1
        conn.execute("""UPDATE crawl_schedules SET last_run_at=now(),next_run_at=now()+make_interval(hours=>interval_hours),updated_at=now() WHERE id=%s""",(row["id"],))
    conn.commit();return created
