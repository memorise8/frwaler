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
from contextlib import redirect_stdout, redirect_stderr

from . import jobs
from . import schedules
from delivery.translation import jobs as translation_jobs
from delivery.translation.providers import provider_from_env
from crawler.base_crawler import CrawlCancelled, CrawlUpToDate
from crawler.enumeration.legacy import apply_overrides_from_env as apply_legacy_overrides
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
        from crawler.search_credentials import redact
        length = len(text)
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            line = redact(line)
            self._stream.write(line + '\n')
            if line.strip():
                self._lines.append(line[:500])
        return length

    def flush(self):
        self._stream.flush()

    def isatty(self):
        return False

    def __getattr__(self, name):  # encoding, errors, buffer, ... stay usable
        return getattr(self._stream, name)

    def lines(self):
        from crawler.search_credentials import redact
        tail = list(self._lines)
        if self._partial.strip():
            tail.append(redact(self._partial)[:500])
        return tail


def _registry(crawler_registry):
    if crawler_registry is not None:
        return crawler_registry
    from crawler.sites import CRAWLERS
    return CRAWLERS


def _count_site_docs(conn, site_id) -> int:
    row = conn.execute("SELECT count(*) AS n FROM documents WHERE site_id=%s", (site_id,)).fetchone()
    return int(row["n"])


def _persist_advance(conn, site_id, inst, start_cursor, mode, scope_id=None) -> bool:
    """크롤러가 보고한 전진을 저장. 전진 없으면 False (저장도 없음).

    completed_at 은 backfill 전진일 때만 지운다: oldest_first incremental 은
    이미 완주한 사이트에서도 정상적으로 계속 커서를 전진시키는 루틴 동작이라,
    그때마다 완주 표시를 지우면 화면이 거꾸로 거짓을 말하게 된다. 반대로
    backfill 이 전진했다는 것은(완주한 사이트에 재개된 백필이든 아니든) 그
    사이트가 지금 이 순간 끝나지 않았다는 증거이므로 지운다.
    """
    pending = getattr(inst, "delivery_pending_cursor", None)
    if pending is None or pending == start_cursor:
        return False
    if scope_id is not None:
        from delivery.worker import scopes
        scopes.save_progress(conn, scope_id, pending, getattr(inst, "delivery_cursor_items_done", 0))
        return True
    jobs.save_progress(conn, site_id, pending,
                       items_delta=getattr(inst, "delivery_cursor_items_done", 0),
                       clear_completed=(mode == "backfill"))
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
    scope_id = job.get('scope_id')
    scope = None
    if scope_id is not None:
        from delivery.worker import scopes
        try:
            if job.get('mode') != 'backfill':
                raise ValueError('scoped collection requires backfill mode')
            scope = scopes.load(conn, scope_id, site_id, cls)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            conn.rollback()
            jobs.fail_job(conn, job['id'], f'configuration: {exc}')
            return 0
    if crawler_registry is None and scope is None:
        from delivery.capabilities import validate_request, CapabilityError
        try:
            validate_request(site_id, job.get('mode', 'incremental'), trial=True)
        except CapabilityError as exc:
            jobs.fail_job(conn, job['id'], str(exc))
            return 0
    from crawler.search_credentials import missing_credentials
    missing = missing_credentials(site_id)
    if missing:
        jobs.fail_job(conn, job['id'], 'configuration: ' + ', '.join(missing))
        return 0
    before = _count_site_docs(conn, site_id)
    process_files = job.get("process_files", False)
    from delivery.files import jobs as file_jobs
    before_seq = conn.execute("SELECT COALESCE(max(seq_id),0) AS n FROM documents WHERE site_id=%s", (site_id,)).fetchone()["n"] if process_files else 0
    capture = _TeeCapture(sys.stdout, CRAWL_OUTPUT_LINES)
    # Recorded after each terminal transition, never before: fail_job rolls the
    # connection back first, and both it and finish_job commit, so writing the
    # output earlier would either be discarded or ride on the crawl's own
    # transaction.
    def record_output():
        if process_files:
            file_jobs.enqueue_new(conn, job["id"], site_id, before_seq)
        jobs.log_crawler_output(conn, job["id"], capture.lines())

    # 재개(cursor) 계약: backfill 은 늘 이전 커서에서, oldest_first incremental
    # 도 마찬가지다 (신규가 오래된 쪽부터 온다). newest_first/arbitrary
    # incremental 과 full 은 커서를 아예 만지지 않는다 -- 주입하지도, 회수해
    # 저장하지도 않는다. 안 그러면 그런 실행이 우연히 _advance_cursor 를 부르는
    # 순간 백필 진도(예: {"page":300})가 조용히 덮어써진다.
    mode = job.get("mode", "incremental")
    order = getattr(cls, "DELIVERY_ORDER", "arbitrary")
    cursor_enabled = mode == "backfill" or (mode == "incremental" and order == "oldest_first")
    start_cursor = scope['cursor'] if scope is not None else (jobs.load_cursor(conn, site_id) if cursor_enabled else None)
    version = getattr(cls, 'DELIVERY_CURSOR_VERSION', None)
    if scope is None and version and start_cursor and start_cursor.get('version') != version:
        # Legacy offsets may already have skipped a partial page. Re-scan once
        # with deduplication rather than continuing from an untrustworthy offset.
        start_cursor = None
        jobs.log_event(conn, job['id'], 'cursor_upgrade', '재개 위치 형식 변경으로 처음부터 중복을 확인하며 재탐색합니다.', level='warning')

    # Computed before the crawl runs: a malformed LIBERTREE_MAX_WALL_S must fail
    # here, not after a successful crawl -- failing late orphans a completed
    # crawl as a stuck `running` row.
    threshold = truncation_threshold_seconds()
    started = time.monotonic()
    inst = None
    try:
        with redirect_stdout(capture), redirect_stderr(capture):
            inst = cls(db_conn=conn, delay=delay)
            # No-op unless LIBERTREE_ENUMERATION_PLAN supplies legacy overrides.
            apply_legacy_overrides(inst)
            if process_files:
                inst.delivery_document_saved = lambda seq: file_jobs.enqueue(conn, job["id"], seq)
            inst.delivery_mode = mode
            inst.delivery_should_cancel = should_cancel or (lambda: False)
            inst.delivery_cursor = start_cursor
            if scope is not None:
                definition = scope['definition']
                inst.crawl_date_plan(definition['lower'], definition['upper'], definition['ranges'],
                                     limit=job.get('limit_n'), max_requests=20,
                                     wall_seconds=wall_budget_seconds())
            else:
                from delivery.incremental_registry import adapter_for
                from crawler.incremental import Collector, Policy
                adapter = adapter_for(inst)
                if adapter is not None:
                    # The shared queue has its own durable discovery/delivery
                    # cursor; a legacy backfill cursor is never a full baseline.
                    cursor_enabled = True
                    policy = Policy(
                        known_run=int(os.environ.get("LIBERTREE_KNOWN_RUN", "50")),
                        overlap_pages=int(os.environ.get("LIBERTREE_OVERLAP_PAGES", "1")),
                        full_check_days=float(os.environ.get("LIBERTREE_FULL_CHECK_DAYS", "30")),
                        max_pages=int(os.environ.get("LIBERTREE_MAX_PAGES", "200")),
                        wall_seconds=wall_budget_seconds())
                    Collector(inst, adapter, policy).run(limit=job.get("limit_n"))
                else:
                    # A save-time known streak cannot establish the initial
                    # baseline or the required extra list page. Unadapted sites
                    # use full traversal and are reported as unsupported.
                    inst.DELIVERY_STOP_ON_KNOWN = False
                    if mode == "incremental":
                        inst.delivery_mode = "full"
                    from delivery.incremental_status import eligibility
                    from crawler.incremental import Store
                    fallback = Store(conn, site_id)
                    fallback.checkpoint({**eligibility(site_id, cls), "support": "full_rescan_required",
                                         "phase": "legacy_full_rescan", "baseline_complete": False})
                    inst.crawl(limit=job.get("limit_n"))
    except CrawlUpToDate:
        # 신규분이 소진됐다는 정상 신호 -- 완료로 종결하고 잘림 판정은 하지 않는다.
        conn.rollback()
        saved = max(0, _count_site_docs(conn, site_id) - before)
        finished = jobs.finish_job(conn, job["id"], saved_count=saved)
        if finished is not None and cursor_enabled:
            # 소유권이 있다면 커서는 항상 남긴다 -- 취소든 완료든 up-to-date
            # 까지 걸은 것은 진짜다.
            _persist_advance(conn, site_id, inst, start_cursor, mode, scope_id)
            if (scope is None and mode == "backfill" and finished == "done"
                    and getattr(inst, "delivery_exhausted", False)):
                # Only an independently observed source end can certify the
                # backfill; a known boundary alone is insufficient.
                jobs.mark_backfill_complete(conn, site_id)
        record_output()
        return saved
    except CrawlCancelled:
        conn.rollback()
        saved = max(0, _count_site_docs(conn, site_id) - before)
        finished = jobs.finish_job(conn, job["id"], saved_count=saved)
        # 취소여도 걸은 만큼은 진짜다: 커서는 남기고, 체인(재큐잉)만 끊는다.
        if finished is not None and cursor_enabled:
            _persist_advance(conn, site_id, inst, start_cursor, mode, scope_id)
        record_output()
        return saved
    except Exception as exc:  # noqa: BLE001
        # SQL errors leave psycopg transactions aborted; clear that state before
        # recording the durable failure transition.
        conn.rollback()
        saved = max(0, _count_site_docs(conn, site_id) - before)
        finished = jobs.fail_job(conn, job["id"], f"{type(exc).__name__}: {exc}", saved_count=saved)
        if finished is not None and cursor_enabled and inst is not None:
            _persist_advance(conn, site_id, inst, start_cursor, mode, scope_id)
        record_output()
        return saved
    saved = _count_site_docs(conn, site_id) - before
    source_failure = getattr(inst, "delivery_source_failure", None)
    if source_failure:
        # Explicit crawler evidence, not a guess from zero new rows or stdout.
        finished = jobs.fail_job(conn, job["id"], f"source failure: {source_failure}",
                                 saved_count=max(0, saved))
        if finished is not None and cursor_enabled:
            _persist_advance(conn, site_id, inst, start_cursor, mode, scope_id)
        record_output()
        return max(0, saved)
    # truncated 는 이제 finish_job 의 truncated 컬럼(운영자에게 "시간에 걸렸다"는
    # 정보)에만 쓰인다 -- 완료·재큐잉 판정에는 더 이상 관여하지 않는다. doaj 의
    # 200페이지 조각(~16분)은 25분 예산의 truncation 임계 아래라 "안 잘렸다"로
    # 보이지만 목록은 13,373,055 건 중 만 건도 못 걸었다; 시간으로 완주를
    # 추론하면 그 조각에서 곧바로 완주 처리되어 체인이 죽는다.
    truncated = (time.monotonic() - started) >= threshold
    finished = jobs.finish_job(conn, job["id"], saved_count=max(0, saved), truncated=truncated)
    if finished == "cancelled":
        # 정상 반환과 동시에 취소가 도착했다(연결 종료 직전 running -> cancelling).
        # CrawlCancelled 예외 경로와 동일하게 다룬다: 커서는 남기고 완료·재큐잉은
        # 건너뛴다 -- 그러지 않으면 운영자의 취소가 조용히 무시된다.
        if cursor_enabled:
            _persist_advance(conn, site_id, inst, start_cursor, mode, scope_id)
    elif finished == "done":
        # 이 작업을 아직 우리가 소유할 때만 커서/완료/재큐잉을 만진다. finished
        # 가 None 이면(=lease 가 이미 회수되어 다른 곳에서 이 행을 넘겨받았으면)
        # 아무것도 만지지 않는다.
        advanced = cursor_enabled and _persist_advance(conn, site_id, inst, start_cursor, mode, scope_id)
        if scope is not None:
            if getattr(inst, 'delivery_date_plan_exhausted', False):
                scopes.mark_complete(conn, scope_id)
                jobs.log_event(conn, job['id'], 'scope_completed', '지정한 날짜 범위 처리가 완료되었습니다. 사이트 전체·파일 검증은 별도입니다.')
                conn.commit()
            elif advanced:
                # Empty/excluded ranges also advance a valid observed scope.
                try:
                    jobs.enqueue_job(conn, site_id, mode='backfill', limit_n=job.get('limit_n'),
                                     requested_by='auto-scoped-backfill', process_files=process_files, scope_id=scope_id)
                except jobs.ActiveJobError:
                    jobs.log_event(conn, job['id'], 'requeue_skipped', '활성 작업이 있어 범위 작업의 자동 재개를 멈춥니다.', level='warning')
                    conn.commit()
            else:
                jobs.log_event(conn, job['id'], 'stalled', '범위 진도가 전진하지 않아 자동 재개를 멈춥니다.', level='warning')
                conn.commit()
        elif mode == "backfill":
            # 항목 목격의 증거는 크롤러의 보고(items_done)와 실제 저장 수 중
            # 어느 쪽이든 인정한다 -- 보고를 빠뜨린 크롤러 때문에 진짜 전진이
            # 있는 체인을 끊으면 안 된다.
            items_seen = (getattr(inst, "delivery_cursor_items_done", 0) > 0 or saved > 0
                          or getattr(inst, "delivery_incremental_pending", False))
            if getattr(inst, "delivery_exhausted", False):
                # 크롤러가 "끝까지 걸었다"고 명시했을 때만 완주다. 시간·예산으로
                # 추론하지 않는다 -- 그것이 이 판정을 바꾼 이유다.
                jobs.mark_backfill_complete(conn, site_id)
            elif advanced and items_seen:
                # 잘렸든 아니든(예산 소진·일시 실패 조각 포함) 전진이 있으면
                # 체인은 계속된다. 전진이 재큐잉의 유일한 면허다. 새 INSERT 는
                # created_at 순서상 큐 맨 뒤.
                try:
                    jobs.enqueue_job(conn, site_id, mode="backfill",
                                     limit_n=job.get("limit_n"), requested_by="auto-backfill",
                                     process_files=process_files)
                except jobs.ActiveJobError:
                    # 경쟁 행이 이미 있다 -- 체인은 그 행이 잇는다. 죽을 일이 아니다.
                    jobs.log_event(conn, job["id"], "requeue_skipped",
                                   "활성 작업이 이미 있어 자동 재큐잉을 건너뜁니다.", level="warning")
                    conn.commit()
            else:
                reason = ("커서가 전진하지 않아" if not advanced
                          else "항목을 하나도 목격하지 못해")
                jobs.log_event(conn, job["id"], "stalled",
                               f"{reason} 자동 재큐잉을 멈춥니다.", level="warning")
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
