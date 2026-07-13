import json
from pathlib import Path

from crawler.nts_revisions.db import connect_db, count_excluded, init_schema
from crawler.nts_revisions.load import export_excluded, export_history, load
from crawler.nts_revisions.models import Revision


def _conn_with_docindex(tmp_path: Path):
    conn = connect_db(tmp_path / "d.db")
    init_schema(conn)
    conn.executescript(
        """CREATE TABLE doc_index (doc_id TEXT, doc_number TEXT, title TEXT, published_date TEXT);
           INSERT INTO doc_index VALUES
             ('D-2000','법인46012-1784','2000년 해석','2000-08-19'),
             ('D-uniq','재산세과-271','유일 해석','2010-05-04');"""
    )
    conn.commit()
    return conn


def _rev() -> Revision:
    return Revision(seq=996, tax_category="상증", summary="사망보험금...", reason="",
                    revision_reason="심의 변경", registered_at="2026.06.26.",
                    keep_cases=(("사전-2014-법령해석재산-20405", "2015.07.13."),),
                    delete_cases=(("재산세과-271", "2010.05.04."), ("법인46012-1784", "2000.08.19.")),
                    source_file="nts_new.xlsx")


def test_load_populates_and_matches(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    stats = load(conn, [_rev()])
    assert stats["revisions"] == 1
    assert stats["excluded"] == 2                     # 두 삭제사례 모두 매칭
    assert count_excluded(conn) == 2
    ids = {r["external_id"] for r in conn.execute("SELECT external_id FROM nts_excluded_docs")}
    assert ids == {"D-uniq", "D-2000"}


def test_load_idempotent_by_source(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    load(conn, [_rev()])
    load(conn, [_rev()])                              # 재적재
    assert count_excluded(conn) == 2                  # 중복 안 쌓임
    assert conn.execute("SELECT COUNT(*) FROM nts_revisions").fetchone()[0] == 1


def test_export(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    load(conn, [_rev()])
    ex = tmp_path / "excluded.ndjson"
    hi = tmp_path / "history.ndjson"
    assert export_excluded(conn, ex) == 2
    assert export_history(conn, hi) == 1
    first = json.loads(ex.read_text(encoding="utf-8").splitlines()[0])
    assert "external_id" in first and "revision_reason" in first
    h0 = json.loads(hi.read_text(encoding="utf-8").splitlines()[0])
    assert h0["seq"] == 996 and h0["tax_category"] == "상증"
