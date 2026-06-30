from pathlib import Path
import sqlite3
import subprocess
import sys

from crawler.fino_law.db import connect_db, init_schema, upsert_document, upsert_article


def _dump(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def test_upsert_document_then_article_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "law.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        for _ in range(2):  # 두 번 실행해도 row 수 불변(멱등)
            doc_id = upsert_document(
                conn,
                source_kind="law",
                external_id="001563",
                title="법인세법",
                category="법률",
                org="기획재정부",
                promulgated_at="20251001",
                effective_at="20260102",
                version_code="현행",
                source_url="https://www.law.go.kr/법령/법인세법",
            )
            upsert_article(
                conn,
                document_id=doc_id,
                article_no="제1조",
                article_title="목적",
                body_text="이 법은 ...",
                clause_json="[]",
                source_url="https://www.law.go.kr/법령/법인세법#제1조",
                seq=1,
            )
        dump = _dump(conn)

    assert dump.count("'법인세법'") == 1
    assert dump.count("'제1조'") == 1


def test_cli_help_when_invoked_as_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "crawler.fino_law.collect", "--help"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--db-path" in result.stdout
    assert "--law" in result.stdout
