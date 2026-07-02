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
