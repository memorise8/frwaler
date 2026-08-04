#!/usr/bin/env python3
"""Generate the site_id -> 대분류(doc-type) map for the Libertree FE.

Reads the original collection spreadsheet (scroll_index.xlsx, '대상종합' sheet),
normalizes the free-text '구분' column into a small set of buckets, matches each
serving-DB site (by full URL path) to a bucket, and emits a TypeScript module:

    libertree-app/src/lib/doc-type-map.generated.ts

The FE reads this map to offer a "유형으로 둘러보기" browse axis. Re-run whenever
scroll_index.xlsx or the sites table changes.
"""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "scroll_index.xlsx"
DB = ROOT / "data" / "libertree.db"
OUT = ROOT / "libertree-app" / "src" / "lib" / "doc-type-map.generated.ts"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Bucket order is the canonical 대분류 taxonomy shown in the FE.
BUCKETS = ["report", "press", "paper", "periodical", "opendata", "statistics", "research", "pdfindex", "etc"]


def bucket_for(gubun: str) -> str:
    g = (gubun or "").strip()
    if not g or g == "구분값 없음":
        return "etc"
    if "보도자료" in g or "Press" in g:
        return "press"
    if "논문" in g:
        return "paper"
    if "연구자료" in g or "연구보고" in g:
        return "research"
    if "보고서" in g or "report" in g.lower():
        return "report"
    if "통계" in g:
        return "statistics"
    if "공공데이터" in g or "데이터" in g or "data" in g.lower():
        return "opendata"
    if "간행물" in g or "출판" in g or "ublication" in g or "정기간행" in g:
        return "periodical"
    if "PDF" in g or "색인" in g:
        return "pdfindex"
    return "etc"


def read_target_sheet() -> dict[str, str]:
    """Return normalized-URL -> 구분(raw) from the 대상종합 sheet."""
    z = zipfile.ZipFile(XLSX)
    shared = [
        "".join(t.text or "" for t in si.iter(NS + "t"))
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(NS + "si")
    ]
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {r.get("Id"): r.get("Target") for r in rels}
    sheets = {s.get("name"): rid_to_target[s.get(RNS + "id")] for s in workbook.find(NS + "sheets")}

    def cell_value(cell) -> str:
        v = cell.find(NS + "v")
        if v is None:
            return ""
        return shared[int(v.text)] if cell.get("t") == "s" else (v.text or "")

    url_to_gubun: dict[str, str] = {}
    for i, row in enumerate(ET.fromstring(z.read("xl/" + sheets["대상종합"])).iter(NS + "row")):
        if i == 0:
            continue
        columns: dict[str, str] = {}
        for cell in row.findall(NS + "c"):
            ref = cell.get("r")
            if ref:
                columns[re.match(r"[A-Z]+", ref).group()] = cell_value(cell)
        url = columns.get("D", "").strip()
        gubun = (columns.get("E") or "").strip()
        if url and gubun:
            url_to_gubun[normalize_url(url)] = gubun
    return url_to_gubun


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host + parsed.path.rstrip("/").lower()
    except ValueError:
        return ""


def resolve_bucket(site_url: str, url_to_gubun: dict[str, str]) -> str:
    normalized = normalize_url(site_url)
    gubun = url_to_gubun.get(normalized)
    if gubun is None:
        candidates = {
            g
            for seed_url, g in url_to_gubun.items()
            if seed_url and (seed_url.startswith(normalized) or normalized.startswith(seed_url))
        }
        gubun = next(iter(candidates)) if len(candidates) == 1 else None
    return bucket_for(gubun) if gubun else "etc"


def main() -> None:
    url_to_gubun = read_target_sheet()
    connection = sqlite3.connect(DB)
    sites = connection.execute("SELECT site_id, site_url FROM sites").fetchall()
    doc_counts = dict(connection.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id").fetchall())

    site_to_bucket: dict[str, str] = {}
    per_bucket_sites: dict[str, int] = {b: 0 for b in BUCKETS}
    per_bucket_docs: dict[str, int] = {b: 0 for b in BUCKETS}
    for site_id, site_url in sites:
        bucket = resolve_bucket(site_url or "", url_to_gubun)
        site_to_bucket[site_id] = bucket
        per_bucket_sites[bucket] += 1
        per_bucket_docs[bucket] += doc_counts.get(site_id, 0)

    ordered = dict(sorted(site_to_bucket.items()))
    body = json.dumps(ordered, ensure_ascii=False, indent=2)
    OUT.write_text(
        "// GENERATED FILE — do not edit by hand.\n"
        "// Source: scripts/generate_doc_types.py (scroll_index.xlsx '대상종합' 구분 → 대분류).\n"
        "// Regenerate: python3 scripts/generate_doc_types.py\n"
        "export const SITE_DOC_TYPES: Readonly<Record<string, string>> = "
        + body
        + " as const\n",
        encoding="utf-8",
    )
    total_docs = sum(per_bucket_docs.values())
    print(f"Wrote {OUT} ({len(ordered)} sites)")
    print(f"{'bucket':12s} {'sites':>6s} {'docs':>10s} {'docs%':>7s}")
    for b in BUCKETS:
        pct = (100 * per_bucket_docs[b] / total_docs) if total_docs else 0
        print(f"{b:12s} {per_bucket_sites[b]:6d} {per_bucket_docs[b]:10d} {pct:6.1f}%")


if __name__ == "__main__":
    main()
