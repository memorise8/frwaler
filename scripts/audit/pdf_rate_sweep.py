# -*- coding: utf-8 -*-
"""상위 기여 사이트별 실제 PDF첨부율 실측 (무저장).
각 크롤러를 자체 로직으로 limit=K 실행, _save_paper 가로채 pdf_url 보유율 측정.
→ 실제 다운로드 파일 수 = source_total × pdf_rate. 스토리지 산정의 핵심 계수."""
import sys, sqlite3, csv, time, signal, os
ROOT="/data_raid/ruci_workspace/frwaler_job"
sys.path.insert(0, ROOT)
K=int(os.environ.get("SAMPLE_K","25"))
PER_SITE_TIMEOUT=90

wb_path=f"{ROOT}/scripts/audit/capacity_final_report.xlsx"
import openpyxl
ws=openpyxl.load_workbook(wb_path)["사이트별"]
rows=[dict(zip(["site_id","sheet","site_name","collected","source_total","count_status","origin","method"],r))
      for r in ws.iter_rows(min_row=2,values_only=True)]
# DOAJ 제외(이미 측정), source_total 있는 상위 N
cand=[r for r in rows if r["site_id"] and 'doaj' not in r["site_id"].lower() and (r["source_total"] or 0)>0]
cand.sort(key=lambda x:-(x["source_total"] or 0))
TOPN=int(os.environ.get("TOPN","40"))
targets=cand[:TOPN]
print(f"대상 상위 {len(targets)}개 (source_total 합 {sum(t['source_total'] for t in targets):,})", flush=True)

from crawler.sites import CRAWLERS
class TO(Exception): pass
def _alarm(s,f): raise TO()
signal.signal(signal.SIGALRM,_alarm)

out=[]
for i,t in enumerate(targets,1):
    sid=t["site_id"]; cls=CRAWLERS.get(sid)
    if cls is None:
        out.append((sid,t["source_total"],0,0,"no_crawler")); print(f"[{i}] {sid[:34]:<35} no_crawler",flush=True); continue
    cap=[]
    def fake_save(self,paper,_c=cap): _c.append(paper); return len(_c)
    orig=getattr(cls,"_save_paper",None); cls._save_paper=fake_save
    conn=sqlite3.connect(f"file:{ROOT}/libertree-app/data/libertree.db?mode=ro",uri=True)
    n=0; wp=0; err=""
    signal.alarm(PER_SITE_TIMEOUT)
    try:
        inst=cls(db_conn=conn,delay=0.2); inst.crawl(limit=K)
    except TO: err="timeout"
    except Exception as e: err=f"{type(e).__name__}:{str(e)[:40]}"
    finally:
        signal.alarm(0)
        if orig: cls._save_paper=orig
    n=len(cap); wp=sum(1 for p in cap if p.get("pdf_url"))
    rate=wp/n if n else 0
    out.append((sid,t["source_total"],n,wp,err or f"{100*rate:.0f}%"))
    est=int(t["source_total"]*rate)
    print(f"[{i}] {sid[:34]:<35} n={n:<3} pdf={wp:<3} rate={100*rate:>3.0f}%  →실PDF~{est:,} {('['+err+']') if err else ''}",flush=True)

with open(f"{ROOT}/scripts/audit/pdf_rate_sweep.csv","w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f); w.writerow(["site_id","source_total","sampled_n","sampled_pdf","note","pdf_rate","est_real_pdf"])
    for sid,st,n,wp,note in out:
        rate=wp/n if n else 0
        w.writerow([sid,st,n,wp,note,round(rate,3),int(st*rate)])
tot_src=sum(st for _,st,_,_,_ in out)
tot_est=sum(int(st*(wp/n if n else 0)) for _,st,n,wp,_ in out)
print(f"\n=== 상위{len(out)} source합 {tot_src:,} → 실PDF추정 {tot_est:,} ({100*tot_est/max(tot_src,1):.0f}%) ===",flush=True)
print(f"=== 완료 ===",flush=True)
