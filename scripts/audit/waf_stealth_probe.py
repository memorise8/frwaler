# -*- coding: utf-8 -*-
"""WAF차단 26개 + 구조적 21개를 StealthSession(크롤러 실제 우회수단)으로 재검증(무저장)."""
import sys, csv, signal, os
ROOT="/data_raid/ruci_workspace/frwaler_job"; sys.path.insert(0,ROOT)
from crawler.stealth_fetcher import StealthSession
r=list(csv.DictReader(open(f"{ROOT}/scripts/audit/broken_fixability.csv",encoding="utf-8-sig")))
targets=[(x["site_id"],x["start_url"],x["category"]) for x in r if x["category"] in ("fixable?","structural","review") and x["start_url"]]
print(f"스텔스 재검증 대상 {len(targets)}개 (WAF+구조적+리뷰)\n",flush=True)
class TO(Exception):pass
signal.signal(signal.SIGALRM,lambda s,f:(_ for _ in ()).throw(TO()))
S=StealthSession()
out=[]
for i,(sid,u,cat) in enumerate(sorted(targets),1):
    signal.alarm(40); verdict=""; n=0
    try:
        html,_=S.fetch_html(u); n=len(html or "")
        bl=(html or "").lower(); links=bl.count("<a ")
        if n>15000 and links>=15: verdict="스텔스로 콘텐츠확보→수정가능"
        elif n<8000 and ("__next_data__" in bl or 'id="app"' in bl or 'id="root"' in bl or links<5): verdict="스텔스로도 SPA쉘→구조적"
        elif n>8000: verdict=f"부분콘텐츠({n}B,{links}링크)→리뷰"
        else: verdict=f"소량({n}B)→구조적의심"
    except TO: verdict="스텔스 타임아웃→차단지속"
    except Exception as e: verdict=f"실패:{type(e).__name__}"
    finally: signal.alarm(0)
    fixable = "수정가능" in verdict
    out.append((sid,cat,verdict,"fixable" if fixable else ("review" if "리뷰" in verdict else "structural")))
    print(f"[{i:>2}] {sid[:32]:<33} {('원'+cat):<12} {verdict}",flush=True)
import collections
cc=collections.Counter(x[3] for x in out)
print(f"\n=== 스텔스 재검증 결과 ===")
for k,v in cc.most_common(): print(f"  {k:<14}{v}")
flip=sum(1 for x in out if x[3]=="fixable")
print(f"\n스텔스로 수정가능 전환: {flip}개 / 재검증 {len(out)}개")
with open(f"{ROOT}/scripts/audit/waf_stealth_result.csv","w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f);w.writerow(["site_id","orig_category","stealth_verdict","final_category"])
    for x in out: w.writerow(x)
print("-> waf_stealth_result.csv 저장")
