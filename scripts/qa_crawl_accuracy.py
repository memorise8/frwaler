#!/usr/bin/env python3
"""QA: per-site crawl accuracy report.

Samples up to ``--limit`` rows per site from ``documents`` and reports:

  - meta completeness (title / published_date / pdf_url / abstract非빈)
  - download success rate (download_status='downloaded')
  - convert success rate (txt_path 존재 + 파일 실재)
  - summary 생성률

Output is a per-site table to stdout, plus an optional Excel workbook
(``--xlsx PATH``) with one sheet per site for issue tracking.

Usage:
    python -m scripts.qa_crawl_accuracy [--db PATH] [--site SITE_ID] [--limit N] [--xlsx OUT.xlsx]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "papers.db"

sys.path.insert(0, str(ROOT))


def _has_value(s) -> bool:
    return bool(s) and bool(str(s).strip())


def _audit_rows(rows: Iterable[sqlite3.Row]) -> dict:
    from crawler import storage as _st

    n = 0
    title_ok = 0
    pubdate_ok = 0
    pdfurl_ok = 0
    abstract_ok = 0
    downloaded = 0
    converted = 0
    summarized = 0
    txt_missing = 0  # txt_path set in DB but file gone

    for r in rows:
        n += 1
        if _has_value(r["title"]):
            title_ok += 1
        if _has_value(r["published_date"]):
            pubdate_ok += 1
        if _has_value(r["pdf_url"]):
            pdfurl_ok += 1
        if _has_value(r["abstract"]):
            abstract_ok += 1
        if (r["download_status"] or "") == "downloaded":
            downloaded += 1
        if _has_value(r["txt_path"]):
            converted += 1
            try:
                p = _st.doc_id_to_txt_path(r["id"])
                if not p.exists() or p.stat().st_size == 0:
                    txt_missing += 1
            except Exception:
                txt_missing += 1
        if _has_value(r["summary"]):
            summarized += 1

    return {
        "sampled": n,
        "title": title_ok,
        "published_date": pubdate_ok,
        "pdf_url": pdfurl_ok,
        "abstract": abstract_ok,
        "downloaded": downloaded,
        "converted": converted,
        "summarized": summarized,
        "txt_missing": txt_missing,
    }


def _print_table(report: dict[str, dict]) -> None:
    cols = [
        "sampled",
        "title", "published_date", "pdf_url", "abstract",
        "downloaded", "converted", "summarized", "txt_missing",
    ]
    header = ["site"] + cols
    widths = [max(len(h), 14) for h in header]
    print(" | ".join(h.ljust(w) for h, w in zip(header, widths)))
    print("-+-".join("-" * w for w in widths))
    for site, m in report.items():
        row = [site] + [
            f"{m[c]}/{m['sampled']} ({m[c]*100//max(1,m['sampled'])}%)"
            if c != "sampled" else str(m["sampled"])
            for c in cols
        ]
        print(" | ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def _write_xlsx(report: dict[str, dict], rows_by_site: dict[str, list[sqlite3.Row]], out: Path) -> None:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        print("[xlsx] openpyxl not installed — pip install openpyxl", file=sys.stderr)
        return

    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = "summary"
    cols = ["site", "sampled", "title%", "published_date%", "pdf_url%",
            "abstract%", "downloaded%", "converted%", "summarized%", "txt_missing"]
    summary.append(cols)
    for site, m in report.items():
        n = m["sampled"] or 1
        summary.append([
            site, m["sampled"],
            m["title"] * 100 // n, m["published_date"] * 100 // n,
            m["pdf_url"] * 100 // n, m["abstract"] * 100 // n,
            m["downloaded"] * 100 // n, m["converted"] * 100 // n,
            m["summarized"] * 100 // n, m["txt_missing"],
        ])

    for site, rows in rows_by_site.items():
        ws = wb.create_sheet(title=site[:30])  # Excel sheet name max 31 chars
        ws.append(["id", "external_id", "title", "published_date", "pdf_url",
                   "download_status", "txt_path", "summary_len"])
        for r in rows:
            ws.append([
                r["id"], r["external_id"] or "", (r["title"] or "")[:200],
                r["published_date"] or "", r["pdf_url"] or "",
                r["download_status"] or "", r["txt_path"] or "",
                len(r["summary"] or ""),
            ])
    wb.save(str(out))
    print(f"[xlsx] wrote {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--site", default=None,
                        help="Audit only this site_id (default: all sites with data)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max rows to sample per site (default 100)")
    parser.add_argument("--xlsx", default=None,
                        help="Optional Excel report path")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[error] DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if args.site:
        site_ids = [args.site]
    else:
        site_ids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT site_id FROM documents "
                "WHERE site_id IS NOT NULL AND site_id != '' "
                "ORDER BY site_id"
            ).fetchall()
        ]
    if not site_ids:
        print("[warn] no sites with documents")
        return 0

    report: dict[str, dict] = {}
    rows_by_site: dict[str, list[sqlite3.Row]] = {}
    for site in site_ids:
        rows = conn.execute(
            """SELECT id, external_id, title, published_date, pdf_url,
                      abstract, download_status, txt_path, summary
               FROM documents WHERE site_id = ?
               ORDER BY id LIMIT ?""",
            (site, args.limit),
        ).fetchall()
        report[site] = _audit_rows(rows)
        rows_by_site[site] = list(rows)

    print(f"\nQA crawl accuracy report — db={db_path}")
    print(f"sample limit per site: {args.limit}\n")
    _print_table(report)

    if args.xlsx:
        _write_xlsx(report, rows_by_site, Path(args.xlsx).resolve())

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
