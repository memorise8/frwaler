# -*- coding: utf-8 -*-
"""C2 API 재측정 v2 — stealth fetcher(봇차단 우회) + 플랫폼별 올바른 엔드포인트."""
import csv, json, re, sqlite3, sys
from urllib.parse import urlparse
sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.stealth_fetcher import StealthSession

ROOT="/data_raid/ruci_workspace/frwaler_job"
S=StealthSession()

def fetch(u):
    try:
        html,_=S.fetch_html(u)
        return html
    except Exception:
        return None

def j(u):
    t=fetch(u)
    if not t: return None
    try: return json.loads(t)
    except Exception:
        m=re.search(r'\{.*\}', t, re.S)
        try: return json.loads(m.group(0)) if m else None
        except Exception: return None

def oai_total(u):
    t=fetch(u)
    if t:
        m=re.search(r'completeListSize="(\d+)"', t)
        if m: return int(m.group(1))
    return None

def ckan_total(o):
    for api in [f"{o}/api/3/action/package_search?rows=0",
                f"{o}/data/api/3/action/package_search?rows=0"]:
        d=j(api)
        if isinstance(d,dict) and d.get("success"):
            try: return int(d["result"]["count"])
            except Exception: pass
    return None

def dspace_total(o):
    d=j(f"{o}/server/api/discover/search/objects?size=1")
    if isinstance(d,dict):
        for path in [("_embedded","searchResult","page","totalElements"),("page","totalElements")]:
            x=d
            for k in path:
                x=x.get(k) if isinstance(x,dict) else None
            if isinstance(x,int): return x
    return None

def invenio_total(o):
    d=j(f"{o}/api/documents/?size=1") or j(f"{o}/api/documents?size=1") or j(f"{o}/api/records?size=1")
    if isinstance(d,dict):
        h=d.get("hits",{}).get("total")
        if isinstance(h,dict): h=h.get("value")
        if isinstance(h,int): return h
    return None

# 사이트별 측정 전략
def measure(sid,url):
    o=f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    ul=url.lower()
    tries=[]
    if '/cgi/' in ul or 'eprints' in ul or 'doras' in ul:
        tries=[("oai",lambda:oai_total(f"{o}/cgi/oai2?verb=ListIdentifiers&metadataPrefix=oai_dc"))]
    elif 'sonar.ch' in ul:
        tries=[("invenio",lambda:invenio_total(o))]
    elif any(k in ul for k in ['opendata','/dataset','ckan','datos','data.gov','catalogue.data']):
        tries=[("ckan",lambda:ckan_total(o))]
    else:  # DSpace/Repository 계열
        tries=[("dspace",lambda:dspace_total(o)),
               ("oai",lambda:oai_total(f"{o}/oai/request?verb=ListIdentifiers&metadataPrefix=oai_dc")),
               ("oai2",lambda:oai_total(f"{o}/oai?verb=ListIdentifiers&metadataPrefix=oai_dc")),
               ("invenio",lambda:invenio_total(o))]
    for m,fn in tries:
        try: v=fn()
        except Exception: v=None
        if v and v>0: return v,m
    return None,"unknown"

c=sqlite3.connect(f"file:{ROOT}/libertree-app/data/libertree.db?mode=ro",uri=True)
url={sid:(u or '') for sid,u in c.execute('SELECT site_id,site_url FROM sites')}
coll=dict(c.execute('SELECT site_id,COUNT(*) FROM documents GROUP BY site_id').fetchall())
c2=[l.strip() for l in open(f"{ROOT}/scripts/audit/c2_sites.txt") if l.strip()]
out=[]
for sid in c2:
    u=url.get(sid,'')
    total,method=measure(sid,u)
    floor=coll.get(sid,0)
    out.append((sid,u,total,method,floor))
    best=max(total or 0, floor)
    print(f"{'OK ' if total else 'x  '} {sid[:30]:<30} api_total={str(total):<9} floor(수집)={floor:<7} [{method}]",flush=True)

with open(f"{ROOT}/scripts/audit/c2_totals.csv","w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f); w.writerow(["site_id","api_total","collected_floor","best","method","site_url"])
    for sid,u,t,m,fl in out: w.writerow([sid,t or "",fl,max(t or 0,fl),m,u])
got=[x for x in out if x[2]]
print(f"\nAPI측정 성공 {len(got)}/{len(out)} | API합계 {sum(x[2] for x in got):,}")
print(f"best(api or 수집) 합계: {sum(max(x[2] or 0,x[4]) for x in out):,}")
