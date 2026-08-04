# -*- coding: utf-8 -*-
"""C2(API 가능분) 사이트를 플랫폼별 API로 total 직접 조회."""
import csv, json, re, sqlite3
from urllib.parse import urlparse
import urllib3, requests
urllib3.disable_warnings()

ROOT="/data_raid/ruci_workspace/frwaler_job"
UA={"User-Agent":"Mozilla/5.0","Accept":"application/json,text/xml,*/*"}
TIMEOUT=25

def origin(u):
    p=urlparse(u); return f"{p.scheme}://{p.netloc}"

def get(u, **kw):
    try: return requests.get(u, headers=UA, verify=False, timeout=TIMEOUT, allow_redirects=True, **kw)
    except Exception: return None

def try_ckan(o):
    r=get(f"{o}/api/3/action/package_search?rows=0")
    if r and r.ok:
        try:
            d=r.json()
            if d.get("success"): return d["result"]["count"], "ckan"
        except Exception: pass
    return None

def try_dspace_rest(o):
    for path in ["/server/api/discover/search/objects?size=1","/rest/items?limit=1&offset=0"]:
        r=get(o+path)
        if r and r.ok:
            try:
                d=r.json()
                te=d.get("_embedded",{}).get("searchResult",{}).get("page",{}).get("totalElements")
                if te is None: te=d.get("page",{}).get("totalElements")
                if te: return int(te),"dspace_rest"
            except Exception: pass
    return None

def _oai_total(o, paths):
    for path in paths:
        r=get(f"{o}{path}?verb=ListIdentifiers&metadataPrefix=oai_dc")
        if r and r.ok and "<" in r.text:
            m=re.search(r'completeListSize="(\d+)"', r.text)
            if m: return int(m.group(1)), "oai"
    return None

def try_eprints_oai(o): return _oai_total(o,["/cgi/oai2"])
def try_generic_oai(o): return _oai_total(o,["/oai/request","/oai","/dspace-oai/request","/oai/openaire"])

def try_json_total(u):
    r=get(u)
    if not r: return None
    try: d=r.json()
    except Exception: return None
    best=0
    def scan(o,dep=0):
        nonlocal best
        if dep>8: return
        if isinstance(o,dict):
            for k,v in o.items():
                if isinstance(v,(int,float)) and not isinstance(v,bool) and re.match(r'^(total|totalcount|total_count|numfound|totalelements|totalresults|totalhits|nbhits|count|value)$',str(k),re.I):
                    if 0<=v<1e9: best=max(best,int(v))
                elif isinstance(v,(dict,list)): scan(v,dep+1)
        elif isinstance(o,list):
            for it in o[:50]:
                if isinstance(it,(dict,list)): scan(it,dep+1)
    scan(d)
    return (best,"json_total") if best>0 else None

def measure(sid, plat, url):
    o=origin(url)
    chain=[]
    if plat=="CKAN": chain=[lambda:try_ckan(o)]
    elif plat=="EPrints": chain=[lambda:try_eprints_oai(o), lambda:try_generic_oai(o)]
    elif plat=="DSpace": chain=[lambda:try_dspace_rest(o), lambda:try_generic_oai(o)]
    else:  # Repository 추정 — 다 시도
        chain=[lambda:try_dspace_rest(o), lambda:try_generic_oai(o), lambda:try_eprints_oai(o),
               lambda:try_ckan(o), lambda:try_json_total(url)]
    for fn in chain:
        try:
            res=fn()
        except Exception:
            res=None
        if res and res[0]>0:
            return res
    # 마지막 폴백: 원 URL JSON 스캔
    res=try_json_total(url)
    return res if res else (None,"unknown")

c=sqlite3.connect(f"file:{ROOT}/libertree-app/data/libertree.db?mode=ro",uri=True)
url={sid:(u or '') for sid,u in c.execute('SELECT site_id,site_url FROM sites')}
def plat(u):
    ul=u.lower()
    if '/cgi/' in ul or 'eprints' in ul: return 'EPrints'
    if any(k in ul for k in ['/handle/','/jspui','/xmlui','/discover','/server/api']): return 'DSpace'
    if any(k in ul for k in ['opendata','/dataset','ckan','datos']): return 'CKAN'
    return 'Repository'
c2=[l.strip() for l in open(f"{ROOT}/scripts/audit/c2_sites.txt") if l.strip()]
out=[]
for sid in c2:
    u=url.get(sid,'')
    total,method=measure(sid, plat(u), u)
    out.append((sid,u,total,method))
    print(f"{'OK ' if total else 'X  '} {sid[:32]:<32} total={total if total else '-':<10} [{method}]", flush=True)

with open(f"{ROOT}/scripts/audit/c2_totals.csv","w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f); w.writerow(["site_id","site_url","api_total","method"])
    for sid,u,t,m in out: w.writerow([sid,u,t or "",m])
got=[x for x in out if x[2]]
print(f"\n측정 성공 {len(got)}/{len(out)} | 합계 {sum(x[2] for x in got):,}")
