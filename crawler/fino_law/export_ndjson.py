from __future__ import annotations

import json
from pathlib import Path

from .db import connect_db


def export_ndjson(*, db_path: Path, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with connect_db(db_path) as conn, out_path.open("w", encoding="utf-8") as fh:
        rows = conn.execute(
            """
            SELECT d.source_kind, d.external_id, d.title AS doc_title, d.org,
                   d.effective_at, a.article_no, a.article_title, a.body_text, a.source_url
            FROM articles a JOIN documents d ON d.id = a.document_id
            ORDER BY d.id, a.seq
            """
        ).fetchall()
        for r in rows:
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            count += 1
    return count
