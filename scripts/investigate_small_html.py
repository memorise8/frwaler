#!/usr/bin/env python3
"""Phase 7: sample qt raw-html files smaller than N bytes and dump their
content so a human can decide whether they are genuinely empty upstream
or a parser artefact.

Writes ``docs/small_html_survey.md`` with a table summarising samples.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
HTML_ROOT = ROOT / "data" / "exports"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-id", default="nts-taxlaw-qt")
    ap.add_argument("--threshold", type=int, default=500,
                    help="Byte threshold; files strictly smaller are sampled")
    ap.add_argument("--sample", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20260423)
    ap.add_argument("--out", default="docs/small_html_survey.md")
    args = ap.parse_args()

    html_dir = HTML_ROOT / args.site_id / "_html"
    if not html_dir.is_dir():
        print(f"no such dir: {html_dir}")
        return

    small_files = [p for p in html_dir.iterdir()
                   if p.is_file() and p.stat().st_size < args.threshold]
    print(f"total small (<{args.threshold}B): {len(small_files)}")
    if not small_files:
        return

    rng = random.Random(args.seed)
    sample = rng.sample(small_files, min(args.sample, len(small_files)))
    sample.sort(key=lambda p: p.stat().st_size)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    lines: list[str] = []
    lines.append(f"# qt small HTML 조사 ({args.site_id}, <{args.threshold}B)")
    lines.append("")
    lines.append(f"- 대상 파일 총 {len(small_files)}개 중 "
                 f"{len(sample)}개 무작위 샘플")
    lines.append(f"- 시드: {args.seed}")
    lines.append("")
    lines.append("| # | DOC_ID | bytes | title | abstract_len | content(head 120) |")
    lines.append("|---|--------|------:|-------|-------------:|-------------------|")

    for i, p in enumerate(sample, 1):
        doc_id = p.stem
        size = p.stat().st_size
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            body = f"(read failed: {e})"
        head = " ".join(body.split())[:120].replace("|", "\\|")
        row = conn.execute(
            "SELECT title, length(abstract) AS abs_len "
            "FROM papers WHERE site_id=? AND external_id=?",
            (args.site_id, doc_id),
        ).fetchone()
        title = (row["title"] if row else "(not in DB)") or ""
        abs_len = row["abs_len"] if row else 0
        title = title[:50].replace("|", "\\|")
        lines.append(f"| {i} | {doc_id} | {size} | {title} | {abs_len} | {head} |")

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out_path.relative_to(ROOT)}")

    conn.close()


if __name__ == "__main__":
    main()
