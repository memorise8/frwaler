# -*- coding: utf-8 -*-
"""사이트별 성격 프로파일 (읽기 전용) — 정상문서 vs 아카이브/시리즈 덤프 판별.

지표:
  n           문서 수
  pdf%        PDF 다운로드 비율
  tmpl%       합성/템플릿 초록 비율 (Title:..Publication date: / Type: Ajaleht / 초록<60자)
  abs_len     평균 초록 길이
  shtitle%    같은 사이트 내 제목을 5개+ 문서가 공유하는 비율 (정기간행물/제네릭제목 신호)
  garb        확정 garbage PDF (전수스캔 pdf_scan_by_site.csv)

archive_score = tmpl% 와 shtitle% 가 모두 높으면 아카이브 덤프 의심.
결과: scripts/audit/out/site_profile.csv
"""
import sqlite3, csv, os

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
DB = f"{REPO}/libertree-app/data/libertree.db"

# 전수스캔 garbage per site
garb = {}
p = f"{OUT}/pdf_scan_by_site.csv"
if os.path.exists(p):
    for r in csv.DictReader(open(p)):
        garb[r["site_id"]] = int(r["not_pdf"])

con = sqlite3.connect(DB); q = con.cursor().execute
tot = q("SELECT COUNT(*) FROM documents").fetchone()[0]

# 사이트 내 제목 공유(>=5) 문서 수
q("DROP TABLE IF EXISTS temp.sht")
q("""CREATE TEMP TABLE sht AS
     SELECT site_id, title, COUNT(*) c FROM documents
     GROUP BY site_id, title HAVING c>=5""")
q("CREATE INDEX temp.i_sht ON sht(site_id,title)")

rows = q("""
  SELECT d.site_id,
    COUNT(*) n,
    SUM(d.pdf_downloaded) pdf,
    SUM(CASE WHEN d.abstract LIKE 'Title:%Publication date:%' OR d.abstract LIKE '%Type: Ajaleht%'
             OR LENGTH(TRIM(COALESCE(d.abstract,'')))<60 THEN 1 ELSE 0 END) tmpl,
    AVG(LENGTH(COALESCE(d.abstract,''))) abslen,
    SUM(CASE WHEN s.title IS NOT NULL THEN 1 ELSE 0 END) shtitle
  FROM documents d
  LEFT JOIN sht s ON s.site_id=d.site_id AND s.title=d.title
  GROUP BY d.site_id
""").fetchall()
con.close()

prof = []
for sid, n, pdf, tmpl, abslen, sht in rows:
    prof.append({
        "site_id": sid, "n": n,
        "pdf%": round(100*pdf/n), "tmpl%": round(100*tmpl/n),
        "abs_len": round(abslen or 0), "shtitle%": round(100*sht/n),
        "garbage": garb.get(sid, 0),
        "archive_susp": (tmpl/n > 0.5 and sht/n > 0.5),
    })

# CSV 저장(문서수 순)
prof.sort(key=lambda r: -r["n"])
with open(f"{OUT}/site_profile.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(prof[0].keys()))
    w.writeheader(); w.writerows(prof)

print(f"총 {tot:,}건 / {len(prof)} 사이트\n")
print("== 문서 최다 TOP25 (아카이브덤프 의심 표시 ★) ==")
print(f"  {'site_id':38s} {'n':>7} {'pdf%':>5} {'tmpl%':>6} {'abs':>5} {'shT%':>5} {'garb':>6}")
for r in prof[:25]:
    star = " ★" if r["archive_susp"] else ""
    print(f"  {str(r['site_id'])[:38]:38s} {r['n']:>7,} {r['pdf%']:>5} {r['tmpl%']:>6} {r['abs_len']:>5} {r['shtitle%']:>5} {r['garbage']:>6}{star}")

# 아카이브덤프 의심 집계
susp = [r for r in prof if r["archive_susp"]]
susp_docs = sum(r["n"] for r in susp)
print(f"\n== 아카이브/시리즈 덤프 의심 사이트: {len(susp)}개 / {susp_docs:,}건 ({susp_docs/tot*100:.1f}%) ==")
for r in sorted(susp, key=lambda r: -r["n"])[:15]:
    print(f"  {str(r['site_id'])[:40]:40s} {r['n']:>7,}  tmpl{r['tmpl%']}% shT{r['shtitle%']}%")
print(f"\n[out] {OUT}/site_profile.csv")
