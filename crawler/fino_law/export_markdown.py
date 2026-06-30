from __future__ import annotations

from pathlib import Path

from .db import connect_db


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " _-()가-힣").strip().replace(" ", "_")


def export_markdown(*, db_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with connect_db(db_path) as conn:
        for doc in conn.execute("SELECT * FROM documents ORDER BY id").fetchall():
            arts = conn.execute(
                "SELECT * FROM articles WHERE document_id = ? ORDER BY seq", (doc["id"],)
            ).fetchall()
            lines = [
                "---",
                f"title: {doc['title']}",
                f"source_kind: {doc['source_kind']}",
                f"external_id: {doc['external_id']}",
                f"category: {doc['category']}",
                f"org: {doc['org']}",
                f"effective_at: {doc['effective_at']}",
                f"source_url: {doc['source_url']}",
                "---",
                "",
                f"# {doc['title']}",
                "",
            ]
            for a in arts:
                heading = f"## {a['article_no']}"
                if a["article_title"]:
                    heading += f"({a['article_title']})"
                lines.append(heading)
                lines.append("")
                lines.append(a["body_text"])
                lines.append("")
            sub = out_dir / doc["source_kind"]
            sub.mkdir(parents=True, exist_ok=True)
            (sub / f"{_safe(doc['title'])}.md").write_text("\n".join(lines), encoding="utf-8")
            count += 1
    return count
