from pathlib import Path

from crawler.nts_revisions.db import (
    connect_db, count_excluded, delete_by_source, init_schema, insert_case,
    insert_revision, iter_excluded, iter_history, upsert_excluded,
)


def _conn(tmp_path: Path):
    c = connect_db(tmp_path / "d.db")
    init_schema(c)
    return c


def test_insert_and_history(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = insert_revision(conn, seq=996, tax_category="상증", summary="사망보험금...",
                          revision_reason="심의 변경", registered_at="2026.06.26.",
                          source_file="nts_new.xlsx")
    insert_case(conn, revision_id=rid, role="delete", case_number="재산세과-271",
                case_date="2010.05.04.", matched_doc_id="010000000000058132")
    insert_case(conn, revision_id=rid, role="keep", case_number="사전-2014-법령해석재산-20405",
                case_date="2015.07.13.", matched_doc_id=None)
    hist = iter_history(conn)
    assert len(hist) == 1 and hist[0]["seq"] == 996 and hist[0]["tax_category"] == "상증"


def test_upsert_excluded_idempotent(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = insert_revision(conn, seq=1, tax_category="법인", summary="x",
                          revision_reason="y", registered_at="2026.01.01.", source_file="a.xlsx")
    upsert_excluded(conn, external_id="E1", revision_id=rid, doc_number="법인46012-1784",
                    title="업무무관가지급금...", revision_reason="y", registered_at="2026.01.01.")
    upsert_excluded(conn, external_id="E1", revision_id=rid, doc_number="법인46012-1784",
                    title="업무무관가지급금...", revision_reason="y", registered_at="2026.01.01.")
    assert count_excluded(conn) == 1                       # 같은 external_id 재등재 → 1건
    rows = iter_excluded(conn)
    assert rows[0]["external_id"] == "E1" and rows[0]["doc_number"] == "법인46012-1784"


def test_delete_by_source_replaces(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = insert_revision(conn, seq=1, tax_category="법인", summary="x", revision_reason="y",
                          registered_at="2026.01.01.", source_file="a.xlsx")
    upsert_excluded(conn, external_id="E1", revision_id=rid, doc_number="D1", title="t",
                    revision_reason="y", registered_at="2026.01.01.")
    delete_by_source(conn, "a.xlsx")
    assert count_excluded(conn) == 0
    assert iter_history(conn) == []
