#!/usr/bin/env python3
"""Dump raw detail-API responses for a few sample DOC_IDs and enumerate
which of the parser-target fields actually contain data.

Target fields (those that came up 100% empty in DB):
    dcmRfrnPrtsList         -> referencedCases
    dcmQutPrtsList          -> citedCases
    dcmRltnStttMatrList     -> relatedTopics
    fleDVOList              -> attachedFiles
Also inspects:
    dcmRltnStttList         -> relatedLaws (11% empty)
    dvo.ntstDcmMatrCntn     -> keywords (93% empty)

Usage: python scripts/diagnose_api.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.sites.nts_taxlaw import (NTSTaxlawPdCrawler,  # noqa: E402
                                        NTSTaxlawQtCrawler)

OUT_DIR = ROOT / "reports"
OUT_DIR.mkdir(parents=True, exist_ok=True)


SAMPLES = [
    # (site_id, doc_id, description)
    ("nts-taxlaw-pd", "000000000000081838", "pd 판례 (첨부파일 예상)"),
    ("nts-taxlaw-pd", "000000000000111816", "pd 심판 (최근)"),
    ("nts-taxlaw-qt", "010000000000076057", "qt 질의 (최근)"),
    ("nts-taxlaw-qt", "010000000000132065", "qt 질의 (내국신용장)"),
]

CRAWLER_CLS = {
    "nts-taxlaw-pd": NTSTaxlawPdCrawler,
    "nts-taxlaw-qt": NTSTaxlawQtCrawler,
}

# Top-level keys to check for presence/size
TOP_KEYS = [
    "dcmDVO", "dcmHwpEditorDVOList",
    "dcmRltnStttList",      # related laws
    "dcmRltnStttMatrList",  # related topics
    "dcmRfrnPrtsList",      # referenced cases
    "dcmQutPrtsList",       # cited cases
    "fleDVOList",           # attached files
    "trilPsagList",         # trial history
]

DVO_KEYS_OF_INTEREST = [
    "ntstDcmTtl", "ntstDcmGistCntn", "ntstDcmCntn",
    "ntstDcmMatrCntn",                 # keywords source
    "ntstDcmDscmCntn", "ntstTlawClNm",
    "dcmRgtDtm", "frsRgtDtm", "lstAltDtm",
]


def main() -> None:
    import sqlite3
    conn = sqlite3.connect(ROOT / "data" / "papers.db")
    for site_id, doc_id, desc in SAMPLES:
        cls = CRAWLER_CLS[site_id]
        crawler = cls(db_conn=conn, delay=0.5)
        print(f"\n=== {site_id} / {doc_id} — {desc} ===")
        detail = crawler._fetch_detail(doc_id)
        if detail is None:
            print("  FAILED to fetch")
            continue

        # Dump full JSON
        out_path = OUT_DIR / f"detail_{site_id}_{doc_id}.json"
        out_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"  wrote {out_path.relative_to(ROOT)}")

        # Top-level structure summary
        print(f"  top-level keys: {sorted(detail.keys())[:15]}...")
        for key in TOP_KEYS:
            val = detail.get(key)
            if isinstance(val, list):
                print(f"    {key:<22} list[{len(val)}]"
                      + (f"  sample[0] keys: {sorted(val[0].keys())[:6]}" if val and isinstance(val[0], dict) else ""))
            elif isinstance(val, dict):
                print(f"    {key:<22} dict with {len(val)} keys")
            else:
                print(f"    {key:<22} {type(val).__name__}: {str(val)[:40]}")

        # dvo interesting fields
        dvo = detail.get("dcmDVO") or {}
        print(f"  dcmDVO keys total: {len(dvo)}")
        for k in DVO_KEYS_OF_INTEREST:
            v = dvo.get(k)
            if isinstance(v, str):
                short = v[:60].replace("\n", " ")
                print(f"    {k:<22} ({len(v)} chars): {short!r}")
            else:
                print(f"    {k:<22} {type(v).__name__}: {v}")


if __name__ == "__main__":
    main()
