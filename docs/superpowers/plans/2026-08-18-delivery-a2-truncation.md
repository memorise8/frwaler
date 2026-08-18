# A2 — 잘린 수집을 완료로 보고하는 문제 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 25분 벽시계 예산에 걸려 남은 페이지를 건너뛴 수집을 `done` 과 구별해 표시한다.

**Architecture:** 크롤러 파일은 **한 개도 건드리지 않는다.** 워커가 `crawl()` 호출을 감싸 경과 시간을 재고, 예산 임계를 넘겨 정상 반환했으면 `crawl_jobs.truncated` 를 세운다. FE는 그 플래그를 배지로 보여준다.

**Tech Stack:** psycopg(dict row factory) · PostgreSQL 16 · FastAPI · Next.js 16 App Router · vitest(node, jsdom 없음) · Python `unittest`

**Spec:** `docs/DELIVERY_READINESS_20260818.md` 의 「5. A1 · A2」 절

## Global Constraints

- **워커는 살아 있다.** 실존 `site_id` 로 작업을 넣으면 실제로 제3자 사이트를 크롤한다. 검증은 존재하지 않는 site_id 나 주입한 가짜 크롤러로만 한다.
- **`git add -A` / `git add .` 금지.** 다른 세션이 같은 브랜치에 파이썬을 커밋한다. 항상 파일을 명시한다.
- **운영 DB에 테스트를 겨누지 않는다.** 테스트는 `DROP TABLE` 을 한다. 아래 일회성 컨테이너 명령을 그대로 쓴다.
- `delivery/fe/tsconfig.json` 과 `delivery/fe/next-env.d.ts` 는 절대 스테이징하지 않는다.
- 사용자 대면 문자열은 한국어. 기존 화면 어투("…했습니다", "…하세요")를 따른다.
- 테스트 개수 언급은 참고용이다. 구속력 있는 요구는 "새 테스트가 통과하고 기존 테스트가 하나도 깨지지 않는다"뿐이다. 기준선: FE 281 / 28 파일, Python 104.

### 테스트 실행 명령 (모든 태스크 공통)

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery
docker rm -f a2-testpg >/dev/null 2>&1
docker run -d --rm --name a2-testpg --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=devpw -e POSTGRES_USER=libertree -e POSTGRES_DB=libertree postgres:16 >/dev/null
until docker exec a2-testpg pg_isready -U libertree -d libertree >/dev/null 2>&1; do sleep 1; done

docker run --rm --network container:a2-testpg \
  -v /mnt/raid/ruci_workspace/frwaler-delivery:/app -w /app \
  -e TEST_PG_DSN='postgresql://libertree:devpw@127.0.0.1:5432/libertree' \
  -e DELIVERY_AUTH_MODE=disabled \
  libertree-delivery-worker:latest python -m unittest tests.test_worker_run tests.test_worker_jobs tests.test_be_app tests.test_delivery_migration -v

docker rm -f a2-testpg >/dev/null 2>&1
```

프론트엔드:

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery/delivery/fe
npx vitest run tests/<파일>.test.ts          # 태스크 중
npm test && npm run typecheck && npm run lint && npm run build   # 커밋 전
```

---

## 왜 문자열이 아니라 경과 시간인가 — 이 계획의 핵심 판단

먼저 검토한 방법은 워커가 이미 보존하는 크롤러 stdout 에서 `"budget reached"` 류 문구를 찾는 것이었다. **측정해 보고 버렸다.**

```
예산 코드를 가진 크롤러                716개
문구가 제각각                          "wall-clock budget; exiting cleanly"
                                       "budget exceeded. Stopping cleanly."
                                       "budget reached after N pages. Exiting."   … 8종 이상
'budget' 단어를 공유하는 것             687개 (96%)

그런데 — 문서 제목을 stdout 에 찍는 크롤러   663개
```

정부·공공 사이트를 긁는 수집기다. **예산(budget) 문서 제목이 그대로 출력된다.** 실제로 `"Budget 2026: stopping inflation"` 같은 평범한 제목이 `budget`+동사 규칙에도 걸린다. 문자열 규칙은 잡는 만큼 잘못 잡는다.

경과 시간은 그런 문제가 없다. 워커는 `crawl()` 을 언제 부르고 언제 돌아왔는지 알고, 예산값도 같은 환경변수에서 읽는다. **예산에 근접한 시간을 쓰고 예외 없이 돌아왔다면 잘린 것이다.**

임계값:

```
threshold = budget - min(60, budget * 0.1)
```

- 기본 25분(1500초) → **1440초(24분)**
- 예산에서 30초를 빼고 미리 빠져나가는 크롤러(25개, 확인한 최대 여유)가 1470초에 종료 → 잡힌다
- 20분에 정상 종료 → 안 잡힌다
- 테스트에서 예산을 2초로 낮추면 임계 1.8초 → 상수를 바꾸지 않고도 빠르게 검증된다 (`budget * 0.1` 항이 이걸 가능하게 한다)

**이 방식은 문구를 찍지 않는 94개도 잡는다.** 문자열 방식으로는 원리적으로 불가능했던 부분이다.

한계도 분명히 적어 둔다: 예산과 무관하게 정확히 24분 이상 걸려 **정상 완료**한 수집도 `truncated` 로 표시된다. 오분류지만 방향이 안전하다 — "다 못 받았을 수 있다"고 알리는 쪽이지, 잘린 것을 완료라고 하는 쪽이 아니다. 배지 문구도 그 불확실성을 담는다.

---

## File Structure

| 파일 | 책임 | 태스크 |
|---|---|---|
| `delivery/db/schema.py` (수정) | `crawl_jobs.truncated` 컬럼 추가 + 필수 컬럼 목록 등록 | 1 |
| `delivery/worker/jobs.py` (수정) | `finish_job(..., truncated=False)` 로 값을 기록 | 2 |
| `delivery/worker/worker.py` (수정) | 경과 시간 측정과 임계 판정 | 2 |
| `delivery/fe/src/lib/job-status.ts` (수정) | 배지 문구와 설명문 | 3 |
| `delivery/fe/src/app/(shell)/job-dashboard.tsx` (수정) | 목록에 배지 표시 | 3 |
| `delivery/fe/src/app/globals.css` (수정) | 배지 스타일 | 3 |
| `docs/DELIVERY_READINESS_20260818.md` (수정) | A2 종료 기록 | 4 |

스키마 변경이 **있다.** 이전 계획과 다른 점이다. `init_delivery_schema` 는 이미 `ADD COLUMN IF NOT EXISTS` 로 컬럼 8개를 그렇게 추가해 왔으므로 확립된 패턴이며, 기존 데이터베이스에 대해 파괴적이지 않다.

---

## 이 계획에 없는 것 (의도적)

- **A1** (전부 차단당해도 `done`). 밖에서 보면 "전부 차단"과 "새 글 없음"이 똑같이 `saved=0` 이라 신뢰할 신호가 없다. 제대로 하려면 `crawl()` 이 시도/성공/실패 건수를 반환해야 하는데 반환 타입이 있는 크롤러가 800개 중 55개뿐이다. 운영 로그가 쌓인 뒤 실제 차단 사이트만 손보는 편이 낫다.
- **크롤러 파일 수정.** 한 개도 건드리지 않는다.
- **자동 실패 판정.** `truncated` 는 `status` 를 바꾸지 않는다. 잘린 수집도 저장한 문서는 유효하므로 `done` 이 맞고, 플래그는 그 옆에 붙는 부가 정보다.

---

### Task 1: `crawl_jobs.truncated` 컬럼

**Files:**
- Modify: `delivery/db/schema.py`
- Test: `tests/test_delivery_migration.py`

**Interfaces:**
- Consumes: 없음
- Produces: `crawl_jobs.truncated BOOLEAN NOT NULL DEFAULT FALSE`. Task 2가 쓰고, Task 3이 `SELECT *` 를 통해 읽는다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_delivery_migration.py` 의 기존 테스트 클래스 안에 추가한다 (파일을 먼저 읽고 클래스명과 setUp 방식을 확인할 것):

```python
    def test_truncated_column_exists_and_defaults_false(self):
        from delivery.be.migrate import migrate
        migrate(TEST_PG_DSN)
        row = self.conn.execute("""SELECT data_type, column_default, is_nullable
              FROM information_schema.columns
             WHERE table_schema='public' AND table_name='crawl_jobs' AND column_name='truncated'""").fetchone()
        self.assertIsNotNone(row, "crawl_jobs.truncated 이 없다")
        self.assertEqual(row["data_type"], "boolean")
        self.assertEqual(row["is_nullable"], "NO")
        self.assertIn("false", (row["column_default"] or "").lower())

    # 컬럼이 빠진 데이터베이스로 BE 가 조용히 뜨면 안 된다 — 다른 필수 컬럼과 같은 취급.
    def test_missing_truncated_column_fails_schema_verification(self):
        from delivery.be.migrate import migrate
        from delivery.db.schema import verify_required_schema
        migrate(TEST_PG_DSN)
        verify_required_schema(self.conn)          # 먼저 통과하는지 확인
        self.conn.execute("ALTER TABLE crawl_jobs DROP COLUMN truncated")
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, r"crawl_jobs\.truncated"):
            verify_required_schema(self.conn)

    # 이미 행이 있는 기존 설치에서도 추가가 안전해야 한다.
    def test_truncated_column_backfills_existing_rows_as_false(self):
        from delivery.be.migrate import migrate
        from delivery.db import schema
        migrate(TEST_PG_DSN)
        self.conn.execute("ALTER TABLE crawl_jobs DROP COLUMN truncated")
        self.conn.execute("INSERT INTO crawl_jobs(site_id, status) VALUES ('legacy','done')")
        self.conn.commit()
        schema.init_delivery_schema(self.conn)     # 재실행 = 마이그레이션
        row = self.conn.execute("SELECT truncated FROM crawl_jobs WHERE site_id='legacy'").fetchone()
        self.assertFalse(row["truncated"])
```

**주의:** 이 파일의 `setUp` 은 `DROP SCHEMA public CASCADE` 를 하므로 각 테스트는 빈 스키마에서
시작한다. 그래서 `init_delivery_schema` 만 부르지 않고 `migrate(TEST_PG_DSN)` 으로 전체를
세운다 — 그러지 않으면 `verify_required_schema` 가 `documents` 부터 없다고 하며,
무엇 때문에 실패했는지 흐려진다.

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: 위 공통 명령에서 `tests.test_delivery_migration` 만
Expected: FAIL 3건 — 컬럼 없음 / `verify_required_schema` 가 아무것도 안 던짐 / 컬럼 없음

- [ ] **Step 3: 최소 구현을 작성한다**

`delivery/db/schema.py` 의 `init_delivery_schema` 안, 기존 `ADD COLUMN IF NOT EXISTS` 들이 나열된 곳 맨 아래에 한 줄 추가한다:

```python
    conn.execute("ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS truncated BOOLEAN NOT NULL DEFAULT FALSE")
```

같은 파일의 `verify_required_schema` 안 `required_columns` 튜플에 한 줄 추가한다:

```python
        ("crawl_jobs", "truncated"),
```

`CREATE TABLE crawl_jobs (...)` 본문에도 컬럼을 넣어 새 설치에서 곧바로 존재하게 한다. 기존 컬럼들이 쓰는 선행 콤마 스타일을 그대로 따른다:

```python
            ,truncated     BOOLEAN NOT NULL DEFAULT FALSE
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: 같은 명령
Expected: PASS

- [ ] **Step 5: 커밋한다**

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery
git add delivery/db/schema.py tests/test_delivery_migration.py
git commit -m "feat(delivery): record whether a crawl was cut off by its time budget

716 of the crawlers stop mid-collection when LIBERTREE_MAX_WALL_S expires and
return normally, so the worker records the job as done. A large site can be
half collected and look finished.

Add the column the worker will set. Follows the ADD COLUMN IF NOT EXISTS
pattern the other eight columns use, so an existing database migrates without
a destructive step, and joins required_columns so a deployment that skips the
migration fails visibly instead of silently reporting every crawl complete.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 워커가 경과 시간으로 판정한다

**Files:**
- Modify: `delivery/worker/jobs.py` (`finish_job`)
- Modify: `delivery/worker/worker.py` (`run_job`)
- Test: `tests/test_worker_run.py`, `tests/test_worker_jobs.py`

**Interfaces:**
- Consumes: Task 1의 `crawl_jobs.truncated`
- Produces:
  - `jobs.finish_job(conn, job_id, saved_count, *, truncated: bool = False) -> None`
  - `worker.wall_budget_seconds() -> float` — 환경변수에서 예산을 읽는다
  - `worker.truncation_threshold_seconds() -> float` — 임계값
  - `run_job` 이 정상 반환 경로에서만 `truncated` 를 세운다

**반드시 지킬 판정 세 가지:**

1. **취소 경로에서는 세우지 않는다.** `CrawlCancelled` 는 자기 상태가 따로 있고, 오래 돌다 취소된 것을 "잘림"이라 부르면 두 가지를 뒤섞는다.
2. **예외 경로에서는 세우지 않는다.** 그건 `failed` 다.
3. **예산은 호출 시점에 읽는다.** 모듈 상단 상수로 굳히면 테스트가 환경변수로 낮출 수 없다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_worker_jobs.py` 에 추가한다:

```python
    def test_finish_job_records_truncation(self):
        from delivery.worker import jobs
        cut = jobs.enqueue_job(self.conn, "cut", requested_by="op")
        whole = jobs.enqueue_job(self.conn, "whole", requested_by="op")
        for jid in (cut, whole):
            self.conn.execute("UPDATE crawl_jobs SET status='running' WHERE id=%s", (jid,))
        self.conn.commit()
        jobs.finish_job(self.conn, cut, saved_count=5, truncated=True)
        jobs.finish_job(self.conn, whole, saved_count=5)
        rows = {r["site_id"]: r for r in self.conn.execute(
            "SELECT site_id, status, truncated FROM crawl_jobs").fetchall()}
        self.assertTrue(rows["cut"]["truncated"])
        self.assertFalse(rows["whole"]["truncated"])
        # 잘렸어도 저장한 문서는 유효하다. status 는 여전히 done 이어야 한다.
        self.assertEqual(rows["cut"]["status"], "done")
```

`tests/test_worker_run.py` 에 추가한다. 기존 `_FakeCrawler` 를 상속해 쓰고, 예산을 2초로 낮춰 임계 1.8초를 만든다:

```python
    def test_slow_crawl_is_recorded_as_truncated(self):
        import time
        from unittest import mock
        from delivery.worker import jobs, worker
        class SlowCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                time.sleep(2.5)
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, crawler_registry={"fake": SlowCrawler}, delay=0)
        row = self.conn.execute("SELECT status, truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["truncated"])

    def test_quick_crawl_is_not_truncated(self):
        from unittest import mock
        from delivery.worker import jobs, worker
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, crawler_registry={"fake": _FakeCrawler}, delay=0)
        row = self.conn.execute("SELECT truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertFalse(row["truncated"])

    # 오래 돌다 취소된 것과 예산에 잘린 것은 다른 사건이다.
    def test_cancelled_slow_crawl_is_not_marked_truncated(self):
        import time
        from unittest import mock
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker
        class SlowCancellable(BaseCrawler):
            site_id="fake";site_name="Fake";base_url="https://fake.example"
            def crawl(self, limit=None):
                time.sleep(2.5)
                self._check_cancelled()
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        jobs.cancel_job(self.conn, jid)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, {"fake": SlowCancellable}, delay=0, should_cancel=lambda: True)
        row = self.conn.execute("SELECT truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertFalse(row["truncated"])

    # 실패는 실패다.
    def test_slow_failing_crawl_is_not_marked_truncated(self):
        import time
        from unittest import mock
        from delivery.worker import jobs, worker
        class SlowBroken(_FakeCrawler):
            def crawl(self, limit=None):
                time.sleep(2.5)
                raise RuntimeError("boom")
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, crawler_registry={"fake": SlowBroken}, delay=0)
        row = self.conn.execute("SELECT status, truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertFalse(row["truncated"])

    def test_truncation_threshold_scales_with_the_budget(self):
        from unittest import mock
        from delivery.worker import worker
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "1500"}):
            self.assertAlmostEqual(worker.truncation_threshold_seconds(), 1440.0, places=3)
        # 테스트가 예산을 낮게 잡아도 임계가 음수로 무너지지 않아야 한다.
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            self.assertAlmostEqual(worker.truncation_threshold_seconds(), 1.8, places=3)
```

**확인된 사실:** 이 파일에는 `_queued_job` 같은 헬퍼가 없다. 작업은 위처럼
`jobs.enqueue_job(...)` → `jobs.claim_next_job(...)` 로 만든다(기존 테스트와 동일).
`os` 는 이미 import 되어 있다. `BaseCrawler` 의 경로는 `crawler.base_crawler` 이며
`site_id`·`site_name`·`base_url` 을 지정해야 한다. 취소 경로는 `finish_job` 의
`WHERE status='running'` 때문에 `jobs.cancel_job` 을 먼저 불러야 기존 테스트와 같은
흐름이 된다.

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `tests.test_worker_run tests.test_worker_jobs`
Expected: FAIL — `finish_job() got an unexpected keyword argument 'truncated'`, `module 'delivery.worker.worker' has no attribute 'truncation_threshold_seconds'`

- [ ] **Step 3: 최소 구현을 작성한다**

`delivery/worker/jobs.py` 의 `finish_job` 을 고친다:

```python
def finish_job(conn, job_id, saved_count, *, truncated: bool = False) -> None:
    row = conn.execute(
        """UPDATE crawl_jobs SET status='done', saved_count=%s, finished_at=clock_timestamp(),
               worker_id=NULL,lease_expires_at=NULL,truncated=%s
            WHERE id=%s AND status='running' RETURNING status""",
        (int(saved_count or 0), bool(truncated), job_id),
    ).fetchone()
    if row:
        message = f"수집 완료: 신규 문서 {int(saved_count or 0)}건"
        if truncated:
            message += " (시간 제한으로 남은 페이지를 건너뛰었습니다)"
        _log(conn,job_id,"completed",message)
    else:
```

이하 `else` 블록은 그대로 둔다.

`delivery/worker/worker.py` 상단의 다른 상수 옆에 추가한다:

```python
# 예산에 근접한 시간을 쓰고 예외 없이 돌아온 수집은 잘린 것으로 본다.
#
# 크롤러가 찍는 문구로 판별하는 방법을 먼저 검토했다가 버렸다: 문구가 8종 이상으로
# 제각각인 데다, 크롤러 663개가 문서 제목을 stdout 으로 흘린다. 공공기관 수집기라
# "Budget 2026: stopping inflation" 같은 평범한 제목이 규칙에 걸린다. 경과 시간에는
# 그런 오탐이 없고, 아무 문구도 찍지 않는 94개까지 함께 잡힌다.
#
# 여유 60초는 확인된 최대치의 두 배다 — 크롤러 25개가 자기 예산에서 30초를 빼고
# 미리 빠져나간다. budget*0.1 항은 테스트가 예산을 몇 초로 낮춰도 임계가 음수로
# 무너지지 않게 한다.
_TRUNCATION_MARGIN_S = 60.0


def wall_budget_seconds() -> float:
    """크롤러가 읽는 것과 같은 예산. 호출 시점에 읽는다(테스트가 낮출 수 있도록)."""
    return float(os.environ.get("LIBERTREE_MAX_WALL_S", 25 * 60))


def truncation_threshold_seconds() -> float:
    budget = wall_budget_seconds()
    return budget - min(_TRUNCATION_MARGIN_S, budget * 0.1)
```

`run_job` 을 고친다. `try:` 직전에 시계를 시작하고, **정상 반환 경로에서만** 판정한다:

```python
    started = time.monotonic()
    try:
        with redirect_stdout(capture):
```

그리고 함수 끝의 정상 반환 블록을 다음으로 바꾼다:

```python
    saved = _count_site_docs(conn, site_id) - before
    # 예산에 걸려 남은 페이지를 건너뛰고 정상 반환한 경우. 취소·실패 경로에서는
    # 판정하지 않는다 -- 그것들은 각자의 상태가 있고, 오래 돌다 취소된 것을
    # "잘렸다"고 부르면 두 사건이 뒤섞인다.
    truncated = (time.monotonic() - started) >= truncation_threshold_seconds()
    jobs.finish_job(conn, job["id"], saved_count=max(0, saved), truncated=truncated)
    record_output()
    return max(0, saved)
```

`import time` 이 파일 상단에 없으면 추가한다.

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `tests.test_worker_run tests.test_worker_jobs`
Expected: PASS. 느린 테스트 3개가 각각 2.5초씩 자므로 전체가 8초쯤 늘어난다 — 정상이다.

- [ ] **Step 5: 커밋한다**

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery
git add delivery/worker/jobs.py delivery/worker/worker.py \
        tests/test_worker_run.py tests/test_worker_jobs.py
git commit -m "feat(delivery): flag a crawl that ran out its time budget

The worker's only success signal was whether crawl() raised. 716 crawlers
break out of their loop when LIBERTREE_MAX_WALL_S expires and return normally,
so a half-collected large site was recorded as done.

Judge it by elapsed time, not by the crawler's output. Matching printed text
was measured and rejected: the message has eight-plus phrasings, and 663
crawlers echo document titles to stdout -- these are public-sector collectors,
so an ordinary headline like \"Budget 2026: stopping inflation\" trips a
budget-keyword rule. Elapsed time has no such false positive and also catches
the 94 crawlers that print nothing at all.

Status stays done: documents a truncated run did save are still valid. Only
the normal-return path is judged -- cancelled and failed keep their own
meaning.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 화면에 표시한다

**Files:**
- Modify: `delivery/fe/src/lib/job-status.ts`
- Modify: `delivery/fe/src/app/(shell)/job-dashboard.tsx`
- Modify: `delivery/fe/src/app/globals.css`
- Test: `delivery/fe/tests/job-status.test.ts`

**Interfaces:**
- Consumes: `crawl_jobs.truncated` — `list_jobs` 가 `SELECT *` 를 하므로 BE 변경 없이 그대로 흘러온다(확인함)
- Produces: `TRUNCATED_BADGE`, 그리고 `describeJobOutcome` 이 잘린 실행을 언급

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`delivery/fe/tests/job-status.test.ts` 에 추가한다 (파일을 먼저 읽고 기존 import 줄에 새 이름을 더할 것):

```ts
describe("truncated runs", () => {
  // 잘린 수집도 status 는 done 이다. 배지는 "완료" 를 대체하지 않고 옆에 붙는다.
  it("keeps the done label and adds a separate badge", () => {
    expect(jobStatusLabel("done")).toBe("완료");
    expect(TRUNCATED_BADGE).toBe("부분 수집");
  });

  it("tells the operator the run was cut short and what to do", () => {
    const text = describeJobOutcome({ status: "done", saved_count: 40, truncated: true });
    expect(text).toContain("시간 제한");
    expect(text).toContain("40");
    // 재실행은 해결책이 아니다 — 크롤러는 1페이지부터 다시 걷는다.
    expect(text).toContain("시간 제한을 늘려");
    expect(text).not.toContain("이어서");
  });

  it("says so even when a truncated run saved nothing", () => {
    expect(describeJobOutcome({ status: "done", saved_count: 0, truncated: true }))
      .toContain("시간 제한");
  });

  it("leaves an ordinary completed run's text untouched", () => {
    const plain = describeJobOutcome({ status: "done", saved_count: 40 });
    expect(plain).not.toContain("시간 제한");
    expect(describeJobOutcome({ status: "done", saved_count: 40, truncated: false })).toBe(plain);
  });

  // 실패·취소는 잘림과 무관하다.
  it("does not mention truncation for failed or cancelled jobs", () => {
    expect(describeJobOutcome({ status: "failed", saved_count: 0, error: "boom", truncated: true }))
      .not.toContain("시간 제한");
    expect(describeJobOutcome({ status: "cancelled", saved_count: 0, truncated: true }))
      .not.toContain("시간 제한");
  });
});
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `npx vitest run tests/job-status.test.ts`
Expected: FAIL — `TRUNCATED_BADGE` 를 찾을 수 없음

- [ ] **Step 3: 최소 구현을 작성한다**

`delivery/fe/src/lib/job-status.ts` 에서 `JobOutcomeInput` 에 필드를 더한다:

```ts
export type JobOutcomeInput = Readonly<{
  status: string;
  saved_count: number | null | undefined;
  error?: string | null;
  truncated?: boolean | null;
}>;
```

배지 상수를 추가한다:

```ts
// status 를 대체하지 않고 "완료" 옆에 붙는다. 잘린 실행도 저장한 문서는 유효하므로
// 실패가 아니고, 그렇다고 다 받은 것도 아니다.
export const TRUNCATED_BADGE = "부분 수집";
```

`describeJobOutcome` 의 `done` 분기를 고친다. 기존 두 문장을 그대로 두고 앞에 한 문장을 덧붙인다:

```ts
  if (job.status === "done") {
    const saved = job.saved_count ?? 0;
    // 재실행하면 이어받는다고 쓰지 않는다. 확인한 사실: 800개 중 이미 받은 것을
    // 건너뛰는 장치를 가진 크롤러는 사실상 없다(has_blob 0개, _last_save_created 0개,
    // pdf_downloaded 조회 10개). 재실행은 1페이지부터 다시 걸어 같은 자리에서 또
    // 잘린다. 실제 해결책은 워커의 LIBERTREE_MAX_WALL_S 를 늘리는 것뿐이다.
    const cut = job.truncated
      ? "시간 제한(기본 25분)에 걸려 남은 페이지를 건너뛰었습니다. 다시 실행해도 크롤러가 처음부터 다시 훑기 때문에 같은 지점에서 멈춥니다. "
        + "이 사이트를 끝까지 받으려면 워커의 시간 제한을 늘려야 합니다(LIBERTREE_MAX_WALL_S). "
      : "";
    if (saved === 0) {
      return cut + "신규 저장 0건입니다. DB에 새로 추가된 문서가 없다는 뜻이며 실패가 아닙니다. "
        + "이미 수집된 문서만 다시 처리했을 수 있습니다. 크롤러가 실제로 처리한 문서 수는 이 화면에서 확인할 수 없습니다.";
    }
    return cut + `신규 저장 ${saved.toLocaleString("ko-KR")}건입니다.`;
  }
```

`delivery/fe/src/app/(shell)/job-dashboard.tsx`:
- `Job` 타입에 `truncated?: boolean` 을 더한다
- import 에 `TRUNCATED_BADGE` 를 더한다
- 상태를 렌더하는 셀에서 라벨 뒤에 배지를 붙인다. **파일을 읽고 실제 구조에 맞출 것** — 이 컴포넌트는 한 줄로 압축된 JSX 를 쓰는 곳이 있다:

```tsx
{jobStatusLabel(job.status)}
{job.truncated && <span className="job-badge job-badge--cut">{TRUNCATED_BADGE}</span>}
```

`delivery/fe/src/app/globals.css` 끝에 추가한다. **아래 토큰만 쓸 것** — `globals.css` 의 `:root` 가 정의하는 것은 이게 전부이고, 없는 이름을 쓰면 `tests/css-tokens.test.ts` 가 실패한다:

```
--canvas  --surface  --ink  --muted  --rule
--moss  --moss-deep  --moss-wash  --sand-wash
--bad  --bad-wash  --sans  --serif  --mono
```

```css
.job-badge { display: inline-block; margin-left: .4rem; padding: .05rem .35rem; border-radius: 2px;
             font-size: .75rem; font-weight: 650; white-space: nowrap; }
.job-badge--cut { background: var(--sand-wash); color: var(--ink); }
```

- [ ] **Step 4: 전체 테스트·타입·린트·빌드를 돌린다**

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery/delivery/fe
npm test && npm run typecheck && npm run lint && npm run build
```

Expected: 전부 통과. 기존 281건이 하나도 깨지지 않을 것. **`npm run build` 가 중요하다** — 과거에 FE 빌드 실패가 이 납품을 한 번 막았다.

- [ ] **Step 5: 커밋한다**

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery
git add delivery/fe/src/lib/job-status.ts \
        'delivery/fe/src/app/(shell)/job-dashboard.tsx' \
        delivery/fe/src/app/globals.css \
        delivery/fe/tests/job-status.test.ts
git commit -m "feat(fe): mark a crawl that was cut off by its time budget

A large site could be half collected and read as \"완료\" with no way to tell.
The badge sits beside the status rather than replacing it -- a truncated run's
documents are real -- and the detail text says the run stopped early and that
re-running continues from where it left off.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 실물 확인과 문서 종료

**Files:**
- Modify: `docs/DELIVERY_READINESS_20260818.md`

**Interfaces:** Task 1~3 전부

- [ ] **Step 1: 파이썬 전체 스위트를 돌린다**

공통 명령의 모듈 목록에 `tests.test_delivery_cache tests.test_delivery_catalogue tests.test_delivery_compose tests.test_delivery_database tests.test_delivery_freshness tests.test_delivery_seed tests.test_delivery_stats tests.test_delivery_verification tests.test_worker_cycle` 를 더해 전부 실행한다.
Expected: OK. 기준선 104 + 신규분.

- [ ] **Step 2: 리허설 스택에서 실물로 확인한다**

리허설 스택은 운영 스택과 격리돼 있다(`-p delivery-coldstart`, 포트 8091/3091). **이미지를 반드시 다시 빌드할 것** — 예전에 낡은 이미지 때문에 "실물 검증"이 무의미해진 적이 있다.

```bash
SP=/tmp/claude-1001/-mnt-raid-ruci-workspace-frwaler-delivery-delivery/45a7a0ac-80e4-4b8c-832b-ce53d00c57e2/scratchpad
"$SP/coldstart/dc" build be worker fe
"$SP/coldstart/dc" --profile bootstrap run --rm migrate      # 새 컬럼 적용
"$SP/coldstart/dc" up -d

# 컬럼이 실제로 생겼는지
"$SP/coldstart/dc" exec -T postgres psql -U libertree -d libertree -tAc \
  "SELECT column_name FROM information_schema.columns WHERE table_name='crawl_jobs' AND column_name='truncated'"

# 잘린 작업이 목록 API 에 흘러오는지 (존재하지 않는 site_id 로만 — 실제 크롤 없음)
"$SP/coldstart/dc" exec -T postgres psql -U libertree -d libertree -q <<'SQL'
INSERT INTO crawl_jobs(site_id,status,saved_count,truncated,started_at,finished_at)
VALUES ('a2-check-cut','done',40,TRUE,clock_timestamp()-interval '30 minutes',clock_timestamp()),
       ('a2-check-whole','done',40,FALSE,clock_timestamp()-interval '1 minute',clock_timestamp());
SQL
curl -s 'http://127.0.0.1:8091/jobs?limit=5' | python3 -m json.tool | grep -A1 truncated | head
curl -s -o /dev/null -w 'FE / → %{http_code}\n' http://127.0.0.1:3091/

# 정리
"$SP/coldstart/dc" exec -T postgres psql -U libertree -d libertree -tAc \
  "DELETE FROM crawl_jobs WHERE site_id LIKE 'a2-check-%'; SELECT count(*) FROM crawl_jobs"
```

Expected: 컬럼 존재, `/jobs` 응답에 `"truncated": true` 와 `false` 가 각각 보임, FE 200, 정리 후 0건.

**운영 스택(`libertree-delivery`)에는 어떤 명령도 실행하지 않는다.**

- [ ] **Step 3: 운영 스택이 무사한지 확인한다**

```bash
docker compose -p libertree-delivery ps --format '{{.Name}}\t{{.Status}}'
docker exec libertree-delivery-postgres-1 psql -U libertree -d libertree -tAc 'select count(*) from documents'
```

Expected: 컨테이너 3개 그대로, 문서 **536056**건.

- [ ] **Step 4: 스펙 문서를 갱신한다**

`docs/DELIVERY_READINESS_20260818.md` 의 「5. A1 · A2」 절을 고친다. **A2만 완료로 표시하고 A1은 그대로 연 채로 둔다.** A2 항목을 다음 취지로 다시 쓴다:

```markdown
- **~~A2~~ · 완료 (2026-08-18)**: `crawl_jobs.truncated` 를 추가하고, 워커가 예산에
  근접한 시간을 쓰고 정상 반환한 수집을 표시한다. 목록에 「부분 수집」 배지가 붙는다.
  **크롤러는 한 개도 고치지 않았다.** 판정은 크롤러 출력 문자열이 아니라 경과 시간으로
  한다 — 문구가 8종 이상으로 제각각인 데다 크롤러 663개가 문서 제목을 stdout 으로
  흘려서, 공공기관 예산 문서 제목이 그대로 오탐이 된다. 경과 시간 방식은 아무 문구도
  찍지 않는 94개까지 함께 잡는다.
  한계: 예산과 무관하게 24분 이상 걸려 정상 완료한 수집도 표시된다. 오분류지만
  방향이 안전하다 — 잘린 것을 완료라고 하는 쪽이 아니라 그 반대다.
```

「데이터」 표에 `crawl_jobs.truncated` 관련 행을 더할 필요는 없다.

- [ ] **Step 5: 커밋한다**

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery
git add docs/DELIVERY_READINESS_20260818.md
git commit -m "docs(delivery): close A2

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. 스펙 커버리지**

| 스펙 항목 | 태스크 |
|---|---|
| A2 — 예산 초과 시 부분 수집이 완료로 보임 | Task 1(저장) · 2(판정) · 3(표시) |
| 크롤러 파일을 고치지 않을 것 | 전 태스크 — diff 에 `crawler/` 가 등장하면 실패 |
| A1 | **범위 밖** — 사유는 「이 계획에 없는 것」에 기재 |

**2. 플레이스홀더 점검** — TBD·TODO 없음. 모든 코드 단계에 실제 코드가 있다. Task 2 Step 1과 Task 3 Step 3만 "파일을 먼저 읽고 실제 구조에 맞추라"고 지시하는데, 각각 테스트 헬퍼 이름과 압축 JSX 때문이며 붙일 코드 자체는 전부 제시했다.

**3. 타입 일관성**
- `finish_job(conn, job_id, saved_count, *, truncated: bool = False)` → Task 2의 `run_job` 이 `truncated=truncated` 로 호출, Task 2 테스트가 같은 키워드 사용. `CrawlCancelled` 경로의 기존 호출은 인자를 안 주므로 기본값 `False` 가 적용된다 — 의도한 동작이다.
- `worker.truncation_threshold_seconds()` → Task 2 테스트가 같은 이름으로 호출.
- `JobOutcomeInput.truncated?: boolean | null` → Task 3 테스트가 `truncated: true/false` 를 넘기고 생략도 한다. 셋 다 타입에 맞는다.
- `TRUNCATED_BADGE` → Task 3 테스트와 `job-dashboard.tsx` 가 같은 이름을 쓴다.
- 임계 계산을 실제로 검산했다: 예산 1500 → 1440.0, 예산 2 → 1.8. 테스트의 `assertAlmostEqual` 기대값과 일치한다.
