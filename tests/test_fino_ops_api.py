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
