# -*- coding: utf-8 -*-
"""납품용 최종 집계: ①정보레코드 총량(gross) ②PDF원문 총량 ③스토리지.
입력: capacity_final_report.xlsx(레코드 source_total), crawler_health_probe.csv(고장제외),
      pdf_rate_sweep.csv(사이트별 실측 PDF율), DB(실측 평균 PDF 크기).
미측정 사이트는 카테고리 기본 PDF율 적용(명시)."""
import csv, sqlite3, collections, openpyxl
ROOT="/data_raid/ruci_workspace/frwaler_job"; A=f"{ROOT}/scripts/audit"

# 1) 레코드 source_total
ws=openpyxl.load_workbook(f"{A}/capacity_final_report.xlsx")["사이트별"]
rec={}
for r in ws.iter_rows(min_row=2,values_only=True):
    sid,sheet,name,coll,src,cs,orig,meth=r
    if sid: rec[sid]={"src":src or 0,"method":(meth or ""),"coll":coll or 0,"status":cs}

# 2) 헬스: 고장(0파싱) 사이트 = 실수집 불가로 간주
health={r["site_id"]:r.get("health","") for r in csv.DictReader(open(f"{A}/crawler_health_probe.csv",encoding="utf-8-sig"))}
BROKEN={sid for sid,h in health.items() if h in ("broken_error","empty_zero_parse")}

# 3) 측정된 PDF율
measured={}
for r in csv.DictReader(open(f"{A}/pdf_rate_sweep.csv",encoding="utf-8-sig")):
    n=int(r["sampled_n"]); rate=float(r["pdf_rate"])
    if n>=10: measured[r["site_id"]]=rate   # 유효 샘플만
# DOAJ 별도 실측
measured["doaj-org-search"]=0.15  # 12~18% 중앙 보수값

# 측정실패(느린 DSpace) 보정: 카테고리 기본값 처리됨(아래 category_rate)
FIXED_DEFAULT={"dspace-ut-ee-search":0.95,"repositorio-uchile-cl-discover":0.95,"ir-lib-uth-gr-xmlui":0.95}

# 4) 카테고리 기본 PDF율 (미측정 사이트용)
def category_rate(sid,method):
    m=method.lower()
    if sid in FIXED_DEFAULT: return FIXED_DEFAULT[sid],"repo_default(측정실패보정)"
    if any(k in m for k in ["hal_solr","dspace","oai_resumption","jspui","xmlui"]): return 0.90,"repo_default"
    if "ckan" in m: return 0.60,"ckan_default"
    if "html_count_regex" in m: return 0.10,"stats_default"
    if "wordpress" in m or "wp_total" in m or "news" in m: return 0.05,"news_default"
    if "count_crawl" in m or "html_paginate" in m: return 0.50,"generic_default"
    return 0.40,"unknown_default"

# 5) DB 실측 평균 PDF 크기
c=sqlite3.connect(f"file:{ROOT}/libertree-app/data/libertree.db?mode=ro",uri=True)
sz=c.execute("SELECT COUNT(*),AVG(pdf_size_bytes),SUM(pdf_size_bytes) FROM documents WHERE pdf_size_bytes>0").fetchone()
n_pdf,avg_sz,sum_sz=sz
AVG_MB=avg_sz/1024/1024
print(f"실측 평균 PDF 크기: {AVG_MB:.2f} MB (보유 PDF {n_pdf:,}개 / {sum_sz/1024**3:.0f} GB)\n")

# 6) 집계
gross_rec=0; healthy_rec=0; pdf_total=0; measured_pdf=0; default_pdf=0
cat_tot=collections.defaultdict(lambda:[0,0,0])  # rate_src -> [sites,records,pdf]
for sid,d in rec.items():
    src=d["src"]
    if not src: continue
    gross_rec+=src
    if sid in BROKEN:   # 고장 = 실수집 0
        continue
    healthy_rec+=src
    if sid in measured:
        rate=measured[sid]; tag="measured"
    else:
        rate,tag=category_rate(sid,d["method"])
    pdf=int(src*rate); pdf_total+=pdf
    (measured_pdf if sid in measured else default_pdf)
    if sid in measured: measured_pdf+=pdf
    else: default_pdf+=pdf
    grp=cat_tot["measured" if sid in measured else tag]
    grp[0]+=1; grp[1]+=src; grp[2]+=pdf

storage_tb=pdf_total*AVG_MB/1024/1024
print("="*62)
print("납품용 최종 집계")
print("="*62)
print(f"① 정보 레코드 (gross, 중복포함)     : {gross_rec:,}")
print(f"   └ 고장크롤러 제외(실수집가능)     : {healthy_rec:,}  (고장 {len(BROKEN)}개 제외)")
print(f"② PDF 원문 (실측+카테고리 기본율)    : {pdf_total:,}")
print(f"     ├ 측정기반                     : {measured_pdf:,}")
print(f"     └ 카테고리기본값(미측정)         : {default_pdf:,}")
print(f"③ 스토리지 (PDF {pdf_total:,} × {AVG_MB:.2f}MB): ≈ {storage_tb:.1f} TB")
print(f"\n[PDF율 근거별 분해]")
print(f"{'근거':<26}{'사이트':>6}{'레코드':>13}{'PDF':>13}")
for tag,(ns,nr,npd) in sorted(cat_tot.items(),key=lambda x:-x[1][2]):
    print(f"{tag:<26}{ns:>6}{nr:>13,}{npd:>13,}")
