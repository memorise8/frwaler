# -*- coding: utf-8 -*-
"""전체 PDF 블롭 전수 스캔 (읽기 전용).

- 모든 pdf_downloaded=1 문서의 블롭 첫 5바이트를 읽어 `%PDF-` 여부를 확정.
  (관대 판정: 앞쪽 1KB 안에 %PDF- 가 있으면 '유효'로 봄 — 선행 공백/BOM 오탐 방지)
- 결과를 site_id별로 집계하고, 확정 garbage 목록을 CSV로 저장.
- pdf_sha256 내용중복도 전량 집계.

DB/블롭 절대 미변경. 결과만 scripts/audit/out/ 에 기록.

Usage:
    python scripts/audit/full_pdf_scan.py
"""
from __future__ import annotations
import csv
import sqlite3
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DB = REPO / "libertree-app" / "data" / "libertree.db"
BLOB = REPO / "libertree"
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def bpath(seq: int) -> Path:
    s = str(seq).zfill(12)
    return BLOB / s[:4] / s[4:8] / f"{s}.pdf"


def is_valid_pdf(p: Path) -> tuple[bool, bool]:
    """(exists, looks_like_pdf). 앞 1KB 안 어디든 %PDF- 있으면 유효로 관대판정."""
    try:
        with open(p, "rb") as f:
            head = f.read(1024)
    except FileNotFoundError:
        return (False, False)
    except OSError:
        return (True, False)
    return (True, b"%PDF-" in head)


def main() -> None:
    con = sqlite3.connect(str(DB))
    q = con.cursor().execute

    rows = q(
        "SELECT seq_id, site_id, pdf_size_bytes FROM documents WHERE pdf_downloaded=1"
    ).fetchall()
    total = len(rows)
    print(f"[scan] pdf_downloaded=1 문서 {total:,}건 전수 스캔 시작", flush=True)

    from collections import defaultdict
    per_site = defaultdict(lambda: [0, 0, 0])  # site -> [checked, missing, not_pdf]
    garbage = []  # (seq_id, site_id, size)
    missing = []
    checked = 0
    t0 = time.time()

    for i, (seq, site, size) in enumerate(rows):
        exists, ok = is_valid_pdf(bpath(seq))
        st = per_site[site]
        if not exists:
            st[1] += 1
            missing.append((seq, site, size))
            continue
        st[0] += 1
        checked += 1
        if not ok:
            st[2] += 1
            garbage.append((seq, site, size))
        if (i + 1) % 20000 == 0:
            el = time.time() - t0
            rate = (i + 1) / el
            print(
                f"  {i+1:>7,}/{total:,}  ({rate:,.0f}/s)  "
                f"garbage={len(garbage):,} missing={len(missing):,}",
                flush=True,
            )

    el = time.time() - t0
    print(f"\n[scan] 완료: {total:,}건, {el:.0f}s", flush=True)
    print(f"  검사됨(파일존재)   : {checked:,}")
    print(f"  파일 없음          : {len(missing):,}")
    print(f"  %PDF- 아님(확정 garbage): {len(garbage):,}  ({len(garbage)/max(1,checked)*100:.2f}%)")

    # ---- garbage / missing CSV ----
    with open(OUT / "pdf_garbage.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq_id", "site_id", "pdf_size_bytes"])
        w.writerows(sorted(garbage, key=lambda r: (str(r[1]), r[0])))
    with open(OUT / "pdf_missing.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq_id", "site_id", "pdf_size_bytes"])
        w.writerows(sorted(missing, key=lambda r: (str(r[1]), r[0])))

    # ---- 사이트별 garbage TOP ----
    print("\n== 확정 garbage 집중 사이트 TOP20 ==")
    ranked = sorted(per_site.items(), key=lambda kv: kv[1][2], reverse=True)
    with open(OUT / "pdf_scan_by_site.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site_id", "checked", "missing", "not_pdf"])
        for site, (chk, mis, npdf) in sorted(per_site.items(), key=lambda kv: kv[1][2], reverse=True):
            w.writerow([site, chk, mis, npdf])
    for site, (chk, mis, npdf) in ranked[:20]:
        if npdf == 0:
            break
        print(f"  {str(site)[:44]:44s} not_pdf={npdf:>6,}  (검사 {chk:,}, 누락 {mis})")

    # ---- 내용(sha256) 중복 전량 집계 ----
    print("\n== PDF 내용(sha256) 중복 전량 ==")
    excess = q(
        """SELECT COALESCE(SUM(n-1),0) FROM
           (SELECT COUNT(*) n FROM documents
            WHERE pdf_downloaded=1 AND COALESCE(pdf_sha256,'')<>''
            GROUP BY pdf_sha256 HAVING n>1)"""
    ).fetchone()[0]
    xsite = q(
        """SELECT COUNT(*) FROM
           (SELECT pdf_sha256 FROM documents
            WHERE pdf_downloaded=1 AND COALESCE(pdf_sha256,'')<>''
            GROUP BY pdf_sha256 HAVING COUNT(DISTINCT site_id)>1)"""
    ).fetchone()[0]
    print(f"  내용 동일 잉여행: {excess:,}   (교차사이트 중복 내용 종류: {xsite:,})")

    print(f"\n[out] {OUT}/pdf_garbage.csv, pdf_missing.csv, pdf_scan_by_site.csv")
    con.close()


if __name__ == "__main__":
    main()
