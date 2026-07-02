from pathlib import Path

from crawler.fino_std.db import connect_db, init_schema, replace_paragraphs, upsert_document
from crawler.fino_std.export_markdown import export_markdown
from crawler.fino_std.export_ndjson import export_ndjson
from crawler.fino_std.models import ParagraphRecord


def _seed_db(db: Path) -> None:
    with connect_db(db) as conn:
        init_schema(conn)
        doc = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시",
                              source_url="https://db.kasb.or.kr/s/1001")
        replace_paragraphs(conn, doc, [
            ParagraphRecord(para_num="1", section_path="목적", body_html="<div>목적 본문</div>",
                            body_text="목적 본문", seq=0,
                            source_url="https://db.kasb.or.kr/s/1001/1"),
            ParagraphRecord(para_num="9", section_path="재무제표 > 재무제표의 목적",
                            body_html="<div>재무제표는…</div>", body_text="재무제표는…", seq=1,
                            source_url="https://db.kasb.or.kr/s/1001/9"),
        ])


def test_export_markdown(tmp_path: Path) -> None:
    db = tmp_path / "std.db"
    _seed_db(db)
    n = export_markdown(db_path=db, out_dir=tmp_path / "md")
    assert n == 1
    text = (tmp_path / "md" / "kifrs_1001_재무제표_표시.md").read_text(encoding="utf-8")
    assert "# [K-IFRS 1001] 재무제표 표시" in text
    assert "## 재무제표 > 재무제표의 목적" in text     # 섹션경로가 헤더로
    assert "**9** 재무제표는…" in text
    assert "https://db.kasb.or.kr/s/1001/9" in text


def test_export_markdown_sanitizes_special_chars_in_title(tmp_path: Path) -> None:
    db = tmp_path / "std.db"
    with connect_db(db) as conn:
        init_schema(conn)
        doc = upsert_document(conn, std_num=1109, std_type="kifrs", title="금융상품: 표시/테스트",
                              source_url="https://db.kasb.or.kr/s/1109")
        replace_paragraphs(conn, doc, [
            ParagraphRecord(para_num="1", section_path="목적", body_html="<div>본문</div>",
                            body_text="본문", seq=0,
                            source_url="https://db.kasb.or.kr/s/1109/1"),
        ])
    n = export_markdown(db_path=db, out_dir=tmp_path / "md")
    assert n == 1
    files = list((tmp_path / "md").glob("kifrs_1109_*.md"))
    assert len(files) == 1
    assert ":" not in files[0].name
    assert "/" not in files[0].name


def test_export_ndjson(tmp_path: Path) -> None:
    import json
    db = tmp_path / "std.db"
    _seed_db(db)
    out = tmp_path / "std.ndjson"
    assert export_ndjson(db_path=db, out_path=out) == 2
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["std_num"] == 1001 and rows[0]["para_num"] == "1"
    assert rows[1]["source_url"].endswith("/s/1001/9")
