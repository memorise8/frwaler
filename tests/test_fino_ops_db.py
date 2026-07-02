from pathlib import Path

import pytest
import sqlite3

from crawler.fino_ops.db import (
    any_running, connect_ops, finish_run, init_ops_schema,
    last_run, mark_stale_running, recent_runs, start_run, try_start_run,
)


def _conn(tmp_path: Path):
    conn = connect_ops(tmp_path / "ops.db")
    init_ops_schema(conn)
    return conn


def test_default_ops_db_is_repo_anchored() -> None:
    from crawler.fino_ops.db import DEFAULT_OPS_DB
    assert DEFAULT_OPS_DB.is_absolute()
    assert DEFAULT_OPS_DB.parts[-2:] == ("data", "fino_ops.db")


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


def test_status_check_constraint_rejects_typo(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = start_run(conn, "std", "s.log")
    with pytest.raises(sqlite3.IntegrityError):
        finish_run(conn, rid, status="okk")


def test_try_start_run_atomic_claim(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = try_start_run(conn, "std", "a.log")
    assert rid is not None
    assert try_start_run(conn, "law", "b.log") is None      # 실행 중이면 획득 실패
    finish_run(conn, rid, status="ok", new_count=0, total_after=0)
    assert try_start_run(conn, "law", "c.log") is not None  # 종료 후 획득 가능
