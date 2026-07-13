from __future__ import annotations

import sqlite3

from .parse import case_year


def resolve_delete_case(conn: sqlite3.Connection, case_number: str,
                        case_date: str) -> list[tuple[str, str]]:
    """삭제사례 번호를 doc_index.doc_number 정확일치로 매칭.
    다건이면 case_date 연도로 좁히고, 안 좁혀지면 전부 반환(보수적 제외)."""
    rows = conn.execute(
        "SELECT doc_id, title, published_date FROM doc_index WHERE doc_number = ?",
        (case_number,),
    ).fetchall()
    if not rows:
        return []
    if len(rows) > 1:
        year = case_year(case_date)
        if year:
            narrowed = [r for r in rows if str(r["published_date"] or "")[:4] == year]
            if narrowed:
                rows = narrowed
    return [(r["doc_id"], r["title"]) for r in rows]
