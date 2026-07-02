from __future__ import annotations

from pathlib import Path

from .db import connect_db

_TYPE_LABEL = {"kifrs": "K-IFRS", "kifrs_interp": "K-IFRS 해석서",
               "kifrs_etc": "K-IFRS 기타", "gaap": "일반기업회계기준"}


def export_markdown(*, db_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with connect_db(db_path) as conn:
        docs = conn.execute("SELECT * FROM documents ORDER BY std_num").fetchall()
        for d in docs:
            paras = conn.execute(
                "SELECT * FROM paragraphs WHERE document_id = ? ORDER BY seq", (d["id"],)
            ).fetchall()
            label = _TYPE_LABEL.get(d["std_type"], d["std_type"])
            lines = [f"# [{label} {d['std_num']}] {d['title']}", "",
                     f"> 출처: {d['source_url']} (수집 {d['collected_at']})", ""]
            current_path = None
            for p in paras:
                if p["section_path"] != current_path:
                    current_path = p["section_path"]
                    lines += [f"## {current_path}", ""]
                head = f"**{p['para_num']}** " if p["para_num"] else ""
                lines += [f"{head}{p['body_text']}", f"[{p['source_url']}]({p['source_url']})", ""]
            name = f"{d['std_type']}_{d['std_num']}_{d['title'].replace('/', '·')}.md"
            (out_dir / name).write_text("\n".join(lines), encoding="utf-8")
            count += 1
    return count
