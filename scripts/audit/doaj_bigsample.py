# -*- coding: utf-8 -*-
"""DOAJ PDF율 정밀화 — 검색어×연도 다변화로 유니크 ~6000건 수집 후 PDF율 측정(무저장)."""
import json, subprocess, time
def curl(u,mt=30):
    r=subprocess.run(["curl","-sk","--tls-max","1.3","--max-time",str(mt),
        "-H","User-Agent: Mozilla/5.0","-H","Accept: application/json",u],capture_output=True,timeout=mt+5)
    return r.stdout
def has_pdf(bj):
    for ln in bj.get("link") or []:
        ct=(ln.get("content_type") or "").upper(); lt=(ln.get("type") or "").lower()
        if "PDF" in ct or "pdf" in lt: return True
    for ln in bj.get("link") or []:
        if (ln.get("url") or "").lower().endswith(".pdf"): return True
    return False
BASE="https://doaj.org/api/search/articles/{q}?pageSize=100&page={pg}"
terms=["health","economics","physics","biology","chemistry","education","agriculture",
       "engineering","medicine","law","psychology","mathematics","history","environment",
       "computer","nursing","energy","food"]
years=[str(y) for y in range(2012,2026)]
queries=[("q",t) for t in terms]+[("y",y) for y in years]
seen=set(); nrec=npdf=nfilter=0; njour=set(); tot=None
for kind,val in queries:
    if nrec>=6000: break
    q = val if kind=="q" else f"bibjson.year:{val}"
    for pg in range(1,7):
        if nrec>=6000: break
        raw=curl(BASE.format(q=q,pg=pg))
        try: d=json.loads(raw)
        except: break
        if tot is None: tot=d.get("total")
        res=d.get("results") or []
        if not res: break
        new=0
        for r in res:
            aid=r.get("id")
            if not aid or aid in seen: continue
            seen.add(aid); new+=1; nrec+=1
            bj=r.get("bibjson") or {}
            t=(bj.get("title") or "").strip(); ab=(bj.get("abstract") or "").strip()
            if t and len(ab)>=100: nfilter+=1
            njour.add((bj.get("journal") or {}).get("title") or "")
            if has_pdf(bj): npdf+=1
        if new==0: break
        time.sleep(0.2)
    print(f"  [{kind}:{val}] 누적 {nrec}건 저널{len(njour)} pdf{100*npdf/max(nrec,1):.0f}%",flush=True)
DOAJ=13362199
fr=nfilter/nrec if nrec else 0; pr=npdf/nrec if nrec else 0
import math
se=math.sqrt(pr*(1-pr)/nrec) if nrec else 0  # PDF율 표준오차
print(f"\n=== DOAJ 대규모샘플 (유니크 {nrec}건, 저널 {len(njour)}개) ===")
print(f"저장필터 통과 {100*fr:.1f}%")
print(f"직접PDF보유 {100*pr:.1f}% (95%CI ±{100*1.96*se:.1f}%p)")
print(f"→ 메타저장가능 ~{int(DOAJ*fr):,} / 직접PDF ~{int(DOAJ*pr):,}건 (~{int(DOAJ*(pr-1.96*se)):,}~{int(DOAJ*(pr+1.96*se)):,})")
print(f"→ PDF용량 ~{DOAJ*pr*5.63/1024/1024:.1f}TB")
open("scripts/audit/doaj_bigsample_result.txt","w").write(
    f"sample={nrec} journals={len(njour)} filter_rate={fr:.4f} pdf_rate={pr:.4f} pdf_rate_ci={1.96*se:.4f} "
    f"est_meta={int(DOAJ*fr)} est_pdf={int(DOAJ*pr)}\n")
print("=== 완료 ===")
