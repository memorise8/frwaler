# FINO Ops 통합 + 대시보드 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 크롤러 4종(6코퍼스)을 단일 레지스트리+상태DB(fino_ops.db)로 통합하고, FastAPI API(:8500) + Next.js 대시보드로 최신화 상태 조회·갱신 트리거를 제공한다.

**Architecture:** `crawler/fino_ops/`(레지스트리·runner·CLI·FastAPI)가 기존 크롤러 CLI를 subprocess로 호출(크롤러 코드 무수정). `dashboard/`(Next.js 15)는 rewrites로 FastAPI를 프록시. 상태는 `data/fino_ops.db`의 runs 테이블 하나.

**Tech Stack:** Python 3(.venv: fastapi 0.135, uvicorn 0.44, pytest), Node 22 + Next.js 15(TS, Tailwind, App Router).

**스펙:** `docs/superpowers/specs/2026-07-02-fino-ops-dashboard-design.md`

## Global Constraints

- 기존 크롤러 코드(crawler/fino_law·fino_acct·fino_std·sites·main.py) **무수정**. 통합은 subprocess 호출로만.
- refresh는 **글로벌 동시 1개** (소스 사이트 매너). 실행 중 POST → HTTP 409.
- API 바인드는 127.0.0.1:8500, 인증 없음(로컬 전용).
- papers.db 경로: env `FINO_PAPERS_DB` (기본 `/data_raid/ruci_workspace/crawler-poc/data/papers.db`). nts refresh 시 subprocess env에 `FINOLAW_DB_PATH` 주입.
- `data/fino_ops.db`, `data/ops_logs/`, `data/export/`, `dashboard/node_modules`·`.next`는 커밋 금지(.gitignore).
- 6코퍼스 key 고정: `law, exec, acct, std, nts_qt, nts_pd`.
- 테스트: `.venv/bin/python -m pytest tests/test_fino_ops_*.py -q`. 실 크롤러 호출 없는 유닛/TestClient만.
- 커밋 스타일: `feat(fino_ops): ...`.

---

### Task 1: fino_ops 상태DB — runs 테이블 + 라이프사이클

**Files:**
- Create: `crawler/fino_ops/__init__.py` (빈 파일)
- Create: `crawler/fino_ops/db.py`
- Test: `tests/test_fino_ops_db.py`

**Interfaces:**
- Produces: `DEFAULT_OPS_DB: Path`, `connect_ops(db_path) -> Connection`, `init_ops_schema(conn)`, `start_run(conn, corpus, log_path) -> int`, `finish_run(conn, run_id, *, status, new_count=None, total_after=None, error=None)`, `any_running(conn) -> bool`, `last_run(conn, corpus) -> dict | None`, `recent_runs(conn, limit=50) -> list[dict]`, `mark_stale_running(conn) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_fino_ops_db.py`:

```python
from pathlib import Path

from crawler.fino_ops.db import (
    any_running, connect_ops, finish_run, init_ops_schema,
    last_run, mark_stale_running, recent_runs, start_run,
)


def _conn(tmp_path: Path):
    conn = connect_ops(tmp_path / "ops.db")
    init_ops_schema(conn)
    return conn


def test_run_lifecycle_ok(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = start_run(conn, "std", "data/ops_logs/x.log")
    assert any_running(conn)
    finish_run(conn, rid, status="ok", new_count=3, total_after=100)
    assert not any_running(conn)
    lr = last_run(conn, "std")
    assert lr["status"] == "ok" and lr["new_count"] == 3 and lr["total_after"] == 100
    assert lr["finished_at"] is not None


def test_run_lifecycle_error(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = start_run(conn, "law", "l.log")
    finish_run(conn, rid, status="error", error="exit code 3")
    assert last_run(conn, "law")["error"] == "exit code 3"
    assert last_run(conn, "std") is None


def test_recent_runs_ordering_and_limit(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for i in range(5):
        rid = start_run(conn, "std", f"{i}.log")
        finish_run(conn, rid, status="ok", new_count=i, total_after=i)
    rows = recent_runs(conn, limit=3)
    assert len(rows) == 3
    assert [r["new_count"] for r in rows] == [4, 3, 2]   # 최신순


def test_mark_stale_running(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    start_run(conn, "std", "s.log")
    start_run(conn, "law", "l.log")
    assert mark_stale_running(conn) == 2
    assert not any_running(conn)
    assert last_run(conn, "std")["status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_db.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'crawler.fino_ops'`

- [ ] **Step 3: Write implementation**

`crawler/fino_ops/__init__.py`: 빈 파일.

`crawler/fino_ops/db.py`:

```python
from __future__ import annotations

from pathlib import Path
import sqlite3

DEFAULT_OPS_DB = Path("data/fino_ops.db")


def connect_ops(db_path: Path = DEFAULT_OPS_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_ops_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            new_count INTEGER,
            total_after INTEGER,
            log_path TEXT NOT NULL DEFAULT '',
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_corpus_id ON runs(corpus, id);
        """
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, corpus: str, log_path: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (corpus, log_path) VALUES (?, ?)", (corpus, log_path)
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, *, status: str,
    new_count: int | None = None, total_after: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = datetime('now', 'localtime'),
            status = ?, new_count = ?, total_after = ?, error = ?
        WHERE id = ?
        """,
        (status, new_count, total_after, error, run_id),
    )
    conn.commit()


def any_running(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM runs WHERE status = 'running' LIMIT 1").fetchone()
    return row is not None


def last_run(conn: sqlite3.Connection, corpus: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE corpus = ? ORDER BY id DESC LIMIT 1", (corpus,)
    ).fetchone()
    return dict(row) if row else None


def recent_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def mark_stale_running(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        UPDATE runs SET status = 'error', error = 'stale: 서버 재시작으로 중단 처리',
            finished_at = datetime('now', 'localtime')
        WHERE status = 'running'
        """
    )
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_db.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_ops/__init__.py crawler/fino_ops/db.py tests/test_fino_ops_db.py
git commit -m "feat(fino_ops): 상태DB runs 테이블 — 실행 라이프사이클/이력/stale 복구"
```

---

### Task 2: 코퍼스 레지스트리 + stats 어댑터

**Files:**
- Create: `crawler/fino_ops/corpora.py`
- Test: `tests/test_fino_ops_corpora.py`

**Interfaces:**
- Consumes: 없음 (소스DB는 read-only SQL)
- Produces: `Corpus(key, label, db_path: Path, count_sql, freshness_sql, argv: tuple, env: dict)`, `CORPORA: dict[str, Corpus]` (6키: law/exec/acct/std/nts_qt/nts_pd), `corpus_stats(c) -> {"total", "last_collected", "db_exists"}`, `REPO_ROOT: Path`, `PAPERS_DB: Path`

- [ ] **Step 1: Write the failing test**

`tests/test_fino_ops_corpora.py`:

```python
import sqlite3
from pathlib import Path

from crawler.fino_ops.corpora import CORPORA, Corpus, corpus_stats


def test_registry_has_six_corpora_with_expected_wiring() -> None:
    assert set(CORPORA) == {"law", "exec", "acct", "std", "nts_qt", "nts_pd"}
    assert "--law" in CORPORA["law"].argv and "--exec" in CORPORA["exec"].argv
    assert "--incremental" in CORPORA["nts_qt"].argv
    assert "FINOLAW_DB_PATH" in CORPORA["nts_pd"].env       # papers.db 주입
    assert CORPORA["std"].argv[-1].endswith("collect")


def _mini_std_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, collected_at TEXT);
        CREATE TABLE paragraphs (id INTEGER PRIMARY KEY, document_id INTEGER);
        INSERT INTO documents VALUES (1, '2026-07-02 10:00:00');
        INSERT INTO paragraphs VALUES (1, 1), (2, 1), (3, 1);
        """
    )
    conn.commit()
    conn.close()


def test_corpus_stats_reads_total_and_freshness(tmp_path: Path) -> None:
    db = tmp_path / "mini.db"
    _mini_std_db(db)
    c = Corpus(
        key="std", label="테스트", db_path=db,
        count_sql="SELECT COUNT(*) FROM paragraphs",
        freshness_sql="SELECT MAX(collected_at) FROM documents",
        argv=("true",),
    )
    s = corpus_stats(c)
    assert s == {"total": 3, "last_collected": "2026-07-02 10:00:00", "db_exists": True}


def test_corpus_stats_missing_db(tmp_path: Path) -> None:
    c = Corpus(key="x", label="없음", db_path=tmp_path / "nope.db",
               count_sql="SELECT 1", freshness_sql="SELECT 1", argv=("true",))
    assert corpus_stats(c) == {"total": None, "last_collected": None, "db_exists": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_corpora.py -q`
Expected: FAIL — `ModuleNotFoundError` (corpora 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_ops/corpora.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAPERS_DB = Path(os.environ.get(
    "FINO_PAPERS_DB", "/data_raid/ruci_workspace/crawler-poc/data/papers.db"))
_PY = str(REPO_ROOT / ".venv" / "bin" / "python")


@dataclass(frozen=True)
class Corpus:
    key: str
    label: str
    db_path: Path
    count_sql: str
    freshness_sql: str
    argv: tuple
    env: dict = field(default_factory=dict)


def _nts(key: str, label: str, site_id: str) -> Corpus:
    return Corpus(
        key=key, label=label, db_path=PAPERS_DB,
        count_sql=f"SELECT COUNT(*) FROM papers WHERE site_id='{site_id}'",
        freshness_sql=f"SELECT MAX(crawled_at) FROM papers WHERE site_id='{site_id}'",
        argv=(_PY, "-m", "crawler.main", "crawl", site_id, "--incremental"),
        env={"FINOLAW_DB_PATH": str(PAPERS_DB)},
    )


CORPORA: dict[str, Corpus] = {
    "law": Corpus(
        key="law", label="세법 법령", db_path=REPO_ROOT / "data" / "fino_law.db",
        count_sql=("SELECT COUNT(*) FROM articles a JOIN documents d ON d.id=a.document_id "
                   "WHERE d.source_kind='law'"),
        freshness_sql="SELECT MAX(collected_at) FROM documents WHERE source_kind='law'",
        argv=(_PY, "-m", "crawler.fino_law.collect", "--law"),
    ),
    "exec": Corpus(
        key="exec", label="세법 집행기준", db_path=REPO_ROOT / "data" / "fino_law.db",
        count_sql=("SELECT COUNT(*) FROM articles a JOIN documents d ON d.id=a.document_id "
                   "WHERE d.source_kind='exec_standard'"),
        freshness_sql="SELECT MAX(collected_at) FROM documents WHERE source_kind='exec_standard'",
        argv=(_PY, "-m", "crawler.fino_law.collect", "--exec"),
    ),
    "acct": Corpus(
        key="acct", label="회계 질의회신", db_path=REPO_ROOT / "data" / "fino_acct.db",
        count_sql="SELECT COUNT(*) FROM acct_documents",
        freshness_sql="SELECT MAX(fetched_at) FROM acct_documents",
        argv=(_PY, "-m", "crawler.fino_acct.collect", "--priorities", "1,2,3,4,5,6"),
    ),
    "std": Corpus(
        key="std", label="회계 기준서(K-IFRS/GAAP)", db_path=REPO_ROOT / "data" / "fino_std.db",
        count_sql="SELECT COUNT(*) FROM paragraphs",
        freshness_sql="SELECT MAX(collected_at) FROM documents",
        argv=(_PY, "-m", "crawler.fino_std.collect"),
    ),
    "nts_qt": _nts("nts_qt", "NTS 예규·질의", "nts-taxlaw-qt"),
    "nts_pd": _nts("nts_pd", "NTS 판례", "nts-taxlaw-pd"),
}


def corpus_stats(c: Corpus) -> dict:
    if not c.db_path.exists():
        return {"total": None, "last_collected": None, "db_exists": False}
    conn = sqlite3.connect(f"file:{c.db_path}?mode=ro", uri=True)
    try:
        total = conn.execute(c.count_sql).fetchone()[0]
        last = conn.execute(c.freshness_sql).fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "last_collected": last, "db_exists": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_corpora.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_ops/corpora.py tests/test_fino_ops_corpora.py
git commit -m "feat(fino_ops): 6코퍼스 레지스트리 + read-only stats 어댑터"
```

---

### Task 3: runner + CLI — subprocess 실행, 락, new_count

**Files:**
- Create: `crawler/fino_ops/runner.py`
- Create: `crawler/fino_ops/cli.py`
- Create: `crawler/fino_ops/__main__.py`
- Test: `tests/test_fino_ops_runner.py`

**Interfaces:**
- Consumes: Task 1 db.*, Task 2 CORPORA/corpus_stats/REPO_ROOT
- Produces: `BusyError(RuntimeError)`, `refresh_corpus(key, *, ops_db=DEFAULT_OPS_DB, log_dir=Path("data/ops_logs"), argv=None, cwd=None, timeout=7200) -> int` (run_id 반환; 실패해도 raise하지 않고 error 기록 — BusyError만 raise), CLI `python -m crawler.fino_ops status|refresh --corpus KEY|--all`

- [ ] **Step 1: Write the failing test**

`tests/test_fino_ops_runner.py`:

```python
import sys
from pathlib import Path

import crawler.fino_ops.runner as runner_mod
from crawler.fino_ops.corpora import Corpus
from crawler.fino_ops.db import connect_ops, init_ops_schema, last_run, start_run
from crawler.fino_ops.runner import BusyError, refresh_corpus
import pytest
import sqlite3


def _make_corpus(tmp_path: Path) -> Corpus:
    db = tmp_path / "mini.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rows (v TEXT)")
    conn.execute("INSERT INTO rows VALUES ('a')")
    conn.commit()
    conn.close()
    return Corpus(key="mini", label="미니", db_path=db,
                  count_sql="SELECT COUNT(*) FROM rows",
                  freshness_sql="SELECT MAX(v) FROM rows", argv=("unused",))


def _install(monkeypatch, c: Corpus) -> None:
    monkeypatch.setattr(runner_mod, "CORPORA", {c.key: c})


def test_refresh_success_records_new_count(tmp_path: Path, monkeypatch) -> None:
    c = _make_corpus(tmp_path)
    _install(monkeypatch, c)
    insert = (f"import sqlite3; con=sqlite3.connect(r'{c.db_path}');"
              "con.execute(\"INSERT INTO rows VALUES ('b')\"); con.commit()")
    rid = refresh_corpus("mini", ops_db=tmp_path / "ops.db", log_dir=tmp_path / "logs",
                         argv=(sys.executable, "-c", insert))
    conn = connect_ops(tmp_path / "ops.db")
    lr = last_run(conn, "mini")
    assert lr["id"] == rid and lr["status"] == "ok"
    assert lr["new_count"] == 1 and lr["total_after"] == 2
    assert Path(lr["log_path"]).exists()


def test_refresh_failure_records_error(tmp_path: Path, monkeypatch) -> None:
    c = _make_corpus(tmp_path)
    _install(monkeypatch, c)
    refresh_corpus("mini", ops_db=tmp_path / "ops.db", log_dir=tmp_path / "logs",
                   argv=(sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"))
    conn = connect_ops(tmp_path / "ops.db")
    lr = last_run(conn, "mini")
    assert lr["status"] == "error" and "exit code 3" in lr["error"]
    assert "boom" in Path(lr["log_path"]).read_text(encoding="utf-8")


def test_refresh_busy_raises(tmp_path: Path, monkeypatch) -> None:
    c = _make_corpus(tmp_path)
    _install(monkeypatch, c)
    conn = connect_ops(tmp_path / "ops.db")
    init_ops_schema(conn)
    start_run(conn, "other", "x.log")          # 실행 중인 척
    with pytest.raises(BusyError):
        refresh_corpus("mini", ops_db=tmp_path / "ops.db", log_dir=tmp_path / "logs",
                       argv=(sys.executable, "-c", "pass"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_runner.py -q`
Expected: FAIL — `ModuleNotFoundError` (runner 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_ops/runner.py`:

```python
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .corpora import CORPORA, REPO_ROOT, corpus_stats
from .db import DEFAULT_OPS_DB, any_running, connect_ops, finish_run, init_ops_schema, start_run

DEFAULT_LOG_DIR = Path("data/ops_logs")


class BusyError(RuntimeError):
    pass


def refresh_corpus(
    key: str, *, ops_db: Path = DEFAULT_OPS_DB, log_dir: Path = DEFAULT_LOG_DIR,
    argv: tuple | None = None, cwd: Path | None = None, timeout: int = 7200,
) -> int:
    c = CORPORA[key]
    conn = connect_ops(ops_db)
    init_ops_schema(conn)
    if any_running(conn):
        conn.close()
        raise BusyError("다른 수집이 실행 중입니다")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{key}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    before = corpus_stats(c)["total"] or 0
    run_id = start_run(conn, key, str(log_path))
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(
                list(argv or c.argv), stdout=fh, stderr=subprocess.STDOUT,
                env={**os.environ, **c.env}, cwd=str(cwd or REPO_ROOT), timeout=timeout,
            )
        after = corpus_stats(c)["total"] or 0
        if proc.returncode == 0:
            finish_run(conn, run_id, status="ok", new_count=after - before, total_after=after)
        else:
            finish_run(conn, run_id, status="error", new_count=after - before,
                       total_after=after, error=f"exit code {proc.returncode}")
    except Exception as exc:  # timeout 등 — 기록만 하고 raise하지 않음(백그라운드 안전)
        finish_run(conn, run_id, status="error", error=str(exc))
    conn.close()
    return run_id
```

`crawler/fino_ops/cli.py`:

```python
from __future__ import annotations

import argparse

from .corpora import CORPORA, corpus_stats
from .db import DEFAULT_OPS_DB, connect_ops, init_ops_schema, last_run
from .runner import BusyError, refresh_corpus


def cmd_status() -> int:
    conn = connect_ops(DEFAULT_OPS_DB)
    init_ops_schema(conn)
    print(f"{'corpus':<8} {'total':>10}  {'last_collected':<20} {'last_run'}")
    for key, c in CORPORA.items():
        s = corpus_stats(c)
        lr = last_run(conn, key)
        lr_txt = f"{lr['status']}({lr['new_count']}) {lr['finished_at'] or ''}" if lr else "-"
        print(f"{key:<8} {str(s['total'] or '-'):>10}  {str(s['last_collected'] or '-'):<20} {lr_txt}")
    conn.close()
    return 0


def cmd_refresh(corpus: str | None, run_all: bool) -> int:
    keys = list(CORPORA) if run_all else [corpus]
    rc = 0
    for key in keys:
        if key not in CORPORA:
            print(f"알 수 없는 코퍼스: {key}")
            return 1
        try:
            rid = refresh_corpus(key)
        except BusyError as exc:
            print(f"[busy] {exc}")
            return 1
        conn = connect_ops(DEFAULT_OPS_DB)
        lr = last_run(conn, key)
        conn.close()
        print(f"[{key}] run#{rid} {lr['status']} new={lr['new_count']} total={lr['total_after']}")
        if lr["status"] != "ok":
            rc = 1
    return rc


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    _ = sub.add_parser("status")
    rp = sub.add_parser("refresh")
    _ = rp.add_argument("--corpus", type=str, default=None)
    _ = rp.add_argument("--all", action="store_true")
    args = p.parse_args()
    if args.cmd == "status":
        return cmd_status()
    if not args.all and not args.corpus:
        p.error("refresh는 --corpus KEY 또는 --all 필요")
    return cmd_refresh(args.corpus, args.all)
```

`crawler/fino_ops/__main__.py`:

```python
from .cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_runner.py -q`
Expected: PASS (3 tests). `python -m crawler.fino_ops status`도 수동 1회 실행해 표가 나오는지 확인(실DB read-only라 안전).

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_ops/runner.py crawler/fino_ops/cli.py crawler/fino_ops/__main__.py tests/test_fino_ops_runner.py
git commit -m "feat(fino_ops): refresh runner(락·new_count·로그) + status/refresh CLI"
```

---

### Task 4: FastAPI 서버 — /api/corpora, refresh, runs, log, export

**Files:**
- Create: `crawler/fino_ops/api.py`
- Test: `tests/test_fino_ops_api.py`

**Interfaces:**
- Consumes: Task 1-3 전부
- Produces: `create_app(ops_db=DEFAULT_OPS_DB, export_dir=Path("data/export")) -> FastAPI`, 모듈 레벨 `app`(uvicorn 진입점: `.venv/bin/uvicorn crawler.fino_ops.api:app --host 127.0.0.1 --port 8500`)

- [ ] **Step 1: Write the failing test**

`tests/test_fino_ops_api.py`:

```python
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

import crawler.fino_ops.api as api_mod
import crawler.fino_ops.runner as runner_mod
from crawler.fino_ops.api import create_app
from crawler.fino_ops.corpora import Corpus
from crawler.fino_ops.db import connect_ops, init_ops_schema, start_run


def _setup(tmp_path: Path, monkeypatch) -> TestClient:
    db = tmp_path / "mini.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rows (v TEXT)")
    conn.commit()
    conn.close()
    c = Corpus(key="mini", label="미니", db_path=db,
               count_sql="SELECT COUNT(*) FROM rows",
               freshness_sql="SELECT MAX(v) FROM rows",
               argv=(sys.executable, "-c", "print('done')"))
    monkeypatch.setattr(api_mod, "CORPORA", {"mini": c})
    monkeypatch.setattr(runner_mod, "CORPORA", {"mini": c})
    monkeypatch.setattr(runner_mod, "DEFAULT_LOG_DIR", tmp_path / "logs")
    app = create_app(ops_db=tmp_path / "ops.db", export_dir=tmp_path / "export")
    return TestClient(app)


def test_corpora_listing(tmp_path: Path, monkeypatch) -> None:
    client = _setup(tmp_path, monkeypatch)
    body = client.get("/api/corpora").json()
    assert len(body) == 1
    assert body[0]["key"] == "mini" and body[0]["total"] == 0
    assert body[0]["busy"] is False and body[0]["last_run"] is None


def test_refresh_then_run_recorded(tmp_path: Path, monkeypatch) -> None:
    client = _setup(tmp_path, monkeypatch)
    r = client.post("/api/corpora/mini/refresh")
    assert r.status_code == 202 and r.json()["started"] is True
    runs = client.get("/api/runs").json()          # TestClient는 응답 후 bg task 완료
    assert runs[0]["corpus"] == "mini" and runs[0]["status"] == "ok"
    log = client.get(f"/api/runs/{runs[0]['id']}/log")
    assert "done" in log.text


def test_refresh_busy_409_and_unknown_404(tmp_path: Path, monkeypatch) -> None:
    client = _setup(tmp_path, monkeypatch)
    conn = connect_ops(tmp_path / "ops.db")
    init_ops_schema(conn)
    start_run(conn, "mini", "x.log")
    conn.close()
    assert client.post("/api/corpora/mini/refresh").status_code == 409
    assert client.post("/api/corpora/nope/refresh").status_code == 404


def test_export_serves_file_or_404(tmp_path: Path, monkeypatch) -> None:
    client = _setup(tmp_path, monkeypatch)
    assert client.get("/api/export/mini").status_code == 404
    (tmp_path / "export").mkdir(parents=True, exist_ok=True)
    (tmp_path / "export" / "mini.ndjson").write_text('{"a":1}\n', encoding="utf-8")
    r = client.get("/api/export/mini")
    assert r.status_code == 200 and r.text.startswith('{"a":1}')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_api.py -q`
Expected: FAIL — `ModuleNotFoundError` (api 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_ops/api.py`:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from . import runner
from .corpora import CORPORA, corpus_stats
from .db import (DEFAULT_OPS_DB, any_running, connect_ops, init_ops_schema,
                 last_run, mark_stale_running, recent_runs)

DEFAULT_EXPORT_DIR = Path("data/export")


def create_app(ops_db: Path = DEFAULT_OPS_DB,
               export_dir: Path = DEFAULT_EXPORT_DIR) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        n = mark_stale_running(conn)
        conn.close()
        if n:
            print(f"[fino_ops] stale running {n}건 error 처리")
        yield

    app = FastAPI(title="fino-ops", lifespan=lifespan)

    @app.get("/api/corpora")
    def corpora_list() -> list[dict]:
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        busy = any_running(conn)
        out = []
        for key, c in CORPORA.items():
            out.append({"key": key, "label": c.label, **corpus_stats(c),
                        "last_run": last_run(conn, key), "busy": busy})
        conn.close()
        return out

    @app.post("/api/corpora/{key}/refresh", status_code=202)
    def corpus_refresh(key: str, bg: BackgroundTasks) -> dict:
        if key not in CORPORA:
            raise HTTPException(404, f"알 수 없는 코퍼스: {key}")
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        busy = any_running(conn)
        conn.close()
        if busy:
            raise HTTPException(409, "다른 수집이 실행 중입니다")

        def _task() -> None:
            try:
                runner.refresh_corpus(key, ops_db=ops_db)
            except runner.BusyError:   # 동시 POST 경합 — runner가 최종 방어
                pass

        bg.add_task(_task)
        return {"started": True, "corpus": key}

    @app.get("/api/runs")
    def runs_list(limit: int = 50) -> list[dict]:
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        rows = recent_runs(conn, limit=limit)
        conn.close()
        return rows

    @app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
    def run_log(run_id: int, tail: int = 200) -> str:
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        row = conn.execute("SELECT log_path FROM runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
        if row is None:
            raise HTTPException(404, "run 없음")
        p = Path(row["log_path"])
        if not p.exists():
            raise HTTPException(404, "로그 파일 없음")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    @app.get("/api/export/{key}")
    def export_file(key: str) -> FileResponse:
        path = export_dir / f"{key}.ndjson"
        if not path.exists():
            raise HTTPException(404, f"export 없음: {path.name} (먼저 export 모듈로 생성)")
        return FileResponse(path, media_type="application/x-ndjson", filename=path.name)

    return app


app = create_app()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_api.py -q`
Expected: PASS (4 tests). 이후 전체: `.venv/bin/python -m pytest tests/test_fino_ops_*.py -q` 전부 PASS.

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_ops/api.py tests/test_fino_ops_api.py
git commit -m "feat(fino_ops): FastAPI — corpora/refresh(409)/runs/log tail/export"
```

---

### Task 5: Next.js 스캐폴드 + 프록시

**Files:**
- Create: `dashboard/` (create-next-app 산출물)
- Modify: `dashboard/next.config.ts`
- Modify: `.gitignore` (루트 — dashboard/node_modules, dashboard/.next 제외 확인)

**Interfaces:**
- Produces: `npm run dev`(:3000) → `/api/*`가 FastAPI(:8500)로 프록시되는 빈 Next.js 앱. 이후 Task 6이 페이지를 채움.

- [ ] **Step 1: Scaffold**

```bash
cd /data_raid/ruci_workspace/frwaler
npx --yes create-next-app@latest dashboard --ts --tailwind --app --eslint --no-src-dir --import-alias "@/*" --use-npm --yes --skip-install false
```
(프롬프트가 나오면 전부 기본값. turbopack 질문은 Yes 무방.)

- [ ] **Step 2: 프록시 설정**

`dashboard/next.config.ts` 전체를 다음으로 교체:

```typescript
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8500/api/:path*" },
    ];
  },
};

export default nextConfig;
```

- [ ] **Step 3: gitignore 확인**

`dashboard/.gitignore`가 node_modules/.next를 이미 제외하는지 확인(create-next-app 기본 포함). 루트 `.gitignore`에는 추가 불필요 — dashboard 내부 .gitignore가 적용됨을 `git status`로 확인 (node_modules가 untracked 목록에 안 떠야 함).

- [ ] **Step 4: Build gate**

Run: `cd dashboard && npm run build`
Expected: 빌드 성공(exit 0). 실패 시 원인 해결 전 진행 금지.

- [ ] **Step 5: Commit**

```bash
cd /data_raid/ruci_workspace/frwaler
git add dashboard
git commit -m "feat(dashboard): Next.js 스캐폴드 + /api → fino_ops(:8500) 프록시"
```

---

### Task 6: 대시보드 UI — 코퍼스 카드 / 이력 / 로그

**Files:**
- Modify: `dashboard/app/page.tsx` (전체 교체)
- Modify: `dashboard/app/layout.tsx` (metadata title만 "FINO Ops"로)

**Interfaces:**
- Consumes: Task 4 API 응답 형태 — `/api/corpora`: `[{key,label,total,last_collected,db_exists,last_run:{status,new_count,finished_at}|null,busy}]`, `/api/runs`: `[{id,corpus,started_at,finished_at,status,new_count,total_after}]`, `/api/runs/{id}/log`(text), `POST /api/corpora/{key}/refresh`(202/409)

- [ ] **Step 1: page.tsx 전체 교체**

`dashboard/app/page.tsx`:

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";

type LastRun = {
  status: string; new_count: number | null; finished_at: string | null;
} | null;
type CorpusCard = {
  key: string; label: string; total: number | null;
  last_collected: string | null; db_exists: boolean;
  last_run: LastRun; busy: boolean;
};
type Run = {
  id: number; corpus: string; started_at: string; finished_at: string | null;
  status: string; new_count: number | null; total_after: number | null;
};

function freshness(last: string | null): { dot: string; text: string } {
  if (!last) return { dot: "bg-gray-400", text: "수집 이력 없음" };
  const days = (Date.now() - new Date(last.replace(" ", "T")).getTime()) / 86400000;
  if (days < 7) return { dot: "bg-green-500", text: "최신" };
  if (days < 30) return { dot: "bg-yellow-500", text: `${Math.floor(days)}일 경과` };
  return { dot: "bg-red-500", text: `${Math.floor(days)}일 경과` };
}

export default function Home() {
  const [corpora, setCorpora] = useState<CorpusCard[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [logRunId, setLogRunId] = useState<number | null>(null);
  const [logText, setLogText] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [c, r] = await Promise.all([
        fetch("/api/corpora").then((x) => x.json()),
        fetch("/api/runs?limit=30").then((x) => x.json()),
      ]);
      setCorpora(c);
      setRuns(r);
      setError("");
    } catch {
      setError("API 서버(:8500)에 연결할 수 없습니다");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    if (logRunId === null) return;
    const fetchLog = () =>
      fetch(`/api/runs/${logRunId}/log?tail=200`)
        .then((x) => (x.ok ? x.text() : "(로그 없음)"))
        .then(setLogText);
    fetchLog();
    const t = setInterval(fetchLog, 3000);
    return () => clearInterval(t);
  }, [logRunId]);

  const refresh = async (key: string) => {
    const r = await fetch(`/api/corpora/${key}/refresh`, { method: "POST" });
    if (r.status === 409) setError("다른 수집이 실행 중입니다");
    load();
  };

  const anyBusy = corpora.some((c) => c.busy);

  return (
    <main className="mx-auto max-w-6xl p-8 font-sans">
      <h1 className="mb-1 text-2xl font-bold">FINO Ops — 코퍼스 최신화</h1>
      <p className="mb-6 text-sm text-gray-500">
        {anyBusy ? "수집 실행 중…" : "대기 중"} · 5초마다 갱신
      </p>
      {error && <p className="mb-4 rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {corpora.map((c) => {
          const f = freshness(c.last_collected);
          return (
            <div key={c.key} className="rounded-lg border p-4 shadow-sm">
              <div className="flex items-center justify-between">
                <span className="font-semibold">{c.label}</span>
                <span className={`h-3 w-3 rounded-full ${f.dot}`} title={f.text} />
              </div>
              <p className="mt-2 text-2xl font-bold">
                {c.total?.toLocaleString() ?? "—"}
              </p>
              <p className="text-xs text-gray-500">
                마지막 수집 {c.last_collected ?? "—"} · {f.text}
              </p>
              <p className="text-xs text-gray-500">
                최근 실행:{" "}
                {c.last_run
                  ? `${c.last_run.status} (+${c.last_run.new_count ?? 0})`
                  : "—"}
              </p>
              <button
                onClick={() => refresh(c.key)}
                disabled={anyBusy || !c.db_exists}
                className="mt-3 w-full rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:bg-gray-300"
              >
                {anyBusy ? "실행 중…" : "Refresh"}
              </button>
            </div>
          );
        })}
      </div>

      <h2 className="mt-10 mb-3 text-lg font-semibold">실행 이력</h2>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b text-left text-gray-500">
            <th className="py-1">#</th><th>코퍼스</th><th>시작</th>
            <th>상태</th><th>신규</th><th>총계</th><th>로그</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className="border-b">
              <td className="py-1">{r.id}</td>
              <td>{r.corpus}</td>
              <td>{r.started_at}</td>
              <td className={r.status === "ok" ? "text-green-600"
                : r.status === "running" ? "text-blue-600" : "text-red-600"}>
                {r.status}
              </td>
              <td>{r.new_count ?? "—"}</td>
              <td>{r.total_after?.toLocaleString() ?? "—"}</td>
              <td>
                <button className="text-blue-600 underline" onClick={() => setLogRunId(r.id)}>
                  보기
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {logRunId !== null && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-8"
             onClick={() => setLogRunId(null)}>
          <div className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded bg-gray-900 p-4"
               onClick={(e) => e.stopPropagation()}>
            <div className="mb-2 flex justify-between text-sm text-gray-300">
              <span>run #{logRunId} 로그 (3초 갱신)</span>
              <button onClick={() => setLogRunId(null)}>닫기 ✕</button>
            </div>
            <pre className="whitespace-pre-wrap text-xs text-green-300">{logText}</pre>
          </div>
        </div>
      )}
    </main>
  );
}
```

- [ ] **Step 2: layout.tsx metadata 수정**

`dashboard/app/layout.tsx`의 `metadata`를 다음으로 교체(그 외 유지):

```tsx
export const metadata: Metadata = {
  title: "FINO Ops",
  description: "FINO 코퍼스 최신화 관리 대시보드",
};
```

- [ ] **Step 3: Build gate**

Run: `cd dashboard && npm run build`
Expected: 빌드 성공. lint 에러 있으면 해결.

- [ ] **Step 4: Commit**

```bash
cd /data_raid/ruci_workspace/frwaler
git add dashboard/app/page.tsx dashboard/app/layout.tsx
git commit -m "feat(dashboard): 코퍼스 카드·실행 이력·로그 뷰 — 5s 폴링, refresh 트리거"
```

---

### Task 7: 실구동 E2E 검증 + 문서/메모리 갱신

**Files:**
- Modify: `docs/2026-07-02_fino_corpus_crawler_handoff.md` (§5 액션 순서에 fino_ops 항목 추가)

- [ ] **Step 1: 서버 기동**

```bash
cd /data_raid/ruci_workspace/frwaler
.venv/bin/uvicorn crawler.fino_ops.api:app --host 127.0.0.1 --port 8500 > data/ops_logs/api.log 2>&1 &
cd dashboard && npm run dev > ../data/ops_logs/next.log 2>&1 &
sleep 8
```

- [ ] **Step 2: API 스모크 (실DB read-only)**

```bash
curl -s http://127.0.0.1:8500/api/corpora | .venv/bin/python -m json.tool | head -30
```
Expected: 6코퍼스, law 6507·exec 2799·acct 4540·std 21375·nts_qt 139617+·nts_pd 151041+ 근사값, busy=false.

- [ ] **Step 3: 실 refresh 1회 (최단 코퍼스 = nts_qt 증분, 오늘 이미 최신이라 수 분·신규 0~수건)**

```bash
curl -s -X POST http://127.0.0.1:8500/api/corpora/nts_qt/refresh
# 완료 대기 후:
curl -s "http://127.0.0.1:8500/api/runs?limit=3" | .venv/bin/python -m json.tool
```
Expected: 202 → run status=ok, new_count ≥ 0. 실행 중 재-POST 시 409도 확인.

- [ ] **Step 4: 브라우저 수동 체크리스트** (http://localhost:3000)

- [ ] 카드 6개 표시, 총계·신선도 배지 정상 (오늘 수집분은 🟢)
- [ ] Refresh 버튼 → 상단 "수집 실행 중…" + 이력에 running 행
- [ ] 완료 후 new_count 반영, 로그 "보기" 모달 정상
- [ ] FastAPI만 죽였을 때 에러 배너 표시

- [ ] **Step 5: 전체 테스트 + 문서 갱신 + 커밋**

Run: `.venv/bin/python -m pytest tests/test_fino_ops_*.py tests/test_fino_std_*.py tests/test_fino_law_*.py tests/test_fino_acct_collector.py -q`
Expected: 전부 PASS.

핸드오프 문서 §5에 추가: "최신화 관리: `python -m crawler.fino_ops status|refresh` 또는 uvicorn(:8500)+dashboard(:3000) — 스펙 `2026-07-02-fino-ops-dashboard-design.md`".

```bash
git add docs/2026-07-02_fino_corpus_crawler_handoff.md
git commit -m "docs(fino_ops): 핸드오프에 통합 CLI·대시보드 사용법 추가"
```

- [ ] **Step 6: 메모리 갱신**

`fino-std-standards-crawler` 패턴대로 `fino-ops-dashboard` 메모리 신규(포트·실행법·구조) + MEMORY.md 인덱스 한 줄. (컨트롤러가 직접 수행)

---

## Self-Review 결과

- **스펙 커버리지**: 상태DB(§데이터 모델)=Task 1, 레지스트리(§레지스트리 계약)=Task 2, runner·CLI(§아키텍처)=Task 3, API 5종(§API)=Task 4, UI(§대시보드 UI)=Task 5-6, E2E·운영(§테스트)=Task 7. 물리 통합 제외·FINO 1단계 소비는 스펙 기록 사항으로 코드 태스크 없음 — 갭 없음.
- **플레이스홀더 스캔**: 모든 코드 스텝에 전체 코드 포함. "적절히 처리" 류 문구 없음.
- **타입 일관성**: `corpus_stats` 반환 dict 키(total/last_collected/db_exists)가 Task 2 구현·테스트, Task 4 API 병합(`**corpus_stats(c)`), Task 6 `CorpusCard` 타입과 일치. `refresh_corpus` 시그니처가 Task 3 테스트·Task 4 `_task()` 호출과 일치. runs 컬럼명이 Task 1 스키마·Task 4 응답·Task 6 `Run` 타입과 일치.
- 주의 기록: Task 4 테스트는 TestClient가 응답 후 BackgroundTasks를 동기 실행하는 성질에 의존(FastAPI 표준 동작). Task 6 `new Date(...)` 파싱은 sqlite `datetime('now','localtime')` 포맷("YYYY-MM-DD HH:MM:SS") 전제 — freshness()에서 "T" 치환으로 처리.
