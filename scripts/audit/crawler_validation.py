# -*- coding: utf-8 -*-
"""크롤러(사이트)별 수집 검증 리포트 (읽기 전용).

"이전에 만든 크롤러들이 정상 동작하는가"를 데이터로 증명하기 위한 것.
각 사이트(=크롤러)가 의미 있는 데이터를 수집했는지 판정한다.

크롤러 정상 지표:
  meta_ok%   : 제목 비어있지 않음
  desc_ok%   : 초록/서지정보 채워짐 (아카이브는 이게 메타데이터)
  date_ok%   : 발행일 채워짐
  pdf_dl%    : PDF 다운로드 시도 성공률
  pdf_valid% : 다운로드한 PDF 중 실제 유효(%PDF-) 비율  ← 전수스캔 기반

판정:
  GOOD        메타 정상 + (PDF 유효율 높음 OR PDF 미수집 정책)
  PDF_DEFECT  PDF를 받지만 유효율 낮음(HTML을 PDF로 저장) = 다운로드 로직 결함
  THIN        메타가 빈약

결과: scripts/audit/out/crawler_validation.csv + 요약
"""
import sqlite3, csv, os

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
DB = f"{REPO}/libertree-app/data/libertree.db"

# 전수 PDF 스캔 (site -> checked/not_pdf)
scan = {}
p = f"{OUT}/pdf_scan_by_site.csv"
for r in csv.DictReader(open(p)):
    scan[r["site_id"]] = (int(r["checked"]), int(r["not_pdf"]))

con = sqlite3.connect(DB); q = con.cursor().execute
tot = q("SELECT COUNT(*) FROM documents").fetchone()[0]
rows = q("""
  SELECT site_id, COUNT(*) n,
    SUM(CASE WHEN COALESCE(TRIM(title),'')<>'' THEN 1 ELSE 0 END) t_ok,
    SUM(CASE WHEN COALESCE(TRIM(abstract),'')<>'' THEN 1 ELSE 0 END) d_ok,
    SUM(CASE WHEN COALESCE(TRIM(published_date),'')<>'' THEN 1 ELSE 0 END) dt_ok,
    SUM(pdf_downloaded) pdf
  FROM documents GROUP BY site_id
""").fetchall()
con.close()

prof = []
for sid, n, t_ok, d_ok, dt_ok, pdf in rows:
    chk, notpdf = scan.get(sid, (0, 0))
    pdf_valid = (100 * (chk - notpdf) / chk) if chk else None   # None = PDF 미수집
    r = {
        "site_id": sid, "n": n,
        "meta%": round(100*t_ok/n), "desc%": round(100*d_ok/n),
        "date%": round(100*dt_ok/n), "pdf_dl%": round(100*pdf/n),
        "pdf_valid%": ("" if pdf_valid is None else round(pdf_valid)),
        "broken_pdf": notpdf,
    }
    # 판정
    meta_ok = r["meta%"] >= 90 and r["desc%"] >= 80
    if pdf_valid is not None and r["pdf_dl%"] >= 30 and pdf_valid < 90:
        r["verdict"] = "PDF_DEFECT"
    elif not meta_ok:
        r["verdict"] = "THIN"
    else:
        r["verdict"] = "GOOD"
    prof.append(r)

prof.sort(key=lambda r: -r["n"])
with open(f"{OUT}/crawler_validation.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(prof[0].keys())); w.writeheader(); w.writerows(prof)

# 요약
from collections import Counter
vc = Counter(r["verdict"] for r in prof)
vd = Counter()
for r in prof: vd[r["verdict"]] += r["n"]
print(f"총 {len(prof)} 크롤러(사이트) / {tot:,}건\n")
print("== 판정 요약 ==")
for v in ("GOOD", "PDF_DEFECT", "THIN"):
    print(f"  {v:11s}: {vc[v]:>4} 크롤러 / {vd[v]:>8,}건 ({vd[v]/tot*100:.1f}%)")
good_rate = vd["GOOD"] / tot * 100
print(f"\n  ✅ 정상 수집 데이터: {vd['GOOD']:,} / {tot:,} = {good_rate:.1f}%")

print("\n== PDF_DEFECT 크롤러 (다운로드 로직 결함) TOP ==")
for r in sorted([r for r in prof if r["verdict"] == "PDF_DEFECT"], key=lambda r: -r["broken_pdf"])[:15]:
    print(f"  {str(r['site_id'])[:40]:40s} n={r['n']:>6,} pdf유효={r['pdf_valid%']}% 깨진PDF={r['broken_pdf']:,}")

print("\n== THIN 크롤러 (메타 빈약) TOP ==")
for r in sorted([r for r in prof if r["verdict"] == "THIN"], key=lambda r: -r["n"])[:10]:
    print(f"  {str(r['site_id'])[:40]:40s} n={r['n']:>6,} meta={r['meta%']}% desc={r['desc%']}%")
print(f"\n[out] {OUT}/crawler_validation.csv")
