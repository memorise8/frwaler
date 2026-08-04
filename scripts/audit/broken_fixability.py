# -*- coding: utf-8 -*-
"""73개 고장 크롤러 수정가능성 라이브 검증(무저장).
각 사이트 start URL을 실제 fetch → 콘텐츠 유무/차단/SPA/사망 판정."""
import sys, sqlite3, csv, subprocess, re, os
ROOT="/data_raid/ruci_workspace/frwaler_job"; sys.path.insert(0,ROOT)

# 고장 사이트 로드 (두 헬스 결과 합집합)
broken=set()
health={}
for fn in ["crawler_health_probe.csv","health_timeout_recheck.csv"]:
    p=f"{ROOT}/scripts/audit/{fn}"
    if os.path.exists(p):
        for r in csv.DictReader(open(p,encoding="utf-8-sig")):
            health[r["site_id"]]=r.get("health","")
for sid,h in health.items():
    if h in ("broken_error","empty_zero_parse"): broken.add(sid)

# start URL: 크롤러의 base_url/start 우선, 없으면 DB site_url
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
    if code in ("403","429","503") or "access denied" in bl or "imperva" in bl or "captcha" in bl or "cloudflare" in bl and "challenge" in bl:
        return "WAF차단(헤더/스텔스 우회검증필요)","fixable?"
    if code in ("404","410") or code=="000":
        return f"사망/무응답(HTTP{code})","structural"
    if edom and dom and edom!=dom and dom.split(".")[-2:]!=edom.split(".")[-2:]:
        return f"도메인이전→{edom}","fixable(도메인갱신)"
    if n<8000 and (("__next_data__" in bl) or ('id="app"' in bl) or ('id="root"' in bl) or ("ng-version" in bl) or (links<5)):
        return "SPA/JS쉘(정적크롤 불가)","structural"
    if code=="200" and links>=15 and n>15000:
        return "콘텐츠 있음(셀렉터 노후 의심)","fixable(셀렉터)"
    return f"불명(HTTP{code}, {n}B, {links}링크)","review"

rows=[]
print(f"고장 크롤러 {len(broken)}개 라이브 검증\n",flush=True)
for i,sid in enumerate(sorted(broken),1):
    u=starturl(sid)
    if not u: rows.append((sid,"","URL없음","review")); print(f"[{i}] {sid[:38]:<39} URL없음");continue
    try: code,eff,body=fetch(u)
    except Exception as e: code,eff,body="ERR","",str(e)
    verdict,cat=classify(sid,u,code,eff,body)
    rows.append((sid,u,verdict,cat))
    print(f"[{i:>2}] {sid[:34]:<35} {cat:<16} {verdict[:40]}",flush=True)

import collections
cc=collections.Counter(r[3] for r in rows)
print(f"\n=== 수정가능성 분포 ===")
for k,v in cc.most_common(): print(f"  {k:<22}{v}")
fixable=sum(v for k,v in cc.items() if "fixable" in k)
print(f"\n수정가능(추정): {fixable}/{len(rows)}  | WAF검증필요 {cc.get('fixable?',0)} | 구조적 {cc.get('structural',0)} | 리뷰 {cc.get('review',0)}")
with open(f"{ROOT}/scripts/audit/broken_fixability.csv","w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f); w.writerow(["site_id","start_url","verdict","category"])
    for x in rows: w.writerow(x)
print("-> broken_fixability.csv 저장")
