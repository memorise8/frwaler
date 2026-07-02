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
