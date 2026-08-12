#!/usr/bin/env python3
"""Generate the shared BE/FE site taxonomy from repository source data."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "delivery" / "data" / "site-taxonomy.json"
LABELS = {
    "etc": "기타", "opendata": "공공데이터", "paper": "논문",
    "pdfindex": "PDF 검색 색인", "periodical": "간행물", "press": "보도자료",
    "report": "보고서", "research": "연구자료", "statistics": "통계",
}


def main() -> None:
    categories = (ROOT / "libertree-app/src/lib/categories.ts").read_text(encoding="utf-8")
    countries = {
        match.group(1) or match.group(2): match.group(3)
        for match in re.finditer(
            r'^\s*(?:"([^"]+)"|([^\s:{]+)):\s*\{\s*country:\s*"([^"]+)"',
            categories, re.MULTILINE,
        )
    }
    doc_types_source = (ROOT / "libertree-app/src/lib/doc-type-map.generated.ts").read_text(encoding="utf-8")
    doc_types = {
        site_id: LABELS.get(code, "기타")
        for site_id, code in re.findall(r'^\s*"([^"]+)":\s*"([^"]+)"', doc_types_source, re.MULTILINE)
    }
    with (ROOT / "scripts/audit/capacity_final.csv").open(encoding="utf-8-sig", newline="") as handle:
        sheets = {row["site_id"]: row["sheet"] for row in csv.DictReader(handle)}
    site_ids = sorted(set(sheets) | set(doc_types))
    payload = {
        "version": "2026-08-12",
        "sites": {
            site_id: {
                "country": countries.get(sheets.get(site_id, ""), "기타"),
                "doc_type": doc_types.get(site_id, "기타"),
            }
            for site_id in site_ids
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(site_ids)} sites")


if __name__ == "__main__":
    main()
