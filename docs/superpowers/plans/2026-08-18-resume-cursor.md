# 재개(cursor) — 백필·증분·진도 표시 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 10만 건 이상 25개 사이트가 25분 예산에 잘려도 조각을 이어받아 완주하고, 완주 후 증분이 싸게 돌고, 콘솔이 진도를 보여주게 한다.

**Architecture:** 사이트별 진도 테이블(`crawl_site_progress`) + 워커의 커서 주입·저장·자동 재큐잉 + BaseCrawler 의 작은 계약(`delivery_cursor`/`_advance_cursor`/`DELIVERY_ORDER`/`CrawlUpToDate`) + 크롤러 25개의 기계적 패치. 기본값에서 기존 785개 크롤러의 동작은 불변이며 이를 테스트로 고정한다.

**Tech Stack:** Python 3 (psycopg, unittest, 일회용 postgres 컨테이너) · FastAPI · Next.js 16 + vitest(jsdom 없음, 순수 함수 lib 규약)

**Spec:** `docs/superpowers/specs/2026-08-18-resume-cursor-design.md`

## Global Constraints

- **실사이트 크롤 절대 금지.** 모든 검증은 가짜 크롤러(레지스트리/`crawler_registry` 파라미터 주입)와 존재하지 않는 site_id, 일회용 `--tmpfs --rm` postgres 컨테이너만 사용한다. 운영 스택(`libertree-delivery`)·리허설 외 실행 금지, `down -v` 금지.
- **`git add -A` / `git add .` 금지** — 다른 세션이 같은 브랜치에 커밋한다. 파일명 명시 스테이징. `delivery/fe/tsconfig.json`·`next-env.d.ts` 스테이징 금지.
- **기본값 불변 원칙:** 커서를 안 쓰는 크롤러(785개)와 `incremental`/`full` 모드는 종전과 동일하게 동작해야 하며, 각 태스크의 테스트가 이를 고정한다.
- `mode="backfill"` 은 **작업(job) 전용** — 스케줄 API 는 종전 `Literal["incremental","full"]` 유지.
- 완주 시 `completed_at` 만 기록하고 **cursor 는 지우지 않는다** (oldest_first 증분이 이어 씀).
- 재큐잉의 유일한 면허는 **커서 전진**. 전진 없으면 재큐잉 없음.
- FE CSS 토큰은 `globals.css` `:root` 의 기존 목록만: `--canvas --surface --ink --muted --rule --moss --moss-deep --moss-wash --sand-wash --bad --bad-wash --sans --serif --mono`. 새 서버 데이터 페이지는 `export const dynamic = "force-dynamic"` 필수(`tests/route-rendering.test.ts` 가 강제).
- Python 테스트 실행(리포지토리 루트):
  ```bash
  docker rm -f rc-testpg >/dev/null 2>&1
  docker run -d --rm --name rc-testpg --tmpfs /var/lib/postgresql/data \
    -e POSTGRES_PASSWORD=devpw -e POSTGRES_USER=libertree -e POSTGRES_DB=libertree postgres:16 >/dev/null
  until docker exec rc-testpg pg_isready -U libertree -d libertree >/dev/null 2>&1; do sleep 1; done
  docker run --rm --network container:rc-testpg -v /mnt/raid/ruci_workspace/frwaler-delivery:/app -w /app \
    -e TEST_PG_DSN='postgresql://libertree:devpw@127.0.0.1:5432/libertree' -e DELIVERY_AUTH_MODE=disabled \
    libertree-delivery-worker:latest python -m unittest <모듈들> -v
  docker rm -f rc-testpg >/dev/null 2>&1
  ```
- 커밋 트레일러: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

### Task 1: 스키마 — crawl_site_progress

**Files:**
- Modify: `delivery/db/schema.py` (CREATE TABLE 블록 뒤, `REQUIRED_TABLES`(:46), `required_columns`(:62))
- Test: `tests/test_delivery_migration.py` (기존 파일에 추가)

**Interfaces:**
- Produces: 테이블 `crawl_site_progress(site_id TEXT PK, cursor JSONB, items_done BIGINT NOT NULL DEFAULT 0, total_estimate BIGINT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ)` — Task 2·4·8 이 그대로 사용

- [ ] **Step 1: 실패하는 테스트** — `tests/test_delivery_migration.py` 의 기존 클래스에 추가 (파일을 먼저 읽고 기존 헬퍼/스타일에 맞출 것):

```python
    def test_crawl_site_progress_table_exists_with_required_columns(self):
        cols = {r["column_name"] for r in self.conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='crawl_site_progress'").fetchall()}
        self.assertLessEqual(
            {"site_id", "cursor", "items_done", "total_estimate",
             "updated_at", "completed_at"}, cols)

    def test_migration_is_idempotent_for_progress_table(self):
        # 두 번 돌려도 무해해야 기존 설치에 안전하다.
        schema.migrate(self.conn)
        schema.migrate(self.conn)
```

(`schema.migrate` 가 아닌 다른 진입점 이름이면 파일이 이미 쓰는 그 이름을 쓴다.)

- [ ] **Step 2: 실패 확인** — 공통 명령으로 `tests.test_delivery_migration` 실행. Expected: FAIL (테이블 없음)

- [ ] **Step 3: 구현** — `delivery/db/schema.py` 의 crawl_jobs CREATE 블록(:90 부근) 뒤에 같은 스타일로:

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crawl_site_progress (
            site_id        TEXT PRIMARY KEY,
            cursor         JSONB,
            items_done     BIGINT NOT NULL DEFAULT 0,
            total_estimate BIGINT,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at   TIMESTAMPTZ
        )""")
```

`REQUIRED_TABLES`(:46) 에 `"crawl_site_progress"` 추가. `required_columns`(:62) 에 `("crawl_site_progress", "cursor")`, `("crawl_site_progress", "items_done")`, `("crawl_site_progress", "total_estimate")`, `("crawl_site_progress", "completed_at")` 추가.

- [ ] **Step 4: 통과 확인** — `tests.test_delivery_migration` 전체. Expected: PASS (기존 테스트 포함)
- [ ] **Step 5: 커밋**

```bash
git add delivery/db/schema.py tests/test_delivery_migration.py
git commit -m "feat(delivery): add crawl_site_progress for resumable backfills"
```

---

### Task 2: jobs.py 진도 헬퍼 + backfill 모드 허용

**Files:**
- Modify: `delivery/worker/jobs.py` (파일 끝에 헬퍼 추가)
- Modify: `delivery/be/app.py:34` (`JobCreate.mode` 의 Literal 만 — :47 스케줄은 그대로)
- Test: `tests/test_worker_jobs.py`, `tests/test_be_app.py` (기존 파일에 추가)

**Interfaces:**
- Consumes: Task 1 의 테이블
- Produces: `jobs.load_cursor(conn, site_id) -> dict | None` · `jobs.save_progress(conn, site_id, cursor: dict, items_delta: int = 0) -> None` (업서트, items_done 누적, 커밋 포함) · `jobs.mark_backfill_complete(conn, site_id) -> None` — Task 4 가 이 이름·시그니처 그대로 호출

- [ ] **Step 1: 실패하는 테스트** — `tests/test_worker_jobs.py` 에 추가:

```python
    def test_progress_roundtrip_accumulates_items(self):
        jobs.save_progress(self.conn, "rc-fake-site", {"page": 5}, items_delta=100)
        jobs.save_progress(self.conn, "rc-fake-site", {"page": 9}, items_delta=50)
        cur = jobs.load_cursor(self.conn, "rc-fake-site")
        self.assertEqual(cur, {"page": 9})
        row = self.conn.execute(
            "SELECT items_done, completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-fake-site",)).fetchone()
        self.assertEqual(row["items_done"], 150)
        self.assertIsNone(row["completed_at"])

    def test_load_cursor_returns_none_for_unknown_site(self):
        self.assertIsNone(jobs.load_cursor(self.conn, "rc-never-seen"))

    def test_mark_complete_keeps_cursor(self):
        # oldest_first 증분이 커서를 이어 쓰므로, 완주가 커서를 지우면 안 된다.
        jobs.save_progress(self.conn, "rc-done-site", {"offset": 900}, items_delta=900)
        jobs.mark_backfill_complete(self.conn, "rc-done-site")
        self.assertEqual(jobs.load_cursor(self.conn, "rc-done-site"), {"offset": 900})
        row = self.conn.execute(
            "SELECT completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-done-site",)).fetchone()
        self.assertIsNotNone(row["completed_at"])
```

`tests/test_be_app.py` 에 추가 (기존의 작업 생성 테스트 스타일을 따르되, **존재하지 않는 site_id 라 422/404 로 거절되는 기존 검증 경로 안에서** mode 만 확인):

```python
    def test_backfill_mode_is_accepted_by_job_schema_but_not_schedule_schema(self):
        # 스키마 수준 검증: backfill 은 작업에서 유효, 스케줄에서 무효.
        r = self.client.post("/jobs", json={"site_id": "rc-no-such-site", "mode": "backfill"})
        self.assertNotEqual(r.status_code, 422)   # mode 로는 거절되지 않는다
        r2 = self.client.put("/schedules/rc-no-such-site", json={"mode": "backfill"})
        self.assertEqual(r2.status_code, 422)     # 스케줄은 backfill 불허
```

(기존 파일의 클라이언트 픽스처·인증 비활성 패턴을 먼저 읽고 맞출 것. `/jobs` POST 가 존재하지 않는 site 를 404 등으로 거절한다면 그 코드를 기대값으로 쓰되, 422(스키마 거절)가 **아님**만 고정하면 된다.)

- [ ] **Step 2: 실패 확인** — `tests.test_worker_jobs tests.test_be_app`. Expected: FAIL (헬퍼 없음 / Literal 거절)

- [ ] **Step 3: 구현** — `delivery/worker/jobs.py` 끝에:

```python
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
    """커서 업서트 + items_done 누적. 워커의 종결 전이와 같은 결로 커밋한다."""
    conn.execute(
        """
        INSERT INTO crawl_site_progress (site_id, cursor, items_done, updated_at)
        VALUES (%s, %s::jsonb, %s, now())
        ON CONFLICT (site_id) DO UPDATE
           SET cursor = EXCLUDED.cursor,
               items_done = crawl_site_progress.items_done + EXCLUDED.items_done,
               updated_at = now()
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
```

파일 상단 import 에 `json` 이 없으면 추가. `delivery/be/app.py:34` 를 `mode: Literal["incremental","full","backfill"] = "incremental"` 로 — **:47 은 건드리지 않는다.**

- [ ] **Step 4: 통과 확인** — `tests.test_worker_jobs tests.test_be_app` 전체 PASS
- [ ] **Step 5: 커밋**

```bash
git add delivery/worker/jobs.py delivery/be/app.py tests/test_worker_jobs.py tests/test_be_app.py
git commit -m "feat(delivery): persist per-site backfill progress and accept backfill jobs"
```

---

### Task 3: BaseCrawler 계약

**Files:**
- Modify: `crawler/base_crawler.py` (`CrawlCancelled` 옆에 예외, `__init__`(:27), 클래스 속성, `_save_paper_v2` 의 incremental 분기(:203-210))
- Test: `tests/test_crawler_cursor_contract.py` (새 파일)

**Interfaces:**
- Consumes: 없음 (독립)
- Produces: `CrawlUpToDate` 예외 · 인스턴스 속성 `delivery_cursor`, `_pending_cursor`, `_cursor_items_done` · 메서드 `_advance_cursor(cursor: dict, items_done: int = 0)` · 클래스 속성 `DELIVERY_ORDER = "arbitrary"`, `UP_TO_DATE_THRESHOLD = 50` — Task 4(워커)·5~7(크롤러) 이 그대로 사용

- [ ] **Step 1: 실패하는 테스트** — `tests/test_crawler_cursor_contract.py` (새 파일; DB 가 필요 없는 부분은 `db_conn=None` 으로):

```python
# -*- coding: utf-8 -*-
"""BaseCrawler 커서 계약: 추가만, 기본값에서 기존 동작 불변."""
import unittest

from crawler.base_crawler import BaseCrawler, CrawlUpToDate


class _Fake(BaseCrawler):
    site_id = "rc-fake"
    site_name = "fake"
    base_url = "http://invalid.invalid"

    def crawl(self, limit=None):
        return None


class CursorContractTest(unittest.TestCase):
    def _inst(self):
        return _Fake(db_conn=None)

    def test_defaults_change_nothing(self):
        inst = self._inst()
        self.assertIsNone(inst.delivery_cursor)
        self.assertIsNone(inst._pending_cursor)
        self.assertEqual(inst._cursor_items_done, 0)
        self.assertEqual(type(inst).DELIVERY_ORDER, "arbitrary")
        self.assertEqual(inst.delivery_mode, "incremental")  # 기존 기본값 유지

    def test_advance_cursor_records_in_memory_only(self):
        inst = self._inst()
        inst._advance_cursor({"page": 3}, items_done=50)
        inst._advance_cursor({"page": 4}, items_done=50)
        self.assertEqual(inst._pending_cursor, {"page": 4})
        self.assertEqual(inst._cursor_items_done, 100)

    def test_advance_cursor_copies_the_dict(self):
        inst = self._inst()
        cur = {"page": 1}
        inst._advance_cursor(cur)
        cur["page"] = 999
        self.assertEqual(inst._pending_cursor, {"page": 1})

    def test_up_to_date_is_a_runtime_error_like_cancelled(self):
        # 워커가 CrawlCancelled 와 같은 방식으로 잡는 제어 흐름 예외다.
        self.assertTrue(issubclass(CrawlUpToDate, RuntimeError))


if __name__ == "__main__":
    unittest.main()
```

그리고 `tests/test_worker_run.py` 에 CrawlUpToDate 카운터의 DB 경합 테스트를 추가한다 (기존 파일의 가짜 크롤러·pg 픽스처 패턴을 먼저 읽고 맞출 것 — 반드시 실존하지 않는 site_id 사용):

```python
    def test_incremental_newest_first_stops_after_threshold_known_docs(self):
        # 같은 문서를 임계+1 회 저장 시도하는 가짜 크롤러: 첫 회는 신규 저장,
        # 이후는 기보유 → 연속 카운터가 임계에 닿으면 CrawlUpToDate.
        class UpToDateCrawler(_FakeBase):          # 기존 픽스처의 가짜 베이스를 따름
            DELIVERY_ORDER = "newest_first"
            UP_TO_DATE_THRESHOLD = 3

            def crawl(self, limit=None):
                for i in range(10):
                    self._save_paper_v2({
                        "site_id": self.site_id, "post_number": "fixed-1",
                        "meta_url": "http://invalid.invalid/1", "title": "t"})
                raise AssertionError("threshold 에서 CrawlUpToDate 가 났어야 한다")
```

워커 처리(정상 done)는 Task 4 테스트가 고정한다 — 이 태스크에서는 예외 발생 자체만.

- [ ] **Step 2: 실패 확인** — `tests.test_crawler_cursor_contract` FAIL (속성 없음)

- [ ] **Step 3: 구현** — `crawler/base_crawler.py`:

`CrawlCancelled` 정의 바로 아래:

```python
class CrawlUpToDate(RuntimeError):
    """Incremental crawl reached already-collected territory (newest-first only).

    Raised from _save_paper_v2 after UP_TO_DATE_THRESHOLD consecutive
    already-known documents, so custom crawl loops stop without per-crawler
    changes -- the same control-flow pattern as CrawlCancelled.
    """
```

클래스 본문(USER_AGENT 옆)에:

```python
    # 재개(cursor) 계약 -- 기본값이면 아무것도 달라지지 않는다.
    DELIVERY_ORDER = "arbitrary"      # newest_first | oldest_first | arbitrary
    UP_TO_DATE_THRESHOLD = 50
```

`__init__` 의 `self._last_save_created = False` 아래:

```python
        self.delivery_cursor = None      # 워커가 주입하는 시작점 (읽기 전용으로 쓸 것)
        self._pending_cursor = None      # _advance_cursor 가 기록, 워커가 회수해 저장
        self._cursor_items_done = 0
        self._consecutive_known = 0
```

헬퍼(`_check_cancelled` 옆):

```python
    def _advance_cursor(self, cursor, items_done=0):
        """페이지 하나를 끝낼 때 호출: 다음 실행이 시작할 지점을 보고한다.

        메모리에만 기록한다 -- DB 저장과 커밋은 워커의 종결 전이에서 일어난다.
        크롤 도중에 저장하면 실패한 크롤의 커서가 남는다.
        """
        self._pending_cursor = dict(cursor)
        self._cursor_items_done += int(items_done)
```

`_save_paper_v2` 의 incremental 분기(:206-208)를 다음으로 교체하고, 신규 저장 뒤 리셋을 추가:

```python
        if self.delivery_mode == "incremental" and existing is not None:
            self._last_save_created = False
            self._consecutive_known += 1
            if (self.DELIVERY_ORDER == "newest_first"
                    and self._consecutive_known >= self.UP_TO_DATE_THRESHOLD):
                raise CrawlUpToDate(
                    f"{self._consecutive_known} consecutive known documents")
            return existing
        seq_id = backend.insert_document(self._conn, doc)
        self._last_save_created = existing is None
        if existing is None:
            self._consecutive_known = 0
```

- [ ] **Step 4: 통과 확인** — `tests.test_crawler_cursor_contract tests.test_worker_run` PASS
- [ ] **Step 5: 커밋**

```bash
git add crawler/base_crawler.py tests/test_crawler_cursor_contract.py tests/test_worker_run.py
git commit -m "feat(crawler): cursor contract on BaseCrawler with up-to-date early stop"
```

---

### Task 4: 워커 — 커서 주입·저장·자동 재큐잉

**Files:**
- Modify: `delivery/worker/worker.py` `run_job`(:107-157)
- Test: `tests/test_worker_run.py`

**Interfaces:**
- Consumes: Task 2 헬퍼 3개, Task 3 의 `CrawlUpToDate`·`delivery_cursor`·`_pending_cursor`·`_cursor_items_done`·`DELIVERY_ORDER`
- Produces: backfill 실행 의미론 — 이후 태스크는 이 동작을 전제

- [ ] **Step 1: 실패하는 테스트** — `tests/test_worker_run.py` 에 추가 (기존 가짜 크롤러·registry 주입 패턴 준수; site_id 는 전부 `rc-` 접두 가짜):

```python
    def test_truncated_backfill_saves_cursor_and_requeues_at_tail(self):
        # 가짜 크롤러: 커서에서 시작해 두 페이지 걷고 _advance_cursor 보고,
        # 테스트가 예산을 0 에 가깝게 줄여 잘림 판정을 강제한다.
        job = self._enqueue("rc-backfill-a", mode="backfill")
        self._run(job, budget_s=0.01)          # 기존 픽스처의 예산 축소 헬퍼 사용
        cur = jobs.load_cursor(self.conn, "rc-backfill-a")
        self.assertEqual(cur, {"page": 3})
        tail = self.conn.execute(
            "SELECT site_id, mode, status FROM crawl_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((tail["site_id"], tail["mode"], tail["status"]),
                         ("rc-backfill-a", "backfill", "queued"))

    def test_no_advance_means_no_requeue(self):
        # _advance_cursor 를 한 번도 부르지 않는 가짜 크롤러 + 잘림 강제
        before = self.conn.execute("SELECT count(*) AS n FROM crawl_jobs").fetchone()["n"]
        job = self._enqueue("rc-backfill-stuck", mode="backfill")
        self._run(job, budget_s=0.01)
        after = self.conn.execute("SELECT count(*) AS n FROM crawl_jobs").fetchone()["n"]
        self.assertEqual(after, before + 1)     # 재큐잉 없음 (원 작업 1건뿐)

    def test_completed_backfill_marks_done_and_stops_chain(self):
        job = self._enqueue("rc-backfill-done", mode="backfill")
        self._run(job)                          # 예산 넉넉 → 완주
        row = self.conn.execute(
            "SELECT completed_at, cursor FROM crawl_site_progress WHERE site_id=%s",
            ("rc-backfill-done",)).fetchone()
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNotNone(row["cursor"])     # 커서는 지우지 않는다
        tail = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s AND status='queued'",
            ("rc-backfill-done",)).fetchone()
        self.assertEqual(tail["n"], 0)

    def test_cancelled_backfill_saves_cursor_but_breaks_chain(self):
        # 기존 취소 테스트 픽스처 재사용: 취소 경로에서 커서는 저장, 재큐잉은 없음
        ...

    def test_failed_backfill_keeps_previous_cursor(self):
        jobs.save_progress(self.conn, "rc-backfill-boom", {"page": 7})
        job = self._enqueue("rc-backfill-boom", mode="backfill")
        self._run(job)                          # 가짜 크롤러가 예외를 던짐
        self.assertEqual(jobs.load_cursor(self.conn, "rc-backfill-boom"), {"page": 7})

    def test_up_to_date_is_finished_as_plain_done(self):
        job = self._enqueue("rc-uptodate", mode="incremental")
        self._run(job)                          # CrawlUpToDate 를 던지는 가짜 크롤러
        row = self.conn.execute(
            "SELECT status, truncated FROM crawl_jobs WHERE id=%s", (job,)).fetchone()
        self.assertEqual((row["status"], row["truncated"]), ("done", False))

    def test_oldest_first_incremental_gets_cursor_and_persists_advance(self):
        jobs.save_progress(self.conn, "rc-oldest", {"offset": 60})
        job = self._enqueue("rc-oldest", mode="incremental")
        self._run(job)                          # DELIVERY_ORDER="oldest_first" 가짜
        self.assertEqual(jobs.load_cursor(self.conn, "rc-oldest"), {"offset": 120})
        # 단, incremental 은 잘려도 재큐잉하지 않는다 (backfill 전용)
```

`...` 로 표시한 취소 테스트는 기존 `test_cancelled_*` 픽스처의 취소 주입 방식을 그대로 재사용해 작성한다 — 새 메커니즘을 만들지 말 것. 가짜 크롤러들은 파일 안에 정의하고 `crawler_registry={"rc-...": FakeCls}` 로 주입한다. `budget_s` 축소는 기존 truncation 테스트가 이미 쓰는 방법(환경변수 `LIBERTREE_MAX_WALL_S` 패치)을 따른다.

- [ ] **Step 2: 실패 확인** — `tests.test_worker_run` FAIL
- [ ] **Step 3: 구현** — `delivery/worker/worker.py` `run_job` 을 다음 취지로 수정 (기존 구조·주석 보존, 추가 위주):

```python
    mode = job.get("mode", "incremental")
    order = getattr(cls, "DELIVERY_ORDER", "arbitrary")
    start_cursor = None
    if mode == "backfill" or (mode == "incremental" and order == "oldest_first"):
        start_cursor = jobs.load_cursor(conn, site_id)

    threshold = truncation_threshold_seconds()
    started = time.monotonic()
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
        jobs.finish_job(conn, job["id"], saved_count=saved)
        record_output()
        return saved
    except CrawlCancelled:
        conn.rollback()
        saved = max(0, _count_site_docs(conn, site_id) - before)
        jobs.finish_job(conn, job["id"], saved_count=saved)
        # 취소여도 걸은 만큼은 진짜다: 커서는 남기고, 체인(재큐잉)만 끊는다.
        _persist_advance(conn, site_id, inst, start_cursor)
        record_output()
        return saved
    except Exception as exc:  # noqa: BLE001
        ...기존 그대로 (커서 저장 없음)...

    saved = _count_site_docs(conn, site_id) - before
    truncated = (time.monotonic() - started) >= threshold
    jobs.finish_job(conn, job["id"], saved_count=max(0, saved), truncated=truncated)
    advanced = _persist_advance(conn, site_id, inst, start_cursor)
    if mode == "backfill":
        if truncated and advanced:
            # 전진이 재큐잉의 유일한 면허다. 새 INSERT 는 created_at 순서상 큐 맨 뒤.
            jobs.enqueue_job(conn, site_id, mode="backfill",
                             limit_n=job.get("limit_n"), requested_by="auto-backfill")
        elif truncated:
            jobs._log(conn, job["id"], "stalled",
                      "잘렸지만 커서가 전진하지 않아 자동 재큐잉을 멈춥니다.")
            conn.commit()
        else:
            jobs.mark_backfill_complete(conn, site_id)
    record_output()
    return max(0, saved)
```

모듈에 헬퍼 추가:

```python
def _persist_advance(conn, site_id, inst, start_cursor) -> bool:
    """크롤러가 보고한 전진을 저장. 전진 없으면 False (저장도 없음)."""
    pending = getattr(inst, "_pending_cursor", None)
    if pending is None or pending == start_cursor:
        return False
    jobs.save_progress(conn, site_id, pending,
                       items_delta=getattr(inst, "_cursor_items_done", 0))
    return True
```

import 줄에 `CrawlUpToDate` 추가 (`from crawler.base_crawler import CrawlCancelled` 가 있는 곳).

- [ ] **Step 4: 통과 확인** — `tests.test_worker_run tests.test_worker_jobs` 전체 PASS (기존 취소·잘림 테스트 포함)
- [ ] **Step 5: 커밋**

```bash
git add delivery/worker/worker.py tests/test_worker_run.py
git commit -m "feat(delivery): backfill jobs resume from the saved cursor and requeue while advancing"
```

---

### Task 5: HAL 6개 패치 (oldest_first)

**Files:**
- Modify: `crawler/sites/custom/inserm-hal-science-search.py`(:375-461), `anr-hal-science-search.py`(:139-176), `amu-hal-science-search.py`(:137-174), `cea-hal-science-cnrgh.py`(:134-171), `ehess-hal-science-search.py`(:138-175), `ens-lyon-hal-science-search.py`(:169-202)
- Test: `tests/test_crawler_cursor_conformance.py` (새 파일 — Task 5·6·7 이 함께 채움)

**Interfaces:**
- Consumes: Task 3 계약
- Produces: 25개 파일 패턴 준수 (커서 읽기 + `_advance_cursor` 보고 + `DELIVERY_ORDER`)

- [ ] **Step 1: 실패하는 테스트** — `tests/test_crawler_cursor_conformance.py` (새 파일). 파일명이 하이픈이라 import 불가 — **정적 소스 검사**로 고정한다:

```python
# -*- coding: utf-8 -*-
"""25개 대형 사이트 크롤러의 커서 패턴 준수 — 정적 소스 검사.

하이픈 파일명이라 import 할 수 없으므로 소스 텍스트로 확인한다.
여기 실패하면: 해당 파일이 delivery_cursor 를 읽지 않거나(재개 불가),
_advance_cursor 를 부르지 않거나(전진 보고 없음 → 재큐잉 불가),
DELIVERY_ORDER 선언이 없다(증분 전략 미지정).
"""
import unittest
from pathlib import Path

CUSTOM = Path(__file__).resolve().parent.parent / "crawler" / "sites" / "custom"

# site_id -> (기대 DELIVERY_ORDER, 커서 키)
TARGETS = {
    "inserm-hal-science-search": ("oldest_first", "offset"),
    "anr-hal-science-search": ("oldest_first", "offset"),
    "amu-hal-science-search": ("oldest_first", "offset"),
    "cea-hal-science-cnrgh": ("oldest_first", "offset"),
    "ehess-hal-science-search": ("oldest_first", "offset"),
    "ens-lyon-hal-science-search": ("oldest_first", "offset"),
    "openresearch-repository-anu-edu-au-search": ("newest_first", "page"),
    "research-collection-ethz-ch-search": ("newest_first", "page"),
    "dspace-ut-ee-search": ("newest_first", "page"),
    "ostrnrcan-dostrncan-canada-ca-search": ("newest_first", "page"),
    "repositorio-uchile-cl-discover": ("newest_first", "page"),
    "sonar-ch-global": ("newest_first", "page"),
    "cds-cern-ch-collection": ("newest_first", "page"),
    "doaj-org-search": ("newest_first", "page"),
    "pergamos-lib-uoa-gr-search": ("newest_first", "page"),
    "etera-ee-browse": ("newest_first", "offset"),
    "data-gv-at-datasets": ("arbitrary", "page"),
    "data-gov-au-data": ("arbitrary", "offset"),
    "e-stat-go-jp-stat-search": ("arbitrary", "page"),
    "ots-at-pressemappe": ("arbitrary", "page"),
    "ntrs-nasa-gov-search": ("arbitrary", "offset"),
    "prism-go-kr-homepage": ("arbitrary", "page"),
    "etis-ee-portal": ("arbitrary", "page"),
    "datacatalogue-adruk-org-browser": ("arbitrary", "page"),
    "mof-go-kr-doc": ("arbitrary", "page"),
}
# 이번 태스크(HAL 6)만 먼저 활성화하고, Task 6·7 이 나머지 키를 살린다.
ACTIVE = {k: v for k, v in TARGETS.items() if v[0] == "oldest_first"}


class CursorConformanceTest(unittest.TestCase):
    def _source(self, site_id):
        return (CUSTOM / f"{site_id}.py").read_text(encoding="utf-8")

    def test_active_targets_follow_the_cursor_pattern(self):
        for site_id, (order, key) in ACTIVE.items():
            src = self._source(site_id)
            with self.subTest(site=site_id):
                self.assertIn("delivery_cursor", src)
                self.assertIn("_advance_cursor", src)
                self.assertIn(f'DELIVERY_ORDER = "{order}"', src)
                self.assertIn(f'"{key}"', src)

    def test_hal_family_uses_stable_docid_sort(self):
        for site_id in ("anr-hal-science-search", "amu-hal-science-search",
                        "ehess-hal-science-search", "ens-lyon-hal-science-search",
                        "inserm-hal-science-search", "cea-hal-science-cnrgh"):
            with self.subTest(site=site_id):
                self.assertIn("docid asc", self._source(site_id))
```

- [ ] **Step 2: 실패 확인** — `tests.test_crawler_cursor_conformance` FAIL
- [ ] **Step 3: 구현** — 6개 파일 각각에 (파일을 먼저 읽고 실제 루프 변수에 맞출 것):

1. 클래스에 `DELIVERY_ORDER = "oldest_first"` 추가
2. 루프 시작 오프셋: `start_offset = (self.delivery_cursor or {}).get("offset", 0)` 을 루프 앞에 두고, `for page in range(...)` 형(anr·amu·ehess·ens-lyon·cea)은 `start_offset + page * _ROWS` 로, `while True` 형(inserm)은 `start = start_offset` 초기화로 반영
3. 각 페이지 처리 끝에 `self._advance_cursor({"offset": <다음 시작 오프셋>}, items_done=len(items))`
4. 무정렬 4개(anr·amu·ehess·ens-lyon)의 요청 파라미터에 `"sort": "docid asc"` 추가 — inserm·cea 가 이미 쓰는 값과 동일 표기
5. **다른 것은 일절 바꾸지 않는다** (파서·필터·딜레이 불변)

- [ ] **Step 4: 통과 확인** — conformance PASS + `python3 -m py_compile` 6개 파일 + `tests.test_crawler_cursor_contract` 재실행
- [ ] **Step 5: 커밋**

```bash
git add crawler/sites/custom/inserm-hal-science-search.py crawler/sites/custom/anr-hal-science-search.py \
        crawler/sites/custom/amu-hal-science-search.py crawler/sites/custom/cea-hal-science-cnrgh.py \
        crawler/sites/custom/ehess-hal-science-search.py crawler/sites/custom/ens-lyon-hal-science-search.py \
        tests/test_crawler_cursor_conformance.py
git commit -m "feat(crawler): resumable cursors for the six HAL crawlers with stable docid ordering"
```

---

### Task 6: DSpace 4 + XMLUI 1 + Invenio 2 패치 (newest_first)

**Files:**
- Modify: `crawler/sites/custom/openresearch-repository-anu-edu-au-search.py`(:37-131), `research-collection-ethz-ch-search.py`(:54-138), `dspace-ut-ee-search.py`(:42-126), `ostrnrcan-dostrncan-canada-ca-search.py`(:42-178), `repositorio-uchile-cl-discover.py`(:35-123), `sonar-ch-global.py`(:267-334), `cds-cern-ch-collection.py`(:251-406)
- Test: `tests/test_crawler_cursor_conformance.py` 의 `ACTIVE` 를 `newest_first` 까지 확장

**Interfaces:** Task 5 와 동일 패턴

- [ ] **Step 1: 테스트 확장** — `ACTIVE = {k: v for k, v in TARGETS.items() if v[0] in ("oldest_first", "newest_first")}` 로 변경. 실행 → 신규 7개 FAIL 확인
- [ ] **Step 2: 구현** — 7개 파일 각각: `DELIVERY_ORDER = "newest_first"` + 시작 페이지 `page = (self.delivery_cursor or {}).get("page", <기존 초기값>)` (0-기준/1-기준은 파일의 기존 초기값을 그대로 따른다) + 페이지 끝마다 `self._advance_cursor({"page": page + 1}, items_done=len(items))`. 다른 변경 없음.
- [ ] **Step 3: 통과 확인** — conformance PASS + `py_compile` 7개
- [ ] **Step 4: 커밋**

```bash
git add crawler/sites/custom/openresearch-repository-anu-edu-au-search.py \
        crawler/sites/custom/research-collection-ethz-ch-search.py crawler/sites/custom/dspace-ut-ee-search.py \
        crawler/sites/custom/ostrnrcan-dostrncan-canada-ca-search.py crawler/sites/custom/repositorio-uchile-cl-discover.py \
        crawler/sites/custom/sonar-ch-global.py crawler/sites/custom/cds-cern-ch-collection.py \
        tests/test_crawler_cursor_conformance.py
git commit -m "feat(crawler): resumable cursors for the DSpace, XMLUI and Invenio crawlers"
```

---

### Task 7: 개별 12개 패치

**Files:**
- Modify: `crawler/sites/custom/doaj-org-search.py`(:137, newest_first·page), `pergamos-lib-uoa-gr-search.py`(:57-146, newest_first·page), `etera-ee-browse.py`(:453-518, newest_first·offset), `e-stat-go-jp-stat-search.py`(:504-576, arbitrary·page), `ots-at-pressemappe.py`(:261, arbitrary·page), `ntrs-nasa-gov-search.py`(:68-249, arbitrary·offset — 루프 변수 `page_from`), `prism-go-kr-homepage.py`(:118-311, arbitrary·page — `startCount=page*20`), `etis-ee-portal.py`(:191-327, arbitrary·page), `datacatalogue-adruk-org-browser.py`(:42-100, arbitrary·page), `data-gov-au-data.py`(:203-263, arbitrary·offset — `start`), `data-gv-at-datasets.py`(:60-136, arbitrary·page), `mof-go-kr-doc.py`(:41-124, arbitrary·page)
- Test: `tests/test_crawler_cursor_conformance.py` 의 `ACTIVE = TARGETS` 로 전체 활성화

**Interfaces:** Task 5 와 동일 패턴. arbitrary 사이트도 **백필 재개는 안전하다** — 같은 실행 계열 안에서 순서가 크게 바뀌지 않는 것에 기대며, 정확성의 최종 방어선은 dedup(UNIQUE 인덱스)이다. 놓친 구간은 완주 후 full 재실행이 회수한다 (이 한계는 spec 5절에 명시됨).

- [ ] **Step 1: 테스트 확장** — `ACTIVE = TARGETS`. 실행 → 신규 12개 FAIL
- [ ] **Step 2: 구현** — 파일별로 읽고: `DELIVERY_ORDER` 선언(표의 값) + 시작값을 커서에서(`page` 또는 `offset`, 파일의 기존 초기값 존중) + 페이지 끝 `_advance_cursor` 보고. doaj·pergamos·etera 는 newest_first 이므로 증분 조기 종료가 함께 활성화된다.
- [ ] **Step 3: 통과 확인** — conformance 전체(25개) PASS + `py_compile` 12개 + 파이썬 전체 스위트(공통 명령의 전 모듈)
- [ ] **Step 4: 커밋**

```bash
git add crawler/sites/custom/doaj-org-search.py crawler/sites/custom/pergamos-lib-uoa-gr-search.py \
        crawler/sites/custom/etera-ee-browse.py crawler/sites/custom/e-stat-go-jp-stat-search.py \
        crawler/sites/custom/ots-at-pressemappe.py crawler/sites/custom/ntrs-nasa-gov-search.py \
        crawler/sites/custom/prism-go-kr-homepage.py crawler/sites/custom/etis-ee-portal.py \
        crawler/sites/custom/datacatalogue-adruk-org-browser.py crawler/sites/custom/data-gov-au-data.py \
        crawler/sites/custom/data-gv-at-datasets.py crawler/sites/custom/mof-go-kr-doc.py \
        tests/test_crawler_cursor_conformance.py
git commit -m "feat(crawler): resumable cursors for the twelve bespoke large-site crawlers"
```

---

### Task 8: 시드 스크립트 + GET /progress

**Files:**
- Create: `delivery/scripts/seed_backfill_estimates.py`
- Modify: `delivery/be/app.py` (`/jobs/summary` 등 기존 GET 옆에 `/progress` 추가)
- Test: `tests/test_be_app.py`, `tests/test_delivery_seed.py` (기존 파일들에 추가)

**Interfaces:**
- Consumes: Task 1 테이블, `scripts/audit/capacity_corrected.csv` (`site_id`, `corrected_max` 컬럼)
- Produces: `GET /progress` → `{"sites": [{"site_id", "cursor", "items_done", "total_estimate", "docs_in_db", "updated_at", "completed_at"}, ...]}` — Task 9 FE 가 이 모양 그대로 소비

- [ ] **Step 1: 실패하는 테스트** — `tests/test_delivery_seed.py` 에 (기존 시드 테스트 스타일 준수):

```python
    def test_seed_backfill_estimates_upserts_only_100k_sites(self):
        from delivery.scripts.seed_backfill_estimates import seed
        n = seed(self.conn, csv_path="scripts/audit/capacity_corrected.csv")
        self.assertEqual(n, 25)
        row = self.conn.execute(
            "SELECT total_estimate FROM crawl_site_progress WHERE site_id=%s",
            ("doaj-org-search",)).fetchone()
        self.assertEqual(row["total_estimate"], 13373055)
        # 재실행해도 행이 늘지 않는다
        self.assertEqual(seed(self.conn, csv_path="scripts/audit/capacity_corrected.csv"), 25)
```

`tests/test_be_app.py` 에:

```python
    def test_progress_endpoint_returns_rows_and_requires_operator(self):
        jobs.save_progress(self.conn, "rc-prog-site", {"page": 4}, items_delta=200)
        r = self.client.get("/progress")
        self.assertEqual(r.status_code, 200)
        rows = {s["site_id"]: s for s in r.json()["sites"]}
        self.assertIn("rc-prog-site", rows)
        self.assertEqual(rows["rc-prog-site"]["items_done"], 200)
        self.assertIn("docs_in_db", rows["rc-prog-site"])
```

(인증 테스트는 기존 파일이 authed 엔드포인트에 쓰는 방식과 동일하게 — `require_operator` 의존성 추가 여부를 기존 GET 들과 같게 맞추고 그 검증 패턴을 복사한다. 기존 GET 이 인증 없이 열려 있다면 이 엔드포인트도 그 관례를 따르고 테스트에서 인증 단언을 뺀다 — **관례 우선**.)

- [ ] **Step 2: 실패 확인** → **Step 3: 구현**

`delivery/scripts/seed_backfill_estimates.py`:

```python
# -*- coding: utf-8 -*-
"""capacity_corrected.csv 의 corrected_max 를 crawl_site_progress.total_estimate 로 시드.

10만 건 이상(백필 대상)만 넣는다. 재실행 무해(업서트). 네트워크 접근 없음.

Usage:
    python3 delivery/scripts/seed_backfill_estimates.py   # LIBERTREE_PG_DSN 사용
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

THRESHOLD = 100_000


def seed(conn, csv_path="scripts/audit/capacity_corrected.csv") -> int:
    n = 0
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            raw = row.get("corrected_max") or ""
            if not raw:
                continue
            estimate = int(raw)
            if estimate < THRESHOLD:
                continue
            conn.execute(
                """
                INSERT INTO crawl_site_progress (site_id, total_estimate, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (site_id) DO UPDATE
                   SET total_estimate = EXCLUDED.total_estimate, updated_at = now()
                """,
                (row["site_id"], estimate),
            )
            n += 1
    conn.commit()
    return n


if __name__ == "__main__":
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["LIBERTREE_PG_DSN"], row_factory=dict_row) as conn:
        print(f"seeded {seed(conn)} sites")
```

`delivery/be/app.py` — 기존 GET 핸들러들 옆에 (의존성·커넥션 획득은 이웃 핸들러의 방식을 그대로 복사):

```python
@app.get("/progress")
def backfill_progress(...기존 GET 과 동일한 의존성...):
    rows = conn.execute(
        """
        SELECT p.site_id, p.cursor, p.items_done, p.total_estimate,
               p.updated_at, p.completed_at,
               (SELECT count(*) FROM documents d WHERE d.site_id = p.site_id) AS docs_in_db
          FROM crawl_site_progress p
         ORDER BY p.completed_at NULLS FIRST, p.updated_at DESC
        """).fetchall()
    return {"sites": [dict(r) for r in rows]}
```

- [ ] **Step 4: 통과 확인** — `tests.test_delivery_seed tests.test_be_app` PASS
- [ ] **Step 5: 커밋**

```bash
git add delivery/scripts/seed_backfill_estimates.py delivery/be/app.py \
        tests/test_delivery_seed.py tests/test_be_app.py
git commit -m "feat(delivery): seed backfill targets and expose per-site progress"
```

---

### Task 9: FE — 백필 진행 화면

**Files:**
- Create: `delivery/fe/src/lib/backfill-progress.ts`
- Create: `delivery/fe/src/app/api/progress/route.ts` (기존 `api/jobs/route.ts` 프록시 패턴 복사)
- Create: `delivery/fe/src/app/(shell)/backfill/page.tsx` (**`export const dynamic = "force-dynamic"` 필수**)
- Modify: 내비게이션 컴포넌트 (경로는 `(shell)` 레이아웃에서 확인) — "백필" 항목 추가
- Test: `delivery/fe/tests/backfill-progress.test.ts`

**Interfaces:**
- Consumes: Task 8 의 `GET /progress` 응답 모양
- Produces: `formatBackfillPercent(itemsDone, totalEstimate): string | null` · `backfillStateLabel(row): "시작 전" | "진행 중" | "완주"`

- [ ] **Step 1: 실패하는 테스트** — `delivery/fe/tests/backfill-progress.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { backfillStateLabel, formatBackfillPercent } from "@/lib/backfill-progress";

describe("formatBackfillPercent", () => {
  it("formats a sane ratio with one decimal", () => {
    expect(formatBackfillPercent(1_337_305, 13_373_055)).toBe("10.0%");
  });
  it("caps display at 100% even when estimate is stale", () => {
    // total_estimate 는 추정치다 -- 초과 수집이 120% 로 보이면 신뢰를 잃는다.
    expect(formatBackfillPercent(150, 100)).toBe("100%");
  });
  it("returns null without a usable estimate", () => {
    expect(formatBackfillPercent(10, null)).toBeNull();
    expect(formatBackfillPercent(10, 0)).toBeNull();
    expect(formatBackfillPercent(-1, 100)).toBeNull();
  });
});

describe("backfillStateLabel", () => {
  it("distinguishes the three states", () => {
    expect(backfillStateLabel({ cursor: null, completed_at: null })).toBe("시작 전");
    expect(backfillStateLabel({ cursor: { page: 4 }, completed_at: null })).toBe("진행 중");
    expect(backfillStateLabel({ cursor: { page: 4 }, completed_at: "2026-08-18T00:00:00Z" })).toBe("완주");
  });
});
```

- [ ] **Step 2: 실패 확인** — `npx vitest run tests/backfill-progress.test.ts` FAIL
- [ ] **Step 3: 구현** — `delivery/fe/src/lib/backfill-progress.ts`:

```ts
export type ProgressRow = Readonly<{
  cursor: Record<string, number> | null;
  completed_at: string | null;
}>;

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;

export const formatBackfillPercent = (
  itemsDone: number,
  totalEstimate: number | null | undefined,
): string | null => {
  if (!isCount(itemsDone) || !isCount(totalEstimate ?? NaN) || !totalEstimate) return null;
  const ratio = Math.min(itemsDone / totalEstimate, 1);
  if (ratio === 1) return "100%";
  return `${(ratio * 100).toFixed(1)}%`;
};

export const backfillStateLabel = (row: ProgressRow): string => {
  if (row.completed_at) return "완주";
  if (row.cursor) return "진행 중";
  return "시작 전";
};
```

프록시 라우트·페이지·내비는 각각 기존 파일(`api/jobs/route.ts`, 서버 데이터 페이지 하나, 내비 컴포넌트)을 읽고 같은 구조로 작성한다 — 페이지는 `GET /progress` 를 서버에서 불러 표 렌더: 사이트 / 진행률(`formatBackfillPercent`) / 상태(`backfillStateLabel`) / `items_done`·`docs_in_db`·마지막 갱신 시각(`toLocaleString("ko-KR")`). 스타일은 기존 표 클래스 재사용, 새 CSS 토큰 금지.

- [ ] **Step 4: 전체 게이트** — `cd delivery/fe && npm test && npm run typecheck && npm run lint && npm run build`. Expected: 기존 286 + 신규 전부 통과, 빌드 성공 (`route-rendering.test.ts` 가 새 페이지의 force-dynamic 을 자동 검사)
- [ ] **Step 5: 커밋**

```bash
git add delivery/fe/src/lib/backfill-progress.ts delivery/fe/src/app/api/progress/route.ts \
        'delivery/fe/src/app/(shell)/backfill/page.tsx' <내비 파일> delivery/fe/tests/backfill-progress.test.ts
git commit -m "feat(fe): backfill progress page"
```

---

### Task 10: 문서 + 리허설 확인

**Files:**
- Modify: `docs/DELIVERY_DEPLOYMENT_RUNBOOK_20260813.md` (백필 운영 절), `docs/DELIVERY_READINESS_20260818.md` (규모 한계 절 갱신), `docs/HANDOFF_20260818_SCALE.md` (§4-2 완료 표시)

**Steps:**

- [ ] **Step 1: 파이썬 전체 스위트** — 공통 명령의 전 모듈 + `tests.test_crawler_cursor_contract tests.test_crawler_cursor_conformance`. Expected: 전부 OK
- [ ] **Step 2: 리허설 스택 확인** (가짜 site_id 만; **이미지 재빌드 필수** — `migrate` 포함 `--build`):
  - migrate 재실행 → `crawl_site_progress` 존재 확인
  - `seed_backfill_estimates.py` 를 리허설 DB에 실행 → 25행·doaj=13373055 확인 후 **DELETE 로 원복**
  - `GET /progress` 200 + FE `/backfill` 200
  - 운영 스택 무사 확인 (읽기 전용 2개 명령: 컨테이너 3개, documents 536056+)
- [ ] **Step 3: 런북에 「백필 운영」 절 추가** — 시작(`POST /jobs {"site_id": ..., "mode": "backfill"}`), 자동 재큐잉과 멈추는 조건(완주·무전진·취소), 진행 확인(`/backfill` 화면), 시드 스크립트 실행법, `LIBERTREE_MAX_WALL_S` 와의 관계(조각 크기). READINESS 의 "대형 사이트 수집 불가" 서술을 "백필 모드로 해소(2026-08-18)" 로 갱신, HANDOFF §4-2 에 완료 표시.
- [ ] **Step 4: 커밋**

```bash
git add docs/DELIVERY_DEPLOYMENT_RUNBOOK_20260813.md docs/DELIVERY_READINESS_20260818.md docs/HANDOFF_20260818_SCALE.md
git commit -m "docs(delivery): backfill operations and close the resume gap"
```

---

## 이 계획에 없는 것 (의도적)

- 작업 분할(조각 크기 파라미터화) — `LIBERTREE_MAX_WALL_S` 가 조각 크기를 이미 결정한다
- 스케줄의 backfill 모드 — 자동 재큐잉과 이중 등록 충돌
- 실사이트 실물 검증 — 제3자 크롤 금지. 납품 후 doaj 첫 조각이 실검증이 된다
- arbitrary 사이트의 증분 최적화 — 순서 보장이 없어 원리적으로 불가

## Self-Review

**1. 스펙 커버리지** — 스펙 1절(테이블)→T1·T8, 2절(워커)→T4, 3절(계약)→T3, 4절(패치 25개: 6+7+12=25)→T5·6·7, 5절(증분)→T3(임계)·T4(done 처리)·T5(oldest_first 커서)·T6/7(newest_first 선언), 6절(진도)→T8·T9, 7절(검증)→각 태스크 테스트+T10. 누락 없음.
**2. 플레이스홀더** — T4 의 `...기존 그대로...` 는 "변경하지 말라"는 지시이고, T4 테스트의 `...` 는 기존 취소 픽스처 재사용 지시로 대상 파일·방식을 명시함. 그 외 코드 블록 전부 실코드.
**3. 타입 일관성** — `load_cursor/save_progress/mark_backfill_complete` (T2 정의 = T4 호출), `_advance_cursor(cursor, items_done=0)` (T3 = T5~7 호출), `CrawlUpToDate` (T3 = T4 import), `formatBackfillPercent/backfillStateLabel` (T9 정의 = 페이지 사용), `GET /progress` 응답 키 (T8 = T9 소비). conformance 의 `TARGETS` 25개 = 스펙 4절 표의 25개와 일치(6 oldest + 10 newest + 9 arbitrary).
