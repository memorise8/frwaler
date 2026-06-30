import json
from pathlib import Path

from crawler.fino_law.db import connect_db, init_schema, upsert_article, upsert_document
from crawler.fino_law.export_markdown import export_markdown
from crawler.fino_law.export_ndjson import export_ndjson


def _seed(db_path: Path) -> None:
    with connect_db(db_path) as conn:
        init_schema(conn)
        doc_id = upsert_document(
            conn, source_kind="law", external_id="001563", title="법인세법",
            category="법률", org="기획재정부", promulgated_at="20251001",
            effective_at="20260102", version_code="현행",
            source_url="https://www.law.go.kr/법령/법인세법",
        )
        upsert_article(
            conn, document_id=doc_id, article_no="제1조", article_title="목적",
            body_text="이 법은 ...", clause_json="[]",
            source_url="https://www.law.go.kr/법령/법인세법#제1조", seq=1,
        )


def test_export_markdown_writes_one_file_per_document(tmp_path: Path) -> None:
    db_path = tmp_path / "law.db"
    _seed(db_path)
    out = tmp_path / "md"
    count = export_markdown(db_path=db_path, out_dir=out)
    assert count == 1
    files = list(out.rglob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "source_url:" in text  # frontmatter
    assert "제1조" in text


def test_export_ndjson_writes_one_record_per_article(tmp_path: Path) -> None:
    db_path = tmp_path / "law.db"
    _seed(db_path)
    out = tmp_path / "law.ndjson"
    count = export_ndjson(db_path=db_path, out_path=out)
    assert count == 1
    line = out.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["source_url"] == "https://www.law.go.kr/법령/법인세법#제1조"
    assert rec["source_kind"] == "law"
    assert rec["doc_title"] == "법인세법"
    assert rec["article_no"] == "제1조"
