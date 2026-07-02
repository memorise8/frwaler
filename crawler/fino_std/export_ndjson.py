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
            SELECT d.std_num, d.std_type, d.title AS doc_title,
                   p.para_num, p.section_path, p.body_text, p.source_url
            FROM paragraphs p JOIN documents d ON d.id = p.document_id
            ORDER BY d.std_num, p.seq
            """
        ).fetchall()
        for r in rows:
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            count += 1
    return count
