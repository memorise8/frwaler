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
from crawler.base_crawler import CrawlCancelled, CrawlUpToDate
from delivery.translation.observations import prune as prune_observations, record as record_observation

_last_observation_prune = 0.0
_last_job_log_prune = 0.0

# How many trailing lines of a crawler's own output are kept with the job.
# Enough to hold a fully blocked run's evidence (17 sections x 3 attempts
# produced ~68 lines) without letting a long successful crawl write thousands
# of rows per job.
CRAWL_OUTPUT_LINES = int(os.environ.get("LIBERTREE_JOB_LOG_LINES", "40"))

# 예산에 근접한 시간을 쓰고 예외 없이 돌아온 수집은 잘린 것으로 본다.
#
# 크롤러가 찍는 문구로 판별하는 방법을 먼저 검토했다가 버렸다: 문구가 8종 이상으로
# 제각각인 데다, 크롤러 663개가 문서 제목을 stdout 으로 흘린다. 공공기관 수집기라
# "Budget 2026: stopping inflation" 같은 평범한 제목이 규칙에 걸린다. 경과 시간에는
# 그런 오탐이 없고, 아무 문구도 찍지 않는 94개까지 함께 잡힌다.
#
# 여유 60초는 확인된 최대치의 두 배다 -- 크롤러 25개가 자기 예산에서 30초를 빼고
# 미리 빠져나간다. budget*0.1 항은 테스트가 예산을 몇 초로 낮춰도 임계가 음수로
# 무너지지 않게 한다.
_TRUNCATION_MARGIN_S = 60.0


def wall_budget_seconds() -> float:
    """크롤러가 읽는 것과 같은 예산. 호출 시점에 읽는다(테스트가 낮출 수 있도록)."""
    return float(os.environ.get("LIBERTREE_MAX_WALL_S", 25 * 60))


def truncation_threshold_seconds() -> float:
    budget = wall_budget_seconds()
    return budget - min(_TRUNCATION_MARGIN_S, budget * 0.1)


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


def _persist_advance(conn, site_id, inst, start_cursor) -> bool:
    """크롤러가 보고한 전진을 저장. 전진 없으면 False (저장도 없음)."""
    pending = getattr(inst, "delivery_pending_cursor", None)
    if pending is None or pending == start_cursor:
        return False
    jobs.save_progress(conn, site_id, pending,
                       items_delta=getattr(inst, "delivery_cursor_items_done", 0))
    return True


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

    # 재개(cursor) 계약: backfill 은 늘 이전 커서에서, oldest_first incremental
    # 도 마찬가지다 (신규가 오래된 쪽부터 온다). newest_first/arbitrary
    # incremental 과 full 은 커서를 아예 만지지 않는다 -- 주입하지도, 회수해
    # 저장하지도 않는다. 안 그러면 그런 실행이 우연히 _advance_cursor 를 부르는
    # 순간 백필 진도(예: {"page":300})가 조용히 덮어써진다.
    mode = job.get("mode", "incremental")
    order = getattr(cls, "DELIVERY_ORDER", "arbitrary")
    cursor_enabled = mode == "backfill" or (mode == "incremental" and order == "oldest_first")
    start_cursor = jobs.load_cursor(conn, site_id) if cursor_enabled else None

    # Computed before the crawl runs: a malformed LIBERTREE_MAX_WALL_S must fail
    # here, not after a successful crawl -- failing late orphans a completed
    # crawl as a stuck `running` row.
    threshold = truncation_threshold_seconds()
    started = time.monotonic()
    inst = None
    try:
        with redirect_stdout(capture):
            inst = cls(db_conn=conn, delay=delay)
            inst.delivery_mode = mode
            inst.delivery_should_cancel = should_cancel or (lambda: False)
            inst.delivery_cursor = start_cursor
            inst.crawl(limit=job.get("limit_n"))
    except CrawlUpToDate:
        # 신규분이 소진됐다는 정상 신호 -- 완료로 종결하고 잘림 판정은 하지 않는다.
        conn.rollback()
        saved = max(0, _count_site_docs(conn, site_id) - before)
        finished = jobs.finish_job(conn, job["id"], saved_count=saved)
        if finished and cursor_enabled:
            # backfill 이 완주 대신 up-to-date 를 만난 경우도 완료다: 신규가
            # 없다는 뜻이지, 못 걸었다는 뜻이 아니다.
            _persist_advance(conn, site_id, inst, start_cursor)
            if mode == "backfill":
                jobs.mark_backfill_complete(conn, site_id)
        record_output()
        return saved
    except CrawlCancelled:
        conn.rollback()
        saved = max(0, _count_site_docs(conn, site_id) - before)
        finished = jobs.finish_job(conn, job["id"], saved_count=saved)
        # 취소여도 걸은 만큼은 진짜다: 커서는 남기고, 체인(재큐잉)만 끊는다.
        if finished and cursor_enabled:
            _persist_advance(conn, site_id, inst, start_cursor)
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
    # 예산에 걸려 남은 페이지를 건너뛰고 정상 반환한 경우. 취소·실패 경로에서는
    # 판정하지 않는다 -- 그것들은 각자의 상태가 있고, 오래 돌다 취소된 것을
    # "잘렸다"고 부르면 두 사건이 뒤섞인다.
    truncated = (time.monotonic() - started) >= threshold
    finished = jobs.finish_job(conn, job["id"], saved_count=max(0, saved), truncated=truncated)
    if finished:
        # 이 작업을 아직 우리가 소유할 때만: lease 가 이미 회수되어 다른 곳에서
        # 이 행을 넘겨받았다면(finished=False) 커서/완료/재큐잉을 만지지 않는다.
        advanced = cursor_enabled and _persist_advance(conn, site_id, inst, start_cursor)
        if mode == "backfill":
            items_seen = getattr(inst, "delivery_cursor_items_done", 0) > 0
            if truncated and advanced and items_seen:
                # 전진이 재큐잉의 유일한 면허다. 새 INSERT 는 created_at 순서상 큐 맨 뒤.
                try:
                    jobs.enqueue_job(conn, site_id, mode="backfill",
                                     limit_n=job.get("limit_n"), requested_by="auto-backfill")
                except jobs.ActiveJobError:
                    # 경쟁 행이 이미 있다 -- 체인은 그 행이 잇는다. 죽을 일이 아니다.
                    jobs.log_event(conn, job["id"], "requeue_skipped",
                                   "활성 작업이 이미 있어 자동 재큐잉을 건너뜁니다.", level="warning")
                    conn.commit()
            elif truncated:
                jobs.log_event(conn, job["id"], "stalled",
                               "잘렸지만 커서가 전진하지 않아 자동 재큐잉을 멈춥니다.", level="warning")
                conn.commit()
            elif inst.delivery_pending_cursor is not None or saved > 0:
                jobs.mark_backfill_complete(conn, site_id)
            else:
                # 아무 진전도 없이 정상 반환한 백필 -- 완주로 표시하면 다시는
                # 재시도되지 않는다.
                jobs.log_event(conn, job["id"], "no_progress",
                               "아무것도 걷지 못한 백필 -- 완주로 표시하지 않습니다.", level="warning")
                conn.commit()
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
