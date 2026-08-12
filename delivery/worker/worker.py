# -*- coding: utf-8 -*-
"""워커: crawl_jobs 를 소비해 크롤러를 실행한다.

기본 백엔드는 postgres 로 강제(LIBERTREE_DB_BACKEND). crawlers-share 와 동일한
호출 패턴: CRAWLERS[site_id](db_conn=conn, delay=...).crawl(limit).
"""
from __future__ import annotations

import os
import time
import json
import shutil
import threading

from . import jobs
from . import schedules
from delivery.translation import jobs as translation_jobs
from delivery.translation.providers import provider_from_env


def _registry(crawler_registry):
    if crawler_registry is not None:
        return crawler_registry
    from crawler.sites import CRAWLERS
    return CRAWLERS


def _count_site_docs(conn, site_id) -> int:
    row = conn.execute("SELECT count(*) AS n FROM documents WHERE site_id=%s", (site_id,)).fetchone()
    return int(row["n"])


def run_job(conn, job, crawler_registry=None, delay=1.0) -> int:
    """Run one claimed job. Returns saved doc count (0 on failure)."""
    os.environ.setdefault("LIBERTREE_DB_BACKEND", "postgres")
    site_id = job["site_id"]
    registry = _registry(crawler_registry)
    cls = registry.get(site_id)
    if cls is None:
        jobs.fail_job(conn, job["id"], f"no crawler for site_id={site_id!r}")
        return 0
    before = _count_site_docs(conn, site_id)
    try:
        inst = cls(db_conn=conn, delay=delay)
        inst.crawl(limit=job.get("limit_n"))
    except Exception as exc:  # noqa: BLE001
        # SQL errors leave psycopg transactions aborted; clear that state before
        # recording the durable failure transition.
        conn.rollback()
        jobs.fail_job(conn, job["id"], f"{type(exc).__name__}: {exc}")
        return 0
    saved = _count_site_docs(conn, site_id) - before
    jobs.finish_job(conn, job["id"], saved_count=max(0, saved))
    return max(0, saved)


def _lease_loop(dsn: str, job_id: int, worker_id: str, stop: threading.Event,
                *, interval: float = 60.0, lease_seconds: int = 600) -> None:
    from crawler import db_pg
    while not stop.wait(interval):
        try:
            lease_conn = db_pg.open_db(dsn)
            try:
                if not jobs.renew_lease(lease_conn, job_id, worker_id=worker_id, lease_seconds=lease_seconds):
                    return
            finally:
                lease_conn.close()
        except Exception:  # lease expiry recovery is the fallback when DB is unavailable
            return


def run_once(conn, crawler_registry=None, delay=1.0, *, worker_id: str = "crawl-worker-1",
             lease_dsn: str | None = None) -> bool:
    """Claim+run one job. False if the queue was empty."""
    job = jobs.claim_next_job(conn, worker_id=worker_id)
    if job is None:
        return False
    stop = threading.Event()
    heartbeat = None
    if lease_dsn:
        heartbeat = threading.Thread(target=_lease_loop, args=(lease_dsn, job["id"], worker_id, stop), daemon=True)
        heartbeat.start()
    try:
        run_job(conn, job, crawler_registry=crawler_registry, delay=delay)
    finally:
        stop.set()
        if heartbeat:
            heartbeat.join(timeout=2)
    return True


def main() -> int:
    import argparse
    from crawler import db_pg

    os.environ.setdefault("LIBERTREE_DB_BACKEND", "postgres")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("LIBERTREE_PG_DSN"))
    ap.add_argument("--once", action="store_true", help="한 작업만 처리하고 종료")
    ap.add_argument("--poll", type=float, default=5.0, help="빈 큐 폴링 간격(초)")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("LIBERTREE_PG_DSN or --dsn required")

    worker_id=os.environ.get("TRANSLATION_WORKER_ID","delivery-worker-1")
    conn = db_pg.open_db(args.dsn)
    try:
        translation_jobs.recover_stale(conn)
        jobs.recover_stale(conn)
        if args.once:
            _observe_host(conn,worker_id)
            if not translation_jobs.run_once(conn, provider_from_env,worker_id=worker_id):
                run_once(conn, delay=args.delay,worker_id=worker_id,lease_dsn=args.dsn)
            return 0
        while True:
            schedules.enqueue_due(conn)
            _observe_host(conn,worker_id)
            translation_jobs.recover_stale(conn)
            jobs.recover_stale(conn)
            translated = translation_jobs.run_once(conn, provider_from_env,worker_id=worker_id)
            if not translated and not run_once(conn, delay=args.delay,worker_id=worker_id,lease_dsn=args.dsn):
                time.sleep(args.poll)
    finally:
        conn.close()


def _observe_host(conn,worker_id: str) -> None:
    free=shutil.disk_usage(os.environ.get("LIBERTREE_BLOB_ROOT","/tmp")).free
    conn.execute("""INSERT INTO translation_system_observations(worker_id,metric,value_numeric)
      VALUES(%s,'disk_free_bytes',%s)""",(worker_id,free))
    conn.execute("""INSERT INTO translation_worker_heartbeats(worker_id,status,last_seen_at,current_job_id)
      VALUES(%s,'idle',now(),NULL) ON CONFLICT(worker_id) DO UPDATE SET status='idle',last_seen_at=now(),current_job_id=NULL""",(worker_id,))
    conn.commit()
    print(json.dumps({"event":"worker_heartbeat","worker_id":worker_id,"disk_free_bytes":free},separators=(",",":")),flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
