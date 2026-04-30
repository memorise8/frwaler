#!/usr/bin/env python3
"""사용자 수동 정리 xlsx와 papers.db를 documentNumber 기준으로 비교.

xlsx 형식 (한 컬럼: 파일명):
    국심-1998-서-1698
    조심-2024-인-1032.md         (.md 확장자 가능)
    2000-헌마-8-                 (trailing dash 가능)

매칭 규칙: 정규화(.md, trailing dash 제거)된 xlsx 항목과 DB의
metadata.documentNumber를 정확히 비교.

출력:
    reports/xlsx_compare_<date>.summary.json
        {
          "xlsx_unique": int,
          "db_unique": int,
          "matched": int,
          "unmatched": int,
          "by_pattern": {...}
        }
    reports/xlsx_compare_<date>.unmatched.json
        [{"raw": "...", "norm": "...", "category": "constitutional|typo|other"}, ...]
    reports/xlsx_compare_<date>.matched.json
        [{"docNumber": "...", "site_id": "...", "external_id": "..."}, ...]

Usage:
    python scripts/compare_xlsx.py --xlsx input.xlsx --out-prefix reports/xlsx_compare_0426
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"


def normalise(s: str) -> str:
    s = (s or "").strip()
    if s.endswith(".md"):
        s = s[:-3]
    if s.endswith("-"):
        s = s[:-1]
    return s.strip()


_HEONJAE_RE = re.compile(r"헌(가|마|바|라|나)")
_TYPO_PREFIXES = ("대정지방법원", "울중앙지방법원")  # 알려진 오타


def categorise_unmatched(s: str) -> str:
    if _HEONJAE_RE.search(s):
        return "constitutional_court"
    if any(s.startswith(p) for p in _TYPO_PREFIXES):
        return "likely_typo"
    if "지방법원" in s and not re.search(r"-[가-힣]+-\d", s):
        return "likely_typo"
    return "other"


def load_xlsx(path: Path) -> list[str]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    raws = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        v = row[0]
        if v:
            raws.append(str(v))
    return raws


def load_db_doc_index(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Map normalised(documentNumber) → [{site_id, external_id, title}, ...]"""
    out: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT site_id, external_id, title, metadata FROM papers"
    ):
        try:
            m = json.loads(row[3] or "{}")
        except json.JSONDecodeError:
            continue
        dn = m.get("documentNumber") or ""
        if not dn:
            continue
        # Apply same normalisation to DB side: some records carry trailing
        # dashes too (e.g. '법인46012-3897-').
        out.setdefault(normalise(dn), []).append({
            "site_id": row[0],
            "external_id": row[1],
            "title": (row[2] or "")[:60],
            "raw_docNumber": dn,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default="input.xlsx")
    ap.add_argument("--out-prefix", required=True)
    args = ap.parse_args()

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.is_absolute():
        xlsx_path = ROOT / xlsx_path
    out_prefix = Path(args.out_prefix)
    if not out_prefix.is_absolute():
        out_prefix = ROOT / out_prefix
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    print(f"loading xlsx: {xlsx_path.relative_to(ROOT)}")
    raws = load_xlsx(xlsx_path)
    print(f"  total rows: {len(raws)}")

    raw_by_norm: dict[str, list[str]] = {}
    for raw in raws:
        n = normalise(raw)
        if not n:
            continue
        raw_by_norm.setdefault(n, []).append(raw)
    xlsx_norm = set(raw_by_norm.keys())
    print(f"  unique normalised: {len(xlsx_norm)}")

    print("loading DB documentNumber index...")
    conn = sqlite3.connect(DB_PATH)
    db_idx = load_db_doc_index(conn)
    db_keys = set(db_idx.keys())
    print(f"  unique DB documentNumber: {len(db_keys)}")

    matched_keys = xlsx_norm & db_keys
    unmatched_keys = xlsx_norm - db_keys

    site_dist = Counter()
    matched_records = []
    for k in sorted(matched_keys):
        rows = db_idx[k]
        for r in rows:
            site_dist[r["site_id"]] += 1
        matched_records.append({
            "docNumber": k,
            "occurrences": len(rows),
            "records": rows,
        })

    unmatched_records = []
    cat_counter = Counter()
    for k in sorted(unmatched_keys):
        cat = categorise_unmatched(k)
        cat_counter[cat] += 1
        unmatched_records.append({
            "raw": raw_by_norm.get(k, [k])[0],
            "norm": k,
            "category": cat,
        })

    summary = {
        "xlsx_total_rows": len(raws),
        "xlsx_unique_normalised": len(xlsx_norm),
        "db_unique_documentNumber": len(db_keys),
        "matched": len(matched_keys),
        "matched_pct": round(len(matched_keys) * 100 / len(xlsx_norm), 2)
        if xlsx_norm else 0,
        "unmatched": len(unmatched_keys),
        "matched_by_site": dict(site_dist),
        "unmatched_by_category": dict(cat_counter),
    }

    (out_prefix.parent / f"{out_prefix.name}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_prefix.parent / f"{out_prefix.name}.unmatched.json").write_text(
        json.dumps(unmatched_records, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_prefix.parent / f"{out_prefix.name}.matched.json").write_text(
        json.dumps(matched_records, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print()
    print("=== summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\n  outputs:")
    print(f"    {out_prefix.relative_to(ROOT)}.summary.json")
    print(f"    {out_prefix.relative_to(ROOT)}.matched.json")
    print(f"    {out_prefix.relative_to(ROOT)}.unmatched.json")
    conn.close()


if __name__ == "__main__":
    main()
