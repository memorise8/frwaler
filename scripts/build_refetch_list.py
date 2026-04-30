#!/usr/bin/env python3
"""Assemble the targeted-refetch work set for Phase 4 as JSON list:
    [{"doc_id": "...", "site_id": "..."}, ...]

Sources:
  - reports/gap_nts-taxlaw-pd.A_missing_in_db.json
  - reports/gap_nts-taxlaw-qt.A_missing_in_db.json
  - papers rows with empty published_date or empty metadata.rawHtmlPath
    or empty metadata.documentNumber (qt only for the latter two).

De-duplicated across sources and sites; stable order (site, doc_id).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
REPORT_DIR = ROOT / "reports"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",
                    default="reports/refetch_targets_0423.json")
    args = ap.parse_args()

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    seen: set[tuple[str, str]] = set()
    pairs: list[dict] = []

    def add(site: str, doc_id: str, source: str) -> None:
        key = (site, doc_id)
        if not doc_id or key in seen:
            return
        seen.add(key)
        pairs.append({"doc_id": doc_id, "site_id": site, "source": source})

    # A: upstream doc_index but not in DB
    for site in ("nts-taxlaw-pd", "nts-taxlaw-qt"):
        p = REPORT_DIR / f"gap_{site}.A_missing_in_db.json"
        if p.exists():
            items = json.loads(p.read_text(encoding="utf-8"))
            for it in items:
                add(site, it.get("doc_id") or "", source="A_missing_in_db")

    # published_date / rawHtmlPath / documentNumber empties
    queries = [
        ("nts-taxlaw-pd",
         "SELECT external_id FROM papers WHERE site_id=? "
         "AND (published_date IS NULL OR published_date='')",
         "published_date_empty"),
        ("nts-taxlaw-qt",
         "SELECT external_id FROM papers WHERE site_id=? "
         "AND (published_date IS NULL OR published_date='')",
         "published_date_empty"),
        ("nts-taxlaw-qt",
         "SELECT external_id FROM papers WHERE site_id=? "
         "AND (json_extract(metadata, '$.rawHtmlPath') IS NULL "
         "OR json_extract(metadata, '$.rawHtmlPath')='')",
         "rawHtmlPath_empty"),
        ("nts-taxlaw-qt",
         "SELECT external_id FROM papers WHERE site_id=? "
         "AND (json_extract(metadata, '$.documentNumber') IS NULL "
         "OR json_extract(metadata, '$.documentNumber')='')",
         "documentNumber_empty"),
    ]
    for site, sql, source in queries:
        rows = conn.execute(sql, (site,)).fetchall()
        for r in rows:
            add(site, r["external_id"] or "", source=source)

    conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(pairs, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # Summary
    from collections import Counter
    c = Counter((p["site_id"], p["source"]) for p in pairs)
    print(f"wrote {out_path.relative_to(ROOT)}  ({len(pairs)} unique doc ids)")
    for (site, src), n in sorted(c.items()):
        print(f"  {site:<20} {src:<22} {n}")


if __name__ == "__main__":
    main()
