from pathlib import Path

from crawler.fino_std.db import connect_db, init_schema, replace_paragraphs, upsert_document
from crawler.fino_std.models import ParagraphRecord


def _para(num: str, seq: int, text: str = "본문") -> ParagraphRecord:
    return ParagraphRecord(para_num=num, section_path="목적", body_html=f"<div>{text}</div>",
                           body_text=text, seq=seq, source_url=f"https://db.kasb.or.kr/s/1001/{num}")


def test_upsert_document_idempotent(tmp_path: Path) -> None:
    with connect_db(tmp_path / "t.db") as conn:
        init_schema(conn)
        a = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시",
                            source_url="https://db.kasb.or.kr/s/1001")
        b = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시(개정)",
                            source_url="https://db.kasb.or.kr/s/1001")
        assert a == b
        row = conn.execute("SELECT title FROM documents WHERE id = ?", (a,)).fetchone()
        assert row["title"] == "재무제표 표시(개정)"


def test_replace_paragraphs_swaps_cleanly(tmp_path: Path) -> None:
    with connect_db(tmp_path / "t.db") as conn:
        init_schema(conn)
        doc = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시",
                              source_url="https://db.kasb.or.kr/s/1001")
        assert replace_paragraphs(conn, doc, [_para("1", 0), _para("2", 1)]) == 2
        # 재수집: 문단 구성이 바뀌어도 잔재 없이 교체된다
        assert replace_paragraphs(conn, doc, [_para("1", 0, "개정 본문")]) == 1
        rows = conn.execute(
            "SELECT para_num, body_text FROM paragraphs WHERE document_id = ? ORDER BY seq", (doc,)
        ).fetchall()
        assert [(r["para_num"], r["body_text"]) for r in rows] == [("1", "개정 본문")]
