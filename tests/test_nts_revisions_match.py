from pathlib import Path

from crawler.nts_revisions.db import connect_db
from crawler.nts_revisions.match import resolve_delete_case


def _conn_with_docindex(tmp_path: Path):
    conn = connect_db(tmp_path / "d.db")
    conn.executescript(
        """CREATE TABLE doc_index (doc_id TEXT, doc_number TEXT, title TEXT, published_date TEXT);
           INSERT INTO doc_index VALUES
             ('D-2000','법인46012-1784','2000년 해석','2000-08-19'),
             ('D-1998','법인46012-1784','1998년 해석','1998-07-02'),
             ('D-1994','법인46012-1784','1994년 해석','1994-06-21'),
             ('D-uniq','재산세과-271','유일 해석','2010-05-04');""")
    conn.commit()
    return conn


def test_single_match(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    assert resolve_delete_case(conn, "재산세과-271", "2010.05.04.") == [("D-uniq", "유일 해석")]


def test_year_disambiguation(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    # 3건 중 2000년만 선택
    assert resolve_delete_case(conn, "법인46012-1784", "2000.08.19.") == [("D-2000", "2000년 해석")]


def test_no_year_returns_all_conservative(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    got = resolve_delete_case(conn, "법인46012-1784", "")
    assert {e for e, _ in got} == {"D-2000", "D-1998", "D-1994"}   # 연도 없으면 전부(보수적)


def test_no_match(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    assert resolve_delete_case(conn, "없는번호-999", "2020.01.01.") == []
