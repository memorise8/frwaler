# -*- coding: utf-8 -*-
"""워커: crawl_jobs 를 소비해 크롤러를 실행한다.

기본 백엔드는 postgres 로 강제(LIBERTREE_DB_BACKEND). crawlers-share 와 동일한
호출 패턴: CRAWLERS[site_id](db_conn=conn, delay=...).crawl(limit).
"""
from __future__ import annotations

import os
import sys
import time
import json
import shutil
import threading
from collections import deque
from contextlib import redirect_stdout

from . import jobs
from . import schedules
from delivery.translation import jobs as translation_jobs
from delivery.translation.providers import provider_from_env
from crawler.base_crawler import CrawlCancelled
from delivery.translation.observations import prune as prune_observations, record as record_observation

_last_observation_prune = 0.0
_last_job_log_prune = 0.0

# How many trailing lines of a crawler's own output are kept with the job.
# Enough to hold a fully blocked run's evidence (17 sections x 3 attempts
# produced ~68 lines) without letting a long successful crawl write thousands
# of rows per job.
CRAWL_OUTPUT_LINES = int(os.environ.get("LIBERTREE_JOB_LOG_LINES", "40"))


class _TeeCapture:
    """Mirror a crawler's stdout to the container log while keeping the tail.

    Crawlers print with plain print(), so redirecting stdout for the duration
    of crawl() collects all 804 of them without touching a single crawler --
    there is no shared fetch layer to hook instead. The real stream is still
    written to, so `docker logs` keeps working exactly as before.
    """

    def __init__(self, stream, limit):
        self._stream = stream
        self._lines = deque(maxlen=max(1, limit))
        self._partial = ""

    def write(self, text):
        self._stream.write(text)
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            if line.strip():
                self._lines.append(line[:500])
        return len(text)

    def flush(self):
        self._stream.flush()

    def isatty(self):
        return False

    def __getattr__(self, name):  # encoding, errors, buffer, ... stay usable
        return getattr(self._stream, name)

    def lines(self):
        tail = list(self._lines)
        if self._partial.strip():
            tail.append(self._partial[:500])
        return tail


def _registry(crawler_registry):
    if crawler_registry is not None:
        return crawler_registry
    from crawler.sites import CRAWLERS
    return CRAWLERS


def _count_site_docs(conn, site_id) -> int:
    row = conn.execute("SELECT count(*) AS n FROM documents WHERE site_id=%s", (site_id,)).fetchone()
    return int(row["n"])


def run_job(conn, job, crawler_registry=None, delay=1.0, should_cancel=None) -> int:
    """Run one claimed job. Returns saved doc count (0 on failure)."""
    os.environ.setdefault("LIBERTREE_DB_BACKEND", "postgres")
    site_id = job["site_id"]
    registry = _registry(crawler_registry)
    cls = registry.get(site_id)
    if cls is None:
        jobs.fail_job(conn, job["id"], f"no crawler for site_id={site_id!r}")
        return 0
    before = _count_site_docs(conn, site_id)
    capture = _TeeCapture(sys.stdout, CRAWL_OUTPUT_LINES)
    # Recorded after each terminal transition, never before: fail_job rolls the
    # connection back first, and both it and finish_job commit, so writing the
    # output earlier would either be discarded or ride on the crawl's own
    # transaction.
    def record_output():
        jobs.log_crawler_output(conn, job["id"], capture.lines())

    try:
        with redirect_stdout(capture):
            inst = cls(db_conn=conn, delay=delay)
            inst.delivery_mode = job.get("mode", "incremental")
            inst.delivery_should_cancel = should_cancel or (lambda: False)
            inst.crawl(limit=job.get("limit_n"))
    except CrawlCancelled:
        conn.rollback()
        saved = max(0, _count_site_docs(conn, site_id) - before)
        jobs.finish_job(conn, job["id"], saved_count=saved)
        record_output()
        return saved
    except Exception as exc:  # noqa: BLE001
        # SQL errors leave psycopg transactions aborted; clear that state before
        # recording the durable failure transition.
        conn.rollback()
        jobs.fail_job(conn, job["id"], f"{type(exc).__name__}: {exc}")
        record_output()
        return 0
    saved = _count_site_docs(conn, site_id) - before
    jobs.finish_job(conn, job["id"], saved_count=max(0, saved))
    record_output()
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
    control_conn = None
    if lease_dsn:
        from crawler import db_pg
        control_conn = db_pg.open_db(lease_dsn)
    def should_cancel():
        target = control_conn or conn
        row = target.execute("SELECT status FROM crawl_jobs WHERE id=%s", (job["id"],)).fetchone()
        if control_conn:
            control_conn.rollback()
        return row is not None and row["status"] == "cancelling"
    try:
        run_job(conn, job, crawler_registry=crawler_registry, delay=delay,should_cancel=should_cancel)
    finally:
        stop.set()
        if heartbeat:
            heartbeat.join(timeout=2)
        if control_conn:
            control_conn.close()
    return True


def run_cycle(conn, *, worker_id: str, delay: float = 1.0, lease_dsn: str | None = None,
              crawler_registry=None, translation_provider=None) -> tuple[bool, bool]:
    """One scheduling pass: at most one translation job and one crawl job.

    Both lanes are attempted every cycle. The previous loop ran the crawl lane
    only when the translation lane had come back empty, which is not a priority
    ordering but a starvation bug on a single serial worker: a translation queue
    that never empties means crawling never runs at all, and an 804-site sweep
    can sit untouched for hours while the console shows only "대기 중".

    Alternating is not throughput-optimal -- while both queues are busy each
    lane advances one job per cycle, so translations proceed at crawl pace, and
    a crawl can hold the worker for minutes. It is chosen because neither lane
    can be starved indefinitely by the other, which no priority order gives.
    When only one queue has work that lane runs back to back, exactly as before.

    Returns (translated, crawled) so the caller can tell an idle cycle from a
    busy one without inspecting the queues again.
    """
    provider = provider_from_env if translation_provider is None else translation_provider
    translated = translation_jobs.run_once(conn, provider, worker_id=worker_id)
    crawled = run_once(conn, crawler_registry=crawler_registry, delay=delay,
                       worker_id=worker_id, lease_dsn=lease_dsn)
    return translated, crawled


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
            run_cycle(conn, worker_id=worker_id, delay=args.delay, lease_dsn=args.dsn)
            return 0
        while True:
            schedules.enqueue_due(conn)
            _observe_host(conn,worker_id)
            translation_jobs.recover_stale(conn)
            jobs.recover_stale(conn)
            translated, crawled = run_cycle(conn, worker_id=worker_id, delay=args.delay,
                                            lease_dsn=args.dsn)
            if not translated and not crawled:
                time.sleep(args.poll)
    finally:
        conn.close()


def _observe_host(conn,worker_id: str) -> None:
    global _last_observation_prune
    free=shutil.disk_usage(os.environ.get("LIBERTREE_BLOB_ROOT","/tmp")).free
    record_observation(conn,worker_id=worker_id,metric="disk_free_bytes",value=free)
    now=time.monotonic()
    if now-_last_observation_prune>=3600:
        prune_observations(conn)
        _last_observation_prune=now
    global _last_job_log_prune
    if now-_last_job_log_prune>=3600:
        removed=jobs.prune_crawler_output(conn)
        _last_job_log_prune=now
        if removed:
            print(json.dumps({"event":"crawler_output_pruned","rows":removed},separators=(",",":")),flush=True)
    conn.execute("""INSERT INTO translation_worker_heartbeats(worker_id,status,last_seen_at,current_job_id)
      VALUES(%s,'idle',now(),NULL) ON CONFLICT(worker_id) DO UPDATE SET status='idle',last_seen_at=now(),current_job_id=NULL""",(worker_id,))
    conn.commit()
    print(json.dumps({"event":"worker_heartbeat","worker_id":worker_id,"disk_free_bytes":free},separators=(",",":")),flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
