#!/usr/bin/env python3
"""3-way gap analysis: doc_index (upstream) vs papers (DB) vs exports/ (files).

Produces a categorised gap report:

    A. missing_in_db        — in doc_index but not in papers              (needs fetch)
    B. deleted_upstream     — in papers but not in doc_index (active)     (may be stale)
    C. missing_raw_html     — in papers but rawHtmlPath is blank or file absent
    D. missing_md           — in papers but MD export file absent
    E. missing_html_symlink — in papers but HTML symlink absent (derived from C)

Run:
    python scripts/analyze_gap.py --site-id nts-taxlaw-pd
    python scripts/analyze_gap.py --site-id nts-taxlaw-qt --write-lists
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
EXPORT_ROOT = ROOT / "data" / "exports"
REPORT_DIR = ROOT / "reports"

UNSAFE_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(s: str, max_len: int = 120) -> str:
    s = UNSAFE_FS.sub("_", s or "")
    s = s.strip(" .")
    return s[:max_len] or "unnamed"


def _export_base(doc_number: str, external_id: str) -> str:
    """Mirror the naming convention in scripts/export_papers_md.py."""
    ext = external_id or ""
    doc = doc_number or ext
    return _safe_filename(f"{doc}_{ext}" if doc != ext else str(ext))


def analyze(site_id: str, write_lists: bool = False) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Upstream set (active only)
    idx_rows = conn.execute(
        "SELECT doc_id FROM doc_index WHERE site_id = ? AND deleted_at IS NULL",
        (site_id,),
    ).fetchall()
    upstream_ids: set[str] = {r["doc_id"] for r in idx_rows}

    # DB documents (libertree: was `papers`)
    db_rows = conn.execute(
        "SELECT external_id, metadata FROM documents WHERE site_id = ?",
        (site_id,),
    ).fetchall()
    db_ids: set[str] = {r["external_id"] for r in db_rows if r["external_id"]}

    # File-system view of exports
    out_dir = EXPORT_ROOT / site_id
    md_names: set[str] = set()
    html_names: set[str] = set()
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            if p.name.startswith("_"):
                continue
            if p.suffix == ".md":
                md_names.add(p.stem)
            elif p.suffix == ".html":
                html_names.add(p.stem)

    # Category A: in upstream but not in DB
    missing_in_db = sorted(upstream_ids - db_ids)

    # Category B: in DB but not in active upstream
    deleted_upstream = sorted(db_ids - upstream_ids)

    # Categories C, D, E: per-paper file-system checks
    missing_raw_html: list[dict] = []
    missing_md: list[dict] = []
    missing_html_symlink: list[dict] = []

    for row in db_rows:
        ext_id = row["external_id"] or ""
        if not ext_id:
            continue
        try:
            meta = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            meta = {}

        doc_number = meta.get("documentNumber") or ""
        raw_path = meta.get("rawHtmlPath") or ""
        base = _export_base(doc_number, ext_id)

        # C. raw HTML missing or path blank
        raw_ok = False
        if raw_path:
            raw_ok = (ROOT / raw_path).exists()
        if not raw_ok:
            missing_raw_html.append({
                "external_id": ext_id,
                "doc_number": doc_number,
                "reason": "no_path" if not raw_path else "file_missing",
            })

        # D. MD file missing
        if base not in md_names:
            missing_md.append({
                "external_id": ext_id,
                "doc_number": doc_number,
                "expected_base": base,
            })

        # E. HTML symlink missing in export dir
        if base not in html_names:
            missing_html_symlink.append({
                "external_id": ext_id,
                "doc_number": doc_number,
                "expected_base": base,
            })

    summary = {
        "site_id": site_id,
        "counts": {
            "upstream_active": len(upstream_ids),
            "db": len(db_ids),
            "md_files": len(md_names),
            "html_symlinks": len(html_names),
            "A_missing_in_db": len(missing_in_db),
            "B_deleted_upstream": len(deleted_upstream),
            "C_missing_raw_html": len(missing_raw_html),
            "D_missing_md": len(missing_md),
            "E_missing_html_symlink": len(missing_html_symlink),
        },
    }

    print(f"\n=== Gap analysis: {site_id} ===")
    for k, v in summary["counts"].items():
        print(f"  {k:<28} {v:>8}")

    if write_lists:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        base = REPORT_DIR / f"gap_{site_id}"

        (base.with_suffix(".summary.json")).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        def _dump(name: str, items):
            p = base.with_suffix(f".{name}.json")
            p.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            print(f"  wrote {p.relative_to(ROOT)} ({len(items)} items)")

        _dump("A_missing_in_db", [{"doc_id": d} for d in missing_in_db])
        _dump("B_deleted_upstream", [{"external_id": d} for d in deleted_upstream])
        _dump("C_missing_raw_html", missing_raw_html)
        _dump("D_missing_md", missing_md)
        _dump("E_missing_html_symlink", missing_html_symlink)

    conn.close()
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-id", required=True)
    ap.add_argument("--write-lists", action="store_true",
                    help="Write per-category JSON lists to reports/")
    args = ap.parse_args()
    analyze(args.site_id, write_lists=args.write_lists)


if __name__ == "__main__":
    main()
