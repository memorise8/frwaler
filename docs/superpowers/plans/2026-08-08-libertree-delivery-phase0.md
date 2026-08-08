# Libertree 납품 시스템 — Phase 0 (기반) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 크롤러가 Postgres에 쓰고, 최소 BE가 그 문서를 반환하며, 워커가 작업 큐에서 크롤을 집어 실행하는 end-to-end 기반을 만든다(빈 시작).

**Architecture:** 기존 크롤러 코드를 수정 없이 재사용하도록 `db_libertree.py`와 동일 시그니처의 `db_pg.py`(Postgres)를 만들고, 환경변수 `LIBERTREE_DB_BACKEND`(기본 `sqlite`)로 백엔드를 전환한다. 워커는 `crawl_jobs` 테이블을 `FOR UPDATE SKIP LOCKED`로 소비하며, 최소 FastAPI BE가 `/health`·문서 조회·작업 enqueue를 제공한다. `docker compose`로 postgres+be+worker를 기동한다.

**Tech Stack:** Python 3.12, psycopg 3, FastAPI, uvicorn, Postgres 16, Docker Compose. 테스트는 프로젝트 컨벤션인 표준 `unittest`.

## Global Constraints

- 프로젝트 인터프리터는 `.venv/bin/python` (Python 3.12). 모든 python/pip 명령은 이 경로로 실행.
- 테스트는 `unittest`(기존 `tests/*.py` 컨벤션). 실행: `.venv/bin/python -m unittest tests.<module> -v`. pytest 도입 금지.
- `crawler` 임포트는 리포지토리 루트(`/data_raid/ruci_workspace/frwaler_job`)를 `sys.path`에 넣어야 함. 테스트 상단에 기존 패턴(`sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))`) 사용.
- `LIBERTREE_DB_BACKEND` 미설정/`sqlite` 시 **기존 SQLite 동작·crawlers-share 공유 패키지에 어떠한 회귀도 없어야 함**. 기본값은 반드시 `"sqlite"`.
- Postgres DSN 환경변수명은 `LIBERTREE_PG_DSN`. 테스트용은 `TEST_PG_DSN`(미설정 시 PG 테스트 skip).
- 블롭(PDF) 저장은 파일시스템 유지, 루트는 `LIBERTREE_BLOB_ROOT`.
- 비밀 무유출: 실제 키/비밀번호는 커밋 금지. `.env.example`은 값 없이 키 이름만.
- dedup 키는 `(site_id, post_number, meta_url)` UNIQUE. Postgres도 SQLite와 동일하게 NULL을 서로 구별하는 시맨틱을 유지.
- 신규 납품 코드는 `delivery/` 아래. 크롤러 DB 포트는 `crawler/` 아래.

---

## 파일 구조 (Phase 0에서 생성/수정)

- `crawler/db_pg.py` (생성) — `db_libertree.py`의 Postgres 미러(동일 공개 함수 시그니처).
- `crawler/db_backend.py` (생성) — `LIBERTREE_DB_BACKEND`로 `db_libertree`/`db_pg` 모듈 선택.
- `crawler/base_crawler.py` (수정) — `_save_paper_v2`가 백엔드 셀렉터를 쓰도록, `_save_paper`가 backend=postgres일 때 v2 경로로 라우팅.
- `delivery/db/schema.py` (생성) — 납품 전용 테이블(`crawl_jobs`) 생성 함수.
- `delivery/worker/jobs.py` (생성) — `claim_next_job`(SKIP LOCKED), `finish_job`, `fail_job`.
- `delivery/worker/worker.py` (생성) — `run_job`(claim→crawl→update) + `--once` 루프.
- `delivery/be/app.py` (생성) — 최소 FastAPI(`/health`, `GET /documents/{seq_id}`, `POST /jobs`, `GET /jobs/{id}`).
- `delivery/requirements.txt` (생성) — psycopg[binary], fastapi, uvicorn.
- `delivery/scripts/test_pg.sh` (생성) — 테스트용 일회성 Postgres 컨테이너 기동/종료.
- `delivery/Dockerfile.be`, `delivery/Dockerfile.worker` (생성).
- `delivery/docker-compose.yml` (생성) — postgres+be+worker.
- `delivery/.env.example` (생성) — 키 이름만.
- `tests/test_db_pg.py`, `tests/test_db_backend.py`, `tests/test_base_crawler_backend.py`, `tests/test_worker_jobs.py`, `tests/test_be_app.py` (생성).

> **범위 조정 메모:** 스펙의 "4컨테이너 골격" 중 `fe`는 Phase 1(카탈로그 API + FE 데이터층 교체)에서 compose에 합류시킨다. Phase 0 compose는 postgres+be+worker 3개로, end-to-end(작업 enqueue→워커 수집→문서 조회)를 검증한다.

---

## Task 1: `crawler/db_pg.py` — Postgres 포트 (스키마·insert/dedup·조회)

**Files:**
- Create: `crawler/db_pg.py`
- Create: `delivery/requirements.txt`
- Create: `delivery/scripts/test_pg.sh`
- Test: `tests/test_db_pg.py`

**Interfaces:**
- Produces: `open_db(dsn: str) -> psycopg.Connection`, `init_db(conn) -> None`, `upsert_site(conn, site_id, site_name, site_url, sheet=None) -> None`, `insert_document(conn, doc: dict) -> int`, `update_document_pdf(conn, seq_id, *, downloaded, size_bytes, sha256) -> None`, `update_document_text(conn, seq_id, *, extracted) -> None`, `update_document_summary(conn, seq_id, summary, model) -> None`, `get_max_post_number(conn, site_id) -> str | None`, `get_last_meta_url(conn, site_id) -> str | None`, `find_by_dedup_key(conn, site_id, post_number, meta_url) -> int | None`, `get_document(conn, seq_id) -> dict | None`. 시그니처는 `crawler/db_libertree.py`와 동일.

- [ ] **Step 1: 의존성 파일 + 테스트 PG 헬퍼 작성 후 설치**

`delivery/requirements.txt`:
```
psycopg[binary]==3.2.*
fastapi==0.115.*
uvicorn[standard]==0.32.*
```

`delivery/scripts/test_pg.sh`:
```sh
#!/bin/sh
# 일회성 테스트 Postgres. 사용:  . delivery/scripts/test_pg.sh up   /   . delivery/scripts/test_pg.sh down
set -e
NAME=libertree-test-pg
case "${1:-up}" in
  up)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --rm --name "$NAME" \
      -e POSTGRES_PASSWORD=test -e POSTGRES_DB=libertree \
      -p 55432:5432 postgres:16 >/dev/null
    printf 'waiting for postgres'
    for i in $(seq 1 30); do
      if docker exec "$NAME" pg_isready -U postgres -d libertree >/dev/null 2>&1; then
        echo " ready"; break; fi
      printf '.'; sleep 1
    done
    echo "export TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/libertree"
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "stopped $NAME"
    ;;
esac
```

Run:
```bash
chmod +x delivery/scripts/test_pg.sh
.venv/bin/pip install "psycopg[binary]==3.2.*"
```
Expected: psycopg 설치 성공.

- [ ] **Step 2: 실패하는 테스트 작성**

`tests/test_db_pg.py`:
```python
# -*- coding: utf-8 -*-
"""crawler.db_pg — Postgres 포트 단위 테스트 (TEST_PG_DSN 필요)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN (delivery/scripts/test_pg.sh up) to run")
class DbPgTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        self.db_pg = db_pg
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP TABLE IF EXISTS documents CASCADE")
        self.conn.execute("DROP TABLE IF EXISTS sites CASCADE")
        self.conn.commit()
        db_pg.init_db(self.conn)
        db_pg.upsert_site(self.conn, "s1", "Site One", "https://s1.example")

    def tearDown(self):
        self.conn.close()

    def _doc(self, **over):
        d = {"site_id": "s1", "post_number": "100", "meta_url": "https://s1/a",
             "title": "Doc A"}
        d.update(over)
        return d

    def test_insert_returns_seq_id(self):
        seq = self.db_pg.insert_document(self.conn, self._doc())
        self.assertIsInstance(seq, int)
        self.assertGreaterEqual(seq, 1)

    def test_dedup_returns_same_seq_id(self):
        a = self.db_pg.insert_document(self.conn, self._doc())
        b = self.db_pg.insert_document(self.conn, self._doc())
        self.assertEqual(a, b)

    def test_distinct_meta_url_new_row(self):
        a = self.db_pg.insert_document(self.conn, self._doc())
        b = self.db_pg.insert_document(self.conn, self._doc(meta_url="https://s1/b"))
        self.assertNotEqual(a, b)

    def test_find_by_dedup_key(self):
        seq = self.db_pg.insert_document(self.conn, self._doc())
        found = self.db_pg.find_by_dedup_key(self.conn, "s1", "100", "https://s1/a")
        self.assertEqual(found, seq)
        self.assertIsNone(self.db_pg.find_by_dedup_key(self.conn, "s1", "999", "https://s1/z"))

    def test_get_max_post_number(self):
        self.db_pg.insert_document(self.conn, self._doc(post_number="100", meta_url="https://s1/a"))
        self.db_pg.insert_document(self.conn, self._doc(post_number="205", meta_url="https://s1/b"))
        self.assertEqual(self.db_pg.get_max_post_number(self.conn, "s1"), "205")

    def test_get_document_roundtrip(self):
        seq = self.db_pg.insert_document(self.conn, self._doc(title="Hello"))
        row = self.db_pg.get_document(self.conn, seq)
        self.assertEqual(row["title"], "Hello")
        self.assertEqual(row["site_id"], "s1")

    def test_null_post_number_not_deduped_by_unique(self):
        a = self.db_pg.insert_document(self.conn, self._doc(post_number=None, meta_url="https://s1/n1"))
        b = self.db_pg.insert_document(self.conn, self._doc(post_number=None, meta_url="https://s1/n1"))
        # find_by_dedup_key(None) 은 기존 행을 찾아 같은 seq 반환해야 함(동작 보존)
        self.assertEqual(a, b)

    def test_update_document_pdf(self):
        seq = self.db_pg.insert_document(self.conn, self._doc())
        self.db_pg.update_document_pdf(self.conn, seq, downloaded=True, size_bytes=123, sha256="abc")
        row = self.db_pg.get_document(self.conn, seq)
        self.assertEqual(row["pdf_downloaded"], 1)
        self.assertEqual(row["pdf_size_bytes"], 123)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 테스트 실행 → 실패 확인**

Run:
```bash
. delivery/scripts/test_pg.sh up
export TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/libertree
.venv/bin/python -m unittest tests.test_db_pg -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'crawler.db_pg'`.

- [ ] **Step 4: `crawler/db_pg.py` 구현**

```python
# -*- coding: utf-8 -*-
"""libertree Postgres 백엔드 — crawler/db_libertree.py 의 Postgres 미러.

공개 함수 시그니처는 db_libertree 와 동일하다. 크롤러 코드는
crawler/db_backend.py 를 통해 이 모듈 또는 db_libertree 를 선택해 쓴다.
dedup 키: (site_id, post_number, meta_url) UNIQUE.
"""
from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

DOCUMENT_INSERT_FIELDS = (
    "site_id", "post_number", "meta_url",
    "title", "published_date", "listed_date",
    "authors", "publisher", "journal",
    "pdf_url", "keywords", "abstract",
    "original_filename",
    "pdf_downloaded", "text_extracted",
    "pdf_size_bytes", "pdf_sha256",
    "summary", "summary_model", "summary_at",
)


def open_db(dsn: str) -> psycopg.Connection:
    """Open a Postgres connection with dict rows (mirrors sqlite3.Row)."""
    return psycopg.connect(dsn, row_factory=dict_row)


def init_db(conn: psycopg.Connection) -> None:
    """Create tables/indexes if absent (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sites (
            site_id     TEXT PRIMARY KEY,
            site_name   TEXT NOT NULL,
            site_url    TEXT NOT NULL,
            sheet       TEXT,
            created_at  TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            seq_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            collected_at      TIMESTAMPTZ DEFAULT now(),
            site_id           TEXT NOT NULL REFERENCES sites(site_id),
            post_number       TEXT,
            meta_url          TEXT NOT NULL,
            title             TEXT NOT NULL,
            published_date    TEXT,
            listed_date       TEXT,
            authors           TEXT,
            publisher         TEXT,
            journal           TEXT,
            pdf_url           TEXT,
            keywords          TEXT,
            abstract          TEXT,
            original_filename TEXT,
            pdf_downloaded    INTEGER DEFAULT 0,
            text_extracted    INTEGER DEFAULT 0,
            pdf_size_bytes    BIGINT,
            pdf_sha256        TEXT,
            summary           TEXT,
            summary_model     TEXT,
            summary_at        TIMESTAMPTZ
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_site_post ON documents(site_id, post_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_meta_url  ON documents(meta_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_collected ON documents(collected_at)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_dedup ON documents(site_id, post_number, meta_url)"
    )
    conn.commit()


def upsert_site(conn, site_id, site_name, site_url, sheet: Optional[str] = None) -> None:
    conn.execute(
        """
        INSERT INTO sites (site_id, site_name, site_url, sheet)
        VALUES (%(site_id)s, %(site_name)s, %(site_url)s, %(sheet)s)
        ON CONFLICT (site_id) DO UPDATE SET
            site_name = EXCLUDED.site_name,
            site_url  = EXCLUDED.site_url,
            sheet     = COALESCE(EXCLUDED.sheet, sites.sheet)
        """,
        {"site_id": site_id, "site_name": site_name, "site_url": site_url, "sheet": sheet},
    )
    conn.commit()


def find_by_dedup_key(conn, site_id, post_number, meta_url) -> Optional[int]:
    if post_number is None:
        row = conn.execute(
            "SELECT seq_id FROM documents WHERE site_id=%s AND post_number IS NULL AND meta_url=%s LIMIT 1",
            (site_id, meta_url),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT seq_id FROM documents WHERE site_id=%s AND post_number=%s AND meta_url=%s LIMIT 1",
            (site_id, post_number, meta_url),
        ).fetchone()
    return None if row is None else row["seq_id"]


def insert_document(conn, doc: dict) -> int:
    if not doc.get("site_id"):
        raise ValueError("insert_document: site_id is required")
    if not doc.get("meta_url"):
        raise ValueError("insert_document: meta_url is required")
    if not doc.get("title"):
        raise ValueError("insert_document: title is required")

    existing = find_by_dedup_key(conn, doc["site_id"], doc.get("post_number"), doc["meta_url"])
    if existing is not None:
        return existing

    payload = {k: doc.get(k) for k in DOCUMENT_INSERT_FIELDS}
    payload["pdf_downloaded"] = int(payload.get("pdf_downloaded") or 0)
    payload["text_extracted"] = int(payload.get("text_extracted") or 0)

    cols = ", ".join(DOCUMENT_INSERT_FIELDS)
    vals = ", ".join("%(" + f + ")s" for f in DOCUMENT_INSERT_FIELDS)
    row = conn.execute(
        f"INSERT INTO documents ({cols}) VALUES ({vals}) RETURNING seq_id",
        payload,
    ).fetchone()
    conn.commit()
    return row["seq_id"]


def update_document_pdf(conn, seq_id, *, downloaded, size_bytes, sha256) -> None:
    conn.execute(
        "UPDATE documents SET pdf_downloaded=%s, pdf_size_bytes=%s, pdf_sha256=%s WHERE seq_id=%s",
        (1 if downloaded else 0, int(size_bytes or 0), sha256 or None, seq_id),
    )
    conn.commit()


def update_document_text(conn, seq_id, *, extracted) -> None:
    conn.execute(
        "UPDATE documents SET text_extracted=%s WHERE seq_id=%s",
        (1 if extracted else 0, seq_id),
    )
    conn.commit()


def update_document_summary(conn, seq_id, summary, model) -> None:
    conn.execute(
        "UPDATE documents SET summary=%s, summary_model=%s, summary_at=now() WHERE seq_id=%s",
        (summary, model, seq_id),
    )
    conn.commit()


def get_max_post_number(conn, site_id) -> Optional[str]:
    row = conn.execute(
        """
        SELECT post_number FROM documents
         WHERE site_id=%s AND post_number IS NOT NULL AND post_number <> ''
         ORDER BY post_number DESC LIMIT 1
        """,
        (site_id,),
    ).fetchone()
    return None if row is None else row["post_number"]


def get_last_meta_url(conn, site_id) -> Optional[str]:
    row = conn.execute(
        "SELECT meta_url FROM documents WHERE site_id=%s ORDER BY collected_at DESC, seq_id DESC LIMIT 1",
        (site_id,),
    ).fetchone()
    return None if row is None else row["meta_url"]


def get_document(conn, seq_id) -> Optional[Any]:
    return conn.execute("SELECT * FROM documents WHERE seq_id=%s", (seq_id,)).fetchone()
```

- [ ] **Step 5: 테스트 실행 → 통과 확인**

Run:
```bash
.venv/bin/python -m unittest tests.test_db_pg -v
```
Expected: PASS (8 tests OK).

- [ ] **Step 6: 커밋**

```bash
git add crawler/db_pg.py delivery/requirements.txt delivery/scripts/test_pg.sh tests/test_db_pg.py
git commit -m "feat(delivery): add db_pg.py — Postgres port of db_libertree

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `crawler/db_backend.py` — 백엔드 셀렉터

**Files:**
- Create: `crawler/db_backend.py`
- Test: `tests/test_db_backend.py`

**Interfaces:**
- Consumes: `crawler.db_libertree`, `crawler.db_pg` (Task 1).
- Produces: `get_backend(name: str | None = None) -> module`. `name` 미지정 시 `os.environ["LIBERTREE_DB_BACKEND"]`(기본 `"sqlite"`)를 사용. `"sqlite"`→`db_libertree`, `"postgres"`→`db_pg`. 그 외 값은 `ValueError`.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_db_backend.py`:
```python
# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class DbBackendTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("LIBERTREE_DB_BACKEND", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LIBERTREE_DB_BACKEND", None)
        else:
            os.environ["LIBERTREE_DB_BACKEND"] = self._saved

    def test_default_is_sqlite(self):
        from crawler import db_backend, db_libertree
        self.assertIs(db_backend.get_backend(), db_libertree)

    def test_env_postgres(self):
        from crawler import db_backend, db_pg
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.assertIs(db_backend.get_backend(), db_pg)

    def test_explicit_name_overrides_env(self):
        from crawler import db_backend, db_libertree
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.assertIs(db_backend.get_backend("sqlite"), db_libertree)

    def test_unknown_raises(self):
        from crawler import db_backend
        with self.assertRaises(ValueError):
            db_backend.get_backend("mysql")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run: `.venv/bin/python -m unittest tests.test_db_backend -v`
Expected: FAIL — `No module named 'crawler.db_backend'`.

- [ ] **Step 3: `crawler/db_backend.py` 구현**

```python
# -*- coding: utf-8 -*-
"""DB 백엔드 셀렉터.

LIBERTREE_DB_BACKEND 환경변수로 SQLite(기본) 또는 Postgres 백엔드를 고른다.
기본값이 'sqlite' 이므로 기존 크롤러 파이프라인과 crawlers-share 공유 패키지는
아무 변경 없이 SQLite 로 계속 동작한다.
"""
from __future__ import annotations

import os


def get_backend(name: str | None = None):
    """Return the db module for the requested backend.

    name is None -> read LIBERTREE_DB_BACKEND (default 'sqlite').
    'sqlite' -> crawler.db_libertree ; 'postgres' -> crawler.db_pg.
    """
    resolved = (name or os.environ.get("LIBERTREE_DB_BACKEND") or "sqlite").lower()
    if resolved == "sqlite":
        from . import db_libertree
        return db_libertree
    if resolved == "postgres":
        from . import db_pg
        return db_pg
    raise ValueError(f"unknown LIBERTREE_DB_BACKEND: {resolved!r}")
```

- [ ] **Step 4: 테스트 실행 → 통과 확인**

Run: `.venv/bin/python -m unittest tests.test_db_backend -v`
Expected: PASS (4 tests OK).

- [ ] **Step 5: 커밋**

```bash
git add crawler/db_backend.py tests/test_db_backend.py
git commit -m "feat(delivery): add db_backend selector (sqlite default, postgres opt-in)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `base_crawler` 백엔드 라우팅 (SQLite 회귀 없음)

**Files:**
- Modify: `crawler/base_crawler.py` (`_save_paper_v2`, `_save_paper`)
- Test: `tests/test_base_crawler_backend.py`

**Interfaces:**
- Consumes: `crawler.db_backend.get_backend` (Task 2), `crawler.db_pg` (Task 1).
- Produces: 변경된 `_save_paper_v2(self, doc)` — `db_backend.get_backend().insert_document(conn, doc)` 사용. 변경된 `_save_paper` — `LIBERTREE_DB_BACKEND == "postgres"`면 무조건 v2 경로. 기본(sqlite)에서는 기존 `_conn_is_libertree()` 판정 로직 그대로.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_base_crawler_backend.py`:
```python
# -*- coding: utf-8 -*-
"""base_crawler 가 backend=postgres 일 때 PG 로 저장하는지 + sqlite 기본 회귀 확인."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _FakeCrawler:
    """base_crawler 의 저장 경로만 재사용하기 위한 최소 크롤러."""
    site_id = "fake"
    site_name = "Fake Site"
    base_url = "https://fake.example"

    def crawl(self, limit=None):  # not used here
        pass


def _make_instance(conn):
    from crawler.base_crawler import BaseCrawler

    # BaseCrawler 는 ABC 이므로 추상 메서드를 채운 서브클래스를 즉석 생성
    Concrete = type("Concrete", (BaseCrawler,), {
        "site_id": property(lambda self: "fake"),
        "site_name": property(lambda self: "Fake Site"),
        "base_url": property(lambda self: "https://fake.example"),
        "crawl": lambda self, limit=None: None,
    })
    return Concrete(db_conn=conn, delay=0)


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class BaseCrawlerPostgresTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.db_pg = db_pg
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP TABLE IF EXISTS documents CASCADE")
        self.conn.execute("DROP TABLE IF EXISTS sites CASCADE")
        self.conn.commit()
        db_pg.init_db(self.conn)
        db_pg.upsert_site(self.conn, "fake", "Fake Site", "https://fake.example")

    def tearDown(self):
        os.environ.pop("LIBERTREE_DB_BACKEND", None)
        self.conn.close()

    def test_save_paper_v2_writes_to_postgres(self):
        inst = _make_instance(self.conn)
        seq = inst._save_paper_v2({
            "site_id": "fake", "post_number": "1",
            "meta_url": "https://fake/a", "title": "T",
        })
        self.assertIsInstance(seq, int)
        row = self.db_pg.get_document(self.conn, seq)
        self.assertEqual(row["title"], "T")


class BaseCrawlerSqliteRegressionTest(unittest.TestCase):
    def test_default_backend_is_sqlite(self):
        # 회귀 가드: 환경변수 없으면 백엔드 셀렉터가 SQLite 를 돌려줘야 함
        os.environ.pop("LIBERTREE_DB_BACKEND", None)
        from crawler import db_backend, db_libertree
        self.assertIs(db_backend.get_backend(), db_libertree)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run:
```bash
export TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/libertree
.venv/bin/python -m unittest tests.test_base_crawler_backend -v
```
Expected: `test_save_paper_v2_writes_to_postgres` FAIL — 현재 `_save_paper_v2`는 `db_libertree`를 하드 임포트하므로 PG 커넥션에 SQLite 코드가 돌아 오류.

- [ ] **Step 3: `_save_paper_v2` 수정 (백엔드 셀렉터 사용)**

`crawler/base_crawler.py`의 `_save_paper_v2` 본문에서 아래를 교체.

기존:
```python
        from . import db_libertree as _ldb
        if not isinstance(doc, dict):
            raise TypeError("doc must be a dict")
        doc.setdefault("site_id", self.site_id)
        return _ldb.insert_document(self._conn, doc)
```
변경:
```python
        from . import db_backend
        if not isinstance(doc, dict):
            raise TypeError("doc must be a dict")
        doc.setdefault("site_id", self.site_id)
        return db_backend.get_backend().insert_document(self._conn, doc)
```

- [ ] **Step 4: `_save_paper` 라우팅 수정 (backend=postgres → v2)**

`crawler/base_crawler.py`의 `_save_paper`에서 `if self._conn_is_libertree():` 분기 조건을 확장한다.

기존:
```python
        if self._conn_is_libertree():
```
변경:
```python
        import os as _os
        if _os.environ.get("LIBERTREE_DB_BACKEND", "sqlite").lower() == "postgres" or self._conn_is_libertree():
```
(기본 sqlite에서는 조건 뒷항 `_conn_is_libertree()`가 그대로 판정 → 기존 동작 보존. postgres에서는 SQLite pragma 검사를 건너뛰고 v2 경로.)

- [ ] **Step 5: 테스트 실행 → 통과 확인**

Run: `.venv/bin/python -m unittest tests.test_base_crawler_backend -v`
Expected: PASS (2 tests OK, PG 미기동 시 1 skip+1 pass).

- [ ] **Step 6: SQLite 회귀 스모크 — 기존 테스트 전량 통과 확인**

Run:
```bash
unset LIBERTREE_DB_BACKEND
.venv/bin/python -m unittest tests.test_storage tests.test_translation_schema -v
```
Expected: PASS (기존 SQLite 경로 무변경 확인).

- [ ] **Step 7: 커밋**

```bash
git add crawler/base_crawler.py tests/test_base_crawler_backend.py
git commit -m "feat(delivery): route base_crawler saves through db_backend (postgres opt-in)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `crawl_jobs` 스키마 + 큐 소비(SKIP LOCKED)

**Files:**
- Create: `delivery/db/schema.py`
- Create: `delivery/worker/jobs.py`
- Test: `tests/test_worker_jobs.py`

**Interfaces:**
- Consumes: `crawler.db_pg.open_db` (Task 1).
- Produces:
  - `delivery/db/schema.py`: `init_delivery_schema(conn) -> None` — `crawl_jobs` 테이블 생성.
  - `delivery/worker/jobs.py`: `enqueue_job(conn, site_id, mode="incremental", limit_n=None, requested_by=None) -> int`; `claim_next_job(conn) -> dict | None` (`FOR UPDATE SKIP LOCKED`로 `queued`→`running` 전이, 잡히면 `{id, site_id, mode, limit_n}` 반환, 없으면 None); `finish_job(conn, job_id, saved_count) -> None`(→`done`); `fail_job(conn, job_id, error) -> None`(→`failed`).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_worker_jobs.py`:
```python
# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class WorkerJobsTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db import schema
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP TABLE IF EXISTS crawl_jobs CASCADE")
        self.conn.commit()
        schema.init_delivery_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_enqueue_and_claim(self):
        from delivery.worker import jobs
        jid = jobs.enqueue_job(self.conn, "s1", mode="full", limit_n=5, requested_by="op")
        self.assertIsInstance(jid, int)
        claimed = jobs.claim_next_job(self.conn)
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(claimed["site_id"], "s1")
        self.assertEqual(claimed["mode"], "full")
        self.assertEqual(claimed["limit_n"], 5)

    def test_claim_transitions_to_running(self):
        from delivery.worker import jobs
        jid = jobs.enqueue_job(self.conn, "s1")
        jobs.claim_next_job(self.conn)
        row = self.conn.execute("SELECT status FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "running")

    def test_claim_empty_returns_none(self):
        from delivery.worker import jobs
        self.assertIsNone(jobs.claim_next_job(self.conn))

    def test_skip_locked_no_double_claim(self):
        from crawler import db_pg
        from delivery.worker import jobs
        jobs.enqueue_job(self.conn, "s1")
        # 두 번째 커넥션이 동시에 잡으려 해도 같은 행을 두 번 잡지 않음
        conn2 = db_pg.open_db(TEST_PG_DSN)
        try:
            a = jobs.claim_next_job(self.conn)
            b = jobs.claim_next_job(conn2)
            self.assertIsNotNone(a)
            self.assertIsNone(b)  # 유일한 queued 작업은 이미 running
        finally:
            conn2.close()

    def test_finish_and_fail(self):
        from delivery.worker import jobs
        jid = jobs.enqueue_job(self.conn, "s1")
        jobs.claim_next_job(self.conn)
        jobs.finish_job(self.conn, jid, saved_count=7)
        row = self.conn.execute("SELECT status, saved_count FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["saved_count"], 7)

        jid2 = jobs.enqueue_job(self.conn, "s2")
        jobs.claim_next_job(self.conn)
        jobs.fail_job(self.conn, jid2, error="boom")
        row2 = self.conn.execute("SELECT status, error FROM crawl_jobs WHERE id=%s", (jid2,)).fetchone()
        self.assertEqual(row2["status"], "failed")
        self.assertEqual(row2["error"], "boom")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run: `.venv/bin/python -m unittest tests.test_worker_jobs -v`
Expected: FAIL — `No module named 'delivery'`.

- [ ] **Step 3: `delivery/db/schema.py` 구현**

`delivery/__init__.py`, `delivery/db/__init__.py`, `delivery/worker/__init__.py` 빈 파일도 함께 생성(패키지화).

`delivery/db/schema.py`:
```python
# -*- coding: utf-8 -*-
"""납품 전용 Postgres 스키마 (documents/sites 는 crawler.db_pg 담당)."""
from __future__ import annotations


def init_delivery_schema(conn) -> None:
    """Create delivery control tables if absent (idempotent). Phase 0: crawl_jobs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_jobs (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            site_id       TEXT NOT NULL,
            mode          TEXT NOT NULL DEFAULT 'incremental',
            status        TEXT NOT NULL DEFAULT 'queued',
            limit_n       INTEGER,
            saved_count   INTEGER NOT NULL DEFAULT 0,
            error         TEXT,
            requested_by  TEXT,
            created_at    TIMESTAMPTZ DEFAULT now(),
            started_at    TIMESTAMPTZ,
            finished_at   TIMESTAMPTZ
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON crawl_jobs(status, created_at)"
    )
    conn.commit()
```

- [ ] **Step 4: `delivery/worker/jobs.py` 구현**

```python
# -*- coding: utf-8 -*-
"""crawl_jobs 큐 조작 — 원자적 소비(SKIP LOCKED)."""
from __future__ import annotations

from typing import Optional


def enqueue_job(conn, site_id, mode="incremental", limit_n=None, requested_by=None) -> int:
    row = conn.execute(
        """
        INSERT INTO crawl_jobs (site_id, mode, limit_n, requested_by)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (site_id, mode, limit_n, requested_by),
    ).fetchone()
    conn.commit()
    return row["id"]


def claim_next_job(conn) -> Optional[dict]:
    """Atomically claim one queued job (status -> running). None if queue empty."""
    row = conn.execute(
        """
        UPDATE crawl_jobs SET status='running', started_at=now()
         WHERE id = (
            SELECT id FROM crawl_jobs
             WHERE status='queued'
             ORDER BY created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1
         )
        RETURNING id, site_id, mode, limit_n
        """
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return {"id": row["id"], "site_id": row["site_id"], "mode": row["mode"], "limit_n": row["limit_n"]}


def finish_job(conn, job_id, saved_count) -> None:
    conn.execute(
        "UPDATE crawl_jobs SET status='done', saved_count=%s, finished_at=now() WHERE id=%s",
        (int(saved_count or 0), job_id),
    )
    conn.commit()


def fail_job(conn, job_id, error) -> None:
    conn.execute(
        "UPDATE crawl_jobs SET status='failed', error=%s, finished_at=now() WHERE id=%s",
        ((error or "")[:2000], job_id),
    )
    conn.commit()
```

- [ ] **Step 5: 테스트 실행 → 통과 확인**

Run: `.venv/bin/python -m unittest tests.test_worker_jobs -v`
Expected: PASS (5 tests OK).

- [ ] **Step 6: 커밋**

```bash
git add delivery/__init__.py delivery/db/__init__.py delivery/db/schema.py \
        delivery/worker/__init__.py delivery/worker/jobs.py tests/test_worker_jobs.py
git commit -m "feat(delivery): crawl_jobs schema + SKIP LOCKED queue consumer

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 워커 실행 — `run_job` (claim → crawl → update)

**Files:**
- Create: `delivery/worker/worker.py`
- Test: `tests/test_worker_run.py`

**Interfaces:**
- Consumes: `delivery.worker.jobs` (Task 4), `crawler.db_pg` (Task 1), `crawler.sites.CRAWLERS`.
- Produces: `run_job(conn, job: dict, crawler_registry=None, delay=1.0) -> int` — `crawler_registry`(기본 `crawler.sites.CRAWLERS`)에서 `job["site_id"]` 크롤러를 찾아 `crawl(limit=job["limit_n"])` 실행 후, 그 사이트에 저장된 문서 수를 세어 `finish_job`으로 기록하고 저장수 반환. 크롤러 없거나 예외 시 `fail_job`. `run_once(conn, ...) -> bool`(작업 하나 처리했으면 True). `main()`(--once/루프).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_worker_run.py`:
```python
# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _FakeCrawler:
    """crawl() 시 PG 에 문서 2건 저장하는 가짜 크롤러."""
    site_id = "fake"
    site_name = "Fake"
    base_url = "https://fake.example"

    def __init__(self, db_conn, delay=1.0):
        self._conn = db_conn
        self._delay = delay

    def crawl(self, limit=None):
        from crawler import db_pg
        db_pg.insert_document(self._conn, {"site_id": "fake", "post_number": "1",
                                           "meta_url": "https://fake/1", "title": "A"})
        db_pg.insert_document(self._conn, {"site_id": "fake", "post_number": "2",
                                           "meta_url": "https://fake/2", "title": "B"})


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class WorkerRunTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db import schema
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.db_pg = db_pg
        self.conn = db_pg.open_db(TEST_PG_DSN)
        for t in ("crawl_jobs", "documents", "sites"):
            self.conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        self.conn.commit()
        db_pg.init_db(self.conn)
        schema.init_delivery_schema(self.conn)
        db_pg.upsert_site(self.conn, "fake", "Fake", "https://fake.example")

    def tearDown(self):
        os.environ.pop("LIBERTREE_DB_BACKEND", None)
        self.conn.close()

    def test_run_job_saves_and_finishes(self):
        from delivery.worker import jobs, worker
        jid = jobs.enqueue_job(self.conn, "fake", mode="full")
        job = jobs.claim_next_job(self.conn)
        saved = worker.run_job(self.conn, job, crawler_registry={"fake": _FakeCrawler}, delay=0)
        self.assertEqual(saved, 2)
        row = self.conn.execute("SELECT status, saved_count FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["saved_count"], 2)

    def test_run_job_unknown_site_fails(self):
        from delivery.worker import jobs, worker
        jid = jobs.enqueue_job(self.conn, "nope")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job, crawler_registry={}, delay=0)
        row = self.conn.execute("SELECT status, error FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("nope", row["error"])

    def test_run_once_processes_one(self):
        from delivery.worker import jobs, worker
        jobs.enqueue_job(self.conn, "fake", mode="full")
        did = worker.run_once(self.conn, crawler_registry={"fake": _FakeCrawler}, delay=0)
        self.assertTrue(did)
        self.assertFalse(worker.run_once(self.conn, crawler_registry={"fake": _FakeCrawler}, delay=0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run: `.venv/bin/python -m unittest tests.test_worker_run -v`
Expected: FAIL — `cannot import name 'worker'`.

- [ ] **Step 3: `delivery/worker/worker.py` 구현**

```python
# -*- coding: utf-8 -*-
"""워커: crawl_jobs 를 소비해 크롤러를 실행한다.

기본 백엔드는 postgres 로 강제(LIBERTREE_DB_BACKEND). crawlers-share 와 동일한
호출 패턴: CRAWLERS[site_id](db_conn=conn, delay=...).crawl(limit).
"""
from __future__ import annotations

import os
import time

from . import jobs


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
        jobs.fail_job(conn, job["id"], f"{type(exc).__name__}: {exc}")
        return 0
    saved = _count_site_docs(conn, site_id) - before
    jobs.finish_job(conn, job["id"], saved_count=max(0, saved))
    return max(0, saved)


def run_once(conn, crawler_registry=None, delay=1.0) -> bool:
    """Claim+run one job. False if the queue was empty."""
    job = jobs.claim_next_job(conn)
    if job is None:
        return False
    run_job(conn, job, crawler_registry=crawler_registry, delay=delay)
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

    conn = db_pg.open_db(args.dsn)
    try:
        if args.once:
            run_once(conn, delay=args.delay)
            return 0
        while True:
            if not run_once(conn, delay=args.delay):
                time.sleep(args.poll)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 테스트 실행 → 통과 확인**

Run: `.venv/bin/python -m unittest tests.test_worker_run -v`
Expected: PASS (3 tests OK).

- [ ] **Step 5: 커밋**

```bash
git add delivery/worker/worker.py tests/test_worker_run.py
git commit -m "feat(delivery): worker run_job/run_once (claim -> crawl -> update)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 최소 FastAPI BE (`/health`, 문서 조회, 작업 enqueue)

**Files:**
- Create: `delivery/be/__init__.py`, `delivery/be/app.py`
- Test: `tests/test_be_app.py`

**Interfaces:**
- Consumes: `crawler.db_pg` (Task 1), `delivery.db.schema` (Task 4), `delivery.worker.jobs` (Task 4).
- Produces: `create_app(dsn: str) -> FastAPI` with routes: `GET /health` (PG 쿼리 성공 시 200 `{"status":"ok"}`, 실패 시 503), `GET /documents/{seq_id}` (있으면 문서 JSON, 없으면 404), `POST /jobs` (body `{site_id, mode?, limit_n?}` → `{"id": <int>}`), `GET /jobs/{id}` (작업 상태 JSON, 없으면 404).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_be_app.py`:
```python
# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class BeAppTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from crawler import db_pg
        from delivery.db import schema
        from delivery.be.app import create_app

        conn = db_pg.open_db(TEST_PG_DSN)
        for t in ("crawl_jobs", "documents", "sites"):
            conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        conn.commit()
        db_pg.init_db(conn)
        schema.init_delivery_schema(conn)
        db_pg.upsert_site(conn, "s1", "Site One", "https://s1.example")
        self.seq = db_pg.insert_document(conn, {"site_id": "s1", "post_number": "1",
                                                "meta_url": "https://s1/1", "title": "Doc One"})
        conn.close()
        self.client = TestClient(create_app(TEST_PG_DSN))

    def test_health_ok(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_get_document(self):
        r = self.client.get(f"/documents/{self.seq}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "Doc One")

    def test_get_document_404(self):
        r = self.client.get("/documents/999999")
        self.assertEqual(r.status_code, 404)

    def test_post_and_get_job(self):
        r = self.client.post("/jobs", json={"site_id": "s1", "mode": "full", "limit_n": 3})
        self.assertEqual(r.status_code, 200)
        jid = r.json()["id"]
        r2 = self.client.get(f"/jobs/{jid}")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["site_id"], "s1")
        self.assertEqual(r2.json()["status"], "queued")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run: `.venv/bin/python -m unittest tests.test_be_app -v`
Expected: FAIL — `No module named 'delivery.be.app'` (또는 fastapi 미설치 시 import 에러).

- [ ] **Step 3: BE 의존성 설치 + `delivery/be/app.py` 구현**

Run: `.venv/bin/pip install "fastapi==0.115.*" "uvicorn[standard]==0.32.*" httpx`
(`httpx`는 `fastapi.testclient`에 필요.)

`delivery/be/__init__.py`: 빈 파일.

`delivery/be/app.py`:
```python
# -*- coding: utf-8 -*-
"""최소 납품 BE (Phase 0): health, 문서 조회, 작업 enqueue/조회.

DB 접근은 crawler.db_pg 단일 경로. 요청마다 짧은 커넥션을 연다(Phase 0 단순화;
커넥션 풀은 Phase 1 에서 도입).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from crawler import db_pg
from delivery.worker import jobs


class JobIn(BaseModel):
    site_id: str
    mode: str = "incremental"
    limit_n: int | None = None
    requested_by: str | None = None


def create_app(dsn: str) -> FastAPI:
    app = FastAPI(title="Libertree Delivery BE (Phase 0)")

    def _conn():
        return db_pg.open_db(dsn)

    @app.get("/health")
    def health():
        try:
            conn = _conn()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)[:200]})
        return {"status": "ok"}

    @app.get("/documents/{seq_id}")
    def get_document(seq_id: int):
        conn = _conn()
        try:
            row = db_pg.get_document(conn, seq_id)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="document not found")
        return dict(row)

    @app.post("/jobs")
    def post_job(body: JobIn):
        conn = _conn()
        try:
            jid = jobs.enqueue_job(conn, body.site_id, mode=body.mode,
                                   limit_n=body.limit_n, requested_by=body.requested_by)
        finally:
            conn.close()
        return {"id": jid}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: int):
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM crawl_jobs WHERE id=%s", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return dict(row)

    return app
```

- [ ] **Step 4: 테스트 실행 → 통과 확인**

Run: `.venv/bin/python -m unittest tests.test_be_app -v`
Expected: PASS (4 tests OK).

- [ ] **Step 5: 커밋**

```bash
git add delivery/be/__init__.py delivery/be/app.py tests/test_be_app.py
git commit -m "feat(delivery): minimal FastAPI BE (health, document, jobs)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Dockerfile(be/worker) + docker-compose + `.env.example`

**Files:**
- Create: `delivery/Dockerfile.be`, `delivery/Dockerfile.worker`
- Create: `delivery/docker-compose.yml`
- Create: `delivery/.env.example`
- Create: `delivery/be/entry.py` (uvicorn `--factory` 진입점)

**Interfaces:**
- Consumes: 전 Task 산출물(crawler/, delivery/).
- Produces: `docker compose -f delivery/docker-compose.yml up`로 postgres+be+worker 기동. be는 `${BE_PORT:-8080}`에서 listen.

- [ ] **Step 1: `.env.example` 작성 (값 없이 키 이름만)**

`delivery/.env.example`:
```
# Postgres
POSTGRES_USER=libertree
POSTGRES_PASSWORD=
POSTGRES_DB=libertree
# BE/worker 가 쓰는 DSN (compose 내부 네트워크 기준)
LIBERTREE_PG_DSN=postgresql://libertree:@postgres:5432/libertree
# 크롤러 백엔드 (납품 시스템은 postgres 고정)
LIBERTREE_DB_BACKEND=postgres
# 블롭(PDF) 볼륨 마운트 경로 (컨테이너 내부)
LIBERTREE_BLOB_ROOT=/data/blob
# BE 노출 포트(호스트)
BE_PORT=8080
```

- [ ] **Step 2: be 진입 팩토리 작성**

`delivery/be/entry.py` (compose 환경변수에서 DSN 을 읽어 앱을 만든다; uvicorn `--factory`가 `app()`을 호출):
```python
# -*- coding: utf-8 -*-
import os
from delivery.be.app import create_app


def app():
    dsn = os.environ["LIBERTREE_PG_DSN"]
    return create_app(dsn)
```

- [ ] **Step 3: Dockerfile 작성**

`delivery/Dockerfile.be`:
```dockerfile
FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY delivery/requirements.txt /app/delivery/requirements.txt
RUN pip install --no-cache-dir -r /app/delivery/requirements.txt
# 크롤러 런타임 의존성(requests, beautifulsoup 등)도 필요
COPY crawlers-share/requirements.txt /app/crawler-runtime-requirements.txt
RUN pip install --no-cache-dir -r /app/crawler-runtime-requirements.txt
COPY crawler /app/crawler
COPY delivery /app/delivery
EXPOSE 3001
CMD ["uvicorn", "--factory", "--host", "0.0.0.0", "--port", "3001", "delivery.be.entry:app"]
```

`delivery/Dockerfile.worker`:
```dockerfile
FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY delivery/requirements.txt /app/delivery/requirements.txt
RUN pip install --no-cache-dir -r /app/delivery/requirements.txt
COPY crawlers-share/requirements.txt /app/crawler-runtime-requirements.txt
RUN pip install --no-cache-dir -r /app/crawler-runtime-requirements.txt
COPY crawler /app/crawler
COPY delivery /app/delivery
CMD ["python", "-m", "delivery.worker.worker", "--poll", "5"]
```

> 확인: `crawlers-share/requirements.txt` 존재(메모리 기준 curl_cffi 포함). 없으면 이 Task 진행 전 `crawler`가 임포트하는 런타임 패키지(requests, beautifulsoup4, lxml, curl_cffi, python-dotenv, playwright 등)를 나열한 파일을 만든다.

- [ ] **Step 4: docker-compose 작성**

`delivery/docker-compose.yml`:
```yaml
name: libertree-delivery

services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-libertree}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
      POSTGRES_DB: ${POSTGRES_DB:-libertree}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-libertree} -d ${POSTGRES_DB:-libertree}"]
      interval: 5s
      timeout: 3s
      retries: 20

  be:
    build:
      context: ..
      dockerfile: delivery/Dockerfile.be
    environment:
      LIBERTREE_PG_DSN: ${LIBERTREE_PG_DSN}
      LIBERTREE_DB_BACKEND: postgres
      LIBERTREE_BLOB_ROOT: /data/blob
    volumes:
      - blob:/data/blob
    ports:
      - "127.0.0.1:${BE_PORT:-8080}:3001"
    depends_on:
      postgres:
        condition: service_healthy

  worker:
    build:
      context: ..
      dockerfile: delivery/Dockerfile.worker
    environment:
      LIBERTREE_PG_DSN: ${LIBERTREE_PG_DSN}
      LIBERTREE_DB_BACKEND: postgres
      LIBERTREE_BLOB_ROOT: /data/blob
    volumes:
      - blob:/data/blob
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  pgdata:
  blob:
```

- [ ] **Step 5: 빌드 스모크 (실행은 Task 8)**

Run:
```bash
cd delivery && cp .env.example .env && sed -i 's/^POSTGRES_PASSWORD=/POSTGRES_PASSWORD=devpw/; s#postgresql://libertree:@postgres#postgresql://libertree:devpw@postgres#' .env
docker compose -f docker-compose.yml build be worker
cd ..
```
Expected: be/worker 이미지 빌드 성공. (`.env`는 gitignore 대상 — 커밋 금지.)

- [ ] **Step 6: `.gitignore`에 delivery/.env 추가 + 커밋**

Run:
```bash
grep -qxF 'delivery/.env' .gitignore || echo 'delivery/.env' >> .gitignore
git add delivery/Dockerfile.be delivery/Dockerfile.worker delivery/docker-compose.yml \
        delivery/.env.example delivery/be/entry.py .gitignore
git commit -m "feat(delivery): docker-compose (postgres+be+worker) + env template

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: End-to-end 기동 검증 (빈 시작 → enqueue → 워커 수집 → 조회)

**Files:**
- Create: `delivery/scripts/e2e_phase0.sh` (검증 스크립트)

**Interfaces:**
- Consumes: 전 Task 산출물.
- Produces: `delivery/scripts/e2e_phase0.sh` — compose 기동, 스키마 초기화, 실제(가벼운) 크롤 작업 enqueue, 워커 처리 대기, 문서 조회 성공 확인.

- [ ] **Step 1: 스키마 부트스트랩을 be 시작 시 보장**

`delivery/be/entry.py`의 `app()`가 앱 생성 전에 스키마를 초기화하도록 확장.

기존:
```python
def app():
    dsn = os.environ["LIBERTREE_PG_DSN"]
    return create_app(dsn)
```
변경:
```python
def app():
    dsn = os.environ["LIBERTREE_PG_DSN"]
    from crawler import db_pg
    from delivery.db import schema
    conn = db_pg.open_db(dsn)
    try:
        db_pg.init_db(conn)
        schema.init_delivery_schema(conn)
    finally:
        conn.close()
    return create_app(dsn)
```

- [ ] **Step 2: e2e 스크립트 작성**

`delivery/scripts/e2e_phase0.sh`:
```sh
#!/bin/sh
# Phase 0 end-to-end: 빈 시작 → 실제 사이트 시드 → 작업 enqueue → 워커 수집 → 조회.
set -e
cd "$(dirname "$0")/.."   # delivery/
BE="http://127.0.0.1:${BE_PORT:-8080}"

echo "[1/6] compose up"
docker compose up -d --build
echo "[2/6] be health 대기"
for i in $(seq 1 40); do
  if curl -fsS "$BE/health" >/dev/null 2>&1; then echo "  ok"; break; fi
  sleep 2
done

echo "[3/6] 대상 사이트 site 행 시드(FK 충족)"
# 가벼운 실제 크롤러 하나 선택. limit 3.
SITE=datos-gob-mx-agricultura
docker compose exec -T postgres psql -U "${POSTGRES_USER:-libertree}" -d "${POSTGRES_DB:-libertree}" \
  -c "INSERT INTO sites(site_id,site_name,site_url) VALUES ('$SITE','$SITE','https://datos.gob.mx') ON CONFLICT DO NOTHING;"

echo "[4/6] 작업 enqueue (limit 3)"
JID=$(curl -fsS -X POST "$BE/jobs" -H 'content-type: application/json' \
  -d "{\"site_id\":\"$SITE\",\"mode\":\"full\",\"limit_n\":3}" | sed 's/.*"id":\([0-9]*\).*/\1/')
echo "  job id=$JID"

echo "[5/6] 워커 처리 대기 (done 까지)"
for i in $(seq 1 60); do
  ST=$(curl -fsS "$BE/jobs/$JID" | sed 's/.*"status":"\([a-z]*\)".*/\1/')
  echo "  status=$ST"
  [ "$ST" = "done" ] && break
  [ "$ST" = "failed" ] && { echo "FAILED"; curl -fsS "$BE/jobs/$JID"; exit 1; }
  sleep 3
done

echo "[6/6] 저장 문서 조회"
N=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "${POSTGRES_DB:-libertree}" \
  -c "SELECT count(*) FROM documents WHERE site_id='$SITE';")
echo "  documents for $SITE = $N"
[ "$N" -ge 1 ] || { echo "NO DOCS SAVED"; exit 1; }
echo "PHASE 0 E2E: PASS"
```

- [ ] **Step 3: e2e 실행 → 통과 확인**

Run:
```bash
chmod +x delivery/scripts/e2e_phase0.sh
cd delivery && set -a && . ./.env && set +a && ./scripts/e2e_phase0.sh; cd ..
```
Expected: 마지막 줄 `PHASE 0 E2E: PASS`, `documents for datos-gob-mx-agricultura >= 1`.

- [ ] **Step 4: 정리(선택) + 커밋**

Run:
```bash
cd delivery && docker compose down; cd ..
git add delivery/be/entry.py delivery/scripts/e2e_phase0.sh
git commit -m "feat(delivery): phase0 e2e verification (empty start -> crawl -> query)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 5: 테스트 PG 정리**

Run: `delivery/scripts/test_pg.sh down`
Expected: `stopped libertree-test-pg`.

---

## Phase 0 완료 기준 (Definition of Done)

- [ ] `crawler/db_pg.py` 단위 테스트 전량 통과 (dedup/insert/조회).
- [ ] `LIBERTREE_DB_BACKEND` 미설정 시 SQLite 회귀 없음 (기존 `test_storage`·`test_translation_schema` 통과).
- [ ] 워커 큐가 SKIP LOCKED 로 이중 소비 없음.
- [ ] `docker compose up`으로 postgres+be+worker 기동, `/health` 200.
- [ ] e2e: 빈 DB → 작업 enqueue → 워커가 실제 사이트에서 문서 수집 → PG 저장 → `GET /documents/{seq_id}` 반환.
- [ ] 커밋에 실제 비밀 없음 (`delivery/.env`는 gitignore).

## 다음 (Phase 1 예고)
카탈로그 API(검색/브라우즈/상세/블롭, 기존 `catalogue.ts` 쿼리 이식) + FE 데이터층을 BE API 호출로 교체 + compose에 `fe` 서비스 합류. 별도 계획 문서로 작성.
