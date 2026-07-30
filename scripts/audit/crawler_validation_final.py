# -*- coding: utf-8 -*-
"""크롤러 검증 최종판 (읽기 전용) — '깨진 PDF' 실제 정체까지 반영.

기존 crawler_validation 은 '%PDF- 아님 = 결함'으로 과다 계상했다.
실제로는 non-PDF 중 대부분이 HWP/Office/이미지 = 진짜 콘텐츠(확장자만 .pdf).
이 스크립트는 깨진 파일의 실제 매직바이트로 유형을 나눠 재판정한다.

사이트(크롤러) 판정:
  GOOD             메타 정상 + PDF 유효(또는 콘텐츠형 non-PDF)
  FORMAT_MISMATCH  실제 콘텐츠(HWP/Office/이미지)를 .pdf로 저장 — 수집은 성공, 라벨만 오류
  TRUE_DEFECT      HTML/미상을 저장 = 실제 콘텐츠 없음 (다운로드 로직 결함)
  THIN             메타 빈약

출력: scripts/audit/out/crawler_validation_final.csv + 요약
"""
import sqlite3, csv, os
from pathlib import Path
from collections import defaultdict

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
DB = f"{REPO}/libertree-app/data/libertree.db"
BLOB = Path(f"{REPO}/libertree")


def kind(b):
    if b[:5] == b"%PDF-": return "pdf"
    if b[:2] == b"\xff\xd8": return "content"          # JPEG
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "content" # PNG
    if b[:4] == b"PK\x03\x04": return "content"        # docx/hwpx/zip
    if b[:4] == b"{\\rt": return "content"             # RTF
    if b[:4] == b"\xd0\xcf\x11\xe0": return "content"  # OLE (hwp/doc)
    if b[:5] == b"%!PS-": return "content"
    h = b[:400].lstrip().lower()
    if h[:14] == b"<!doctype html" or h[:5] == b"<html" or b"<head" in h[:200] or b"<body" in h[:200]:
        return "html"
    return "unknown"


# 깨진 PDF 실제 유형 집계 (site -> {content, html, unknown})
brk = defaultdict(lambda: {"content": 0, "html": 0, "unknown": 0})
for row in csv.DictReader(open(f"{OUT}/pdf_garbage.csv")):
    s = str(row["seq_id"]).zfill(12)
    p = BLOB / s[:4] / s[4:8] / f"{s}.pdf"
    try:
        k = kind(p.read_bytes()[:512])
    except OSError:
        k = "unknown"
    if k == "pdf":
        continue
    brk[row["site_id"]][k if k in ("content", "html") else "unknown"] += 1

con = sqlite3.connect(DB); q = con.cursor().execute
tot = q("SELECT COUNT(*) FROM documents").fetchone()[0]
rows = q("""SELECT site_id, COUNT(*) n,
    SUM(CASE WHEN COALESCE(TRIM(title),'')<>'' THEN 1 ELSE 0 END) t_ok,
    SUM(CASE WHEN COALESCE(TRIM(abstract),'')<>'' THEN 1 ELSE 0 END) d_ok,
    SUM(pdf_downloaded) pdf
    FROM documents GROUP BY site_id""").fetchall()
con.close()

prof = []
for sid, n, t_ok, d_ok, pdf in rows:
    b = brk.get(sid, {"content": 0, "html": 0, "unknown": 0})
    real_defect = b["html"] + b["unknown"]
    fmt = b["content"]
    meta_ok = (t_ok / n >= 0.9) and (d_ok / n >= 0.8)
    if real_defect / n > 0.1:
        v = "TRUE_DEFECT"
    elif fmt / n > 0.1:
        v = "FORMAT_MISMATCH"
    elif not meta_ok:
        v = "THIN"
    else:
        v = "GOOD"
    prof.append({"site_id": sid, "n": n, "meta%": round(100*t_ok/n), "desc%": round(100*d_ok/n),
                 "pdf_dl%": round(100*pdf/n), "fmt_mismatch": fmt, "true_defect": real_defect,
                 "verdict": v})

prof.sort(key=lambda r: -r["n"])
with open(f"{OUT}/crawler_validation_final.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(prof[0].keys())); w.writeheader(); w.writerows(prof)

from collections import Counter
vc, vd = Counter(), Counter()
for r in prof:
    vc[r["verdict"]] += 1; vd[r["verdict"]] += r["n"]
print(f"총 {len(prof)} 크롤러 / {tot:,}건\n== 최종 판정 ==")
for v in ("GOOD", "FORMAT_MISMATCH", "TRUE_DEFECT", "THIN"):
    print(f"  {v:16s}: {vc[v]:>4} 크롤러 / {vd[v]:>8,}건 ({vd[v]/tot*100:.1f}%)")

meaningful = vd["GOOD"] + vd["FORMAT_MISMATCH"] + vd["THIN"]
tot_fmt = sum(r["fmt_mismatch"] for r in prof)
tot_def = sum(r["true_defect"] for r in prof)
print(f"\n  ✅ 의미있는 데이터(콘텐츠 확보): {meaningful:,} = {meaningful/tot*100:.1f}%")
print(f"  ❌ 진짜 수집 실패(HTML/미상)   : {tot_def:,} = {tot_def/tot*100:.2f}%")
print(f"  ⚠️ 포맷 라벨 오류(.pdf 저장, 콘텐츠 정상): {tot_fmt:,} = {tot_fmt/tot*100:.2f}%  → 재라벨만 하면 됨")

print("\n== TRUE_DEFECT 크롤러 (진짜 수정 필요) ==")
for r in sorted([r for r in prof if r["verdict"] == "TRUE_DEFECT"], key=lambda r: -r["true_defect"])[:12]:
    print(f"  {str(r['site_id'])[:40]:40s} n={r['n']:>6,} 진짜결함={r['true_defect']:,}")
print(f"\n[out] {OUT}/crawler_validation_final.csv")
