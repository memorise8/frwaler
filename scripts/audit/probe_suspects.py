# -*- coding: utf-8 -*-
"""html_repair_worklist.csv의 UNDIAGNOSED(미진단) 의심 크롤러를 라이브 진단(무저장).
broken_fixability.py의 fetch/classify 로직 재사용. 병렬."""
import sys, sqlite3, csv, subprocess, re, os
from concurrent.futures import ThreadPoolExecutor, as_completed
ROOT="/data_raid/ruci_workspace/frwaler_job"; sys.path.insert(0,ROOT)
AUD=f"{ROOT}/scripts/audit"

targets=[]
for r in csv.DictReader(open(f"{AUD}/html_repair_worklist.csv",encoding="utf-8-sig")):
    if r["fix_class"]=="UNDIAGNOSED": targets.append(r["site_id"])
print(f"미진단 대상 {len(targets)}개 진단 시작", flush=True)

from crawler.sites import CRAWLERS
c=sqlite3.connect(f"file:{ROOT}/libertree-app/data/libertree.db?mode=ro",uri=True)
dburl=dict(c.execute("SELECT site_id,site_url FROM sites"))

def starturl(sid):
    cls=CRAWLERS.get(sid)
    for attr in ("_START_URL","start_url","_LIST_URL","base_url"):
        v=getattr(cls,attr,None)
        if isinstance(v,str) and v.startswith("http"): return v
    return dburl.get(sid,"")

def fetch(u,ua="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",mt=20):
    r=subprocess.run(["curl","-skL","--max-time",str(mt),"-A",ua,"-w","\n__HTTP__%{http_code}__%{url_effective}",u],
                     capture_output=True,timeout=mt+5)
    t=r.stdout.decode("utf-8",errors="replace")
    m=re.search(r"__HTTP__(\d+)__(.*)$",t); code=m.group(1) if m else "000"; eff=m.group(2) if m else ""
    body=t[:m.start()] if m else t
    return code,eff,body

def classify(sid,u,code,eff,body):
    bl=body.lower(); n=len(body); links=body.count("<a ")
    dom=re.sub(r"https?://([^/]+).*",r"\1",u); edom=re.sub(r"https?://([^/]+).*",r"\1",eff)
    if code in ("403","429","503") or "access denied" in bl or "imperva" in bl or "captcha" in bl or ("cloudflare" in bl and "challenge" in bl):
        return "WAF차단(헤더/스텔스 우회검증필요)","FIX_stealth"
    if code in ("404","410") or code=="000":
        return f"사망/무응답(HTTP{code})","SKIP_dead"
    if edom and dom and edom!=dom and dom.split(".")[-2:]!=edom.split(".")[-2:]:
        return f"도메인이전→{edom}","FIX_domain"
    if n<8000 and (("__next_data__" in bl) or ('id="app"' in bl) or ('id="root"' in bl) or ("ng-version" in bl) or (links<5)):
        return "SPA/JS쉘(정적크롤 불가)","SKIP_spa"
    if code=="200" and links>=15 and n>15000:
        return "콘텐츠 있음(셀렉터 노후 의심)","FIX_selector"
    return f"불명(HTTP{code}, {n}B, {links}링크)","PROBE_review"

def work(sid):
    u=starturl(sid)
    if not u: return (sid,"","URL없음","PROBE_review")
    try: code,eff,body=fetch(u)
    except Exception as e: return (sid,u,f"ERR:{e}","PROBE_review")
    v,fc=classify(sid,u,code,eff,body)
    return (sid,u,v,fc)

rows=[]
with ThreadPoolExecutor(max_workers=12) as ex:
    futs={ex.submit(work,s):s for s in targets}
    for i,f in enumerate(as_completed(futs),1):
        r=f.result(); rows.append(r)
        print(f"[{i}/{len(targets)}] {r[0][:38]:<39} {r[3]:14s} {r[2][:30]}",flush=True)

out=f"{AUD}/probe_suspects.csv"
with open(out,"w",newline="") as f:
    w=csv.writer(f); w.writerow(["site_id","url","verdict","fix_class"]); w.writerows(rows)

from collections import Counter
print("\n=== 진단 분포 ===")
for k,v in Counter(r[3] for r in rows).most_common(): print(f"  {k:16s} {v}")
print(f"\n저장: {out}")
