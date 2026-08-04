# -*- coding: utf-8 -*-
"""Finalize: total capacity + crawler health from all audit passes.

Combines:
  - measured source_totals (coverage_report.csv + custom_crawler_totals.SNAPSHOT.csv)
  - count-only survey (count_only_totals.csv): exact / lower-bound / broken / no_crawler
Produces:
  - scripts/audit/FINAL_capacity.md
  - scripts/audit/crawler_health_final.csv
"""
import csv, sqlite3, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
AUD = ROOT / "scripts" / "audit"

def num(x):
    try: return int(float(x))
    except: return None
def load(p):
    return list(csv.DictReader(open(p, encoding="utf-8-sig"))) if Path(p).exists() else []

# --- DB: collected + sheet ---
c = sqlite3.connect(f"file:{ROOT}/libertree-app/data/libertree.db?mode=ro", uri=True)
collected = dict(c.execute(
    "SELECT s.site_id, COUNT(d.seq_id) FROM sites s LEFT JOIN documents d ON d.site_id=s.site_id GROUP BY s.site_id"
).fetchall())
sheet = dict(c.execute("SELECT site_id, sheet FROM sites").fetchall())
all_sites = set(sheet) | set(collected)
tot_collected = sum(collected.values())

# --- measured source_totals (API + custom), drop HAL global ---
measured = {}
for r in load(AUD/"coverage_report.csv"):
    s=num(r.get("source_total")); m=r.get("method","")
    if s and s>0 and not (m=="hal_solr" and s>1_500_000):
        measured[r["site_id"]]=(s,m)
snap = AUD/"custom_crawler_totals.SNAPSHOT.csv"
cust = snap if snap.exists() else AUD/"custom_crawler_totals.csv"
for r in load(cust):
    s=num(r.get("source_total"))
    if s and s>0: measured[r["site_id"]]=(s,r.get("method",""))

# --- count-only survey ---
survey = {r["site_id"]: r for r in load(AUD/"count_only_totals.csv")}

# --- classify every site ---
def health(sid):
    if sid in measured: return "정상(측정됨)"
    r = survey.get(sid)
    if not r: return "미조사"
    m=r.get("method",""); cnt=num(r.get("counted")); comp=str(r.get("completed","")).lower() in ("true","1")
    if m=="no_crawler": return "크롤러없음"
    if m.startswith("error"): return "에러(크래시)"
    if comp and (cnt or 0)==0: return "고장(0건파싱)"
    if not comp: return "느림(상한걸림,하한값)"
    if (cnt or 0)>0: return "정상(count됨)"
    return "미상"

rows=[]
for sid in all_sites:
    col=collected.get(sid,0)
    if sid in measured:
        src,method=measured[sid]; kind="measured"; exact=True
    elif sid in survey and num(survey[sid].get("counted")) is not None:
        src=num(survey[sid]["counted"]); method="count_"+survey[sid].get("method","")
        exact = str(survey[sid].get("completed","")).lower() in ("true","1")
        kind="count_exact" if exact else "count_lowerbound"
    else:
        src=None; method=survey.get(sid,{}).get("method","none"); exact=False; kind="unknown"
    rows.append(dict(site_id=sid, sheet=sheet.get(sid,""), collected=col,
                     source_total=src, exact=exact, kind=kind, health=health(sid)))

# --- capacity aggregation ---
def s(kind): return sum(r["source_total"] or 0 for r in rows if r["kind"]==kind)
meas_src=s("measured"); cex=s("count_exact"); clb=s("count_lowerbound")
n=lambda k: sum(1 for r in rows if r["kind"]==k)
unknown_sites=[r for r in rows if r["kind"]=="unknown"]
cap_total = meas_src+cex+clb

lines=[]
def P(x=""): lines.append(x); print(x)
P("# 전체 케파 + 크롤러 건강검진 최종")
P()
P("## 케파 (전체 가용 데이터 총량)")
P(f"- 총 사이트: {len(all_sites)}")
P(f"- 보유(수집 완료): {tot_collected:,}건")
P(f"- 측정(API/custom) {n('measured')}개 사이트: {meas_src:,}건")
P(f"- count 정확 {n('count_exact')}개: {cex:,}건")
P(f"- count 하한(상한걸림) {n('count_lowerbound')}개: {clb:,}건 이상")
P(f"- 미조사(불명) {len(unknown_sites)}개")
P(f"- **확인된 전체 케파 ≈ {cap_total:,}건 이상** (하한 포함; 미조사분 제외)")
if cap_total: P(f"- 현재 커버리지 ≈ {100*tot_collected/max(cap_total,1):.1f}% (보유/케파)")
P()
P("## 크롤러 건강검진")
hc=collections.Counter(r["health"] for r in rows)
for k,v in hc.most_common(): P(f"- {k}: {v}")
P()
broken=[r for r in rows if r["health"] in ("고장(0건파싱)","에러(크래시)")]
P(f"## 손봐야 할 크롤러 (고장/크래시) {len(broken)}개")
for r in sorted(broken, key=lambda x:-x["collected"])[:40]:
    P(f"  {r['site_id'][:34]:<34} 수집{r['collected']:>5} [{r['sheet'][:14]}] {r['health']}")
slow=[r for r in rows if r["health"]=="느림(상한걸림,하한값)"]
P(f"\n## 재측정 대상 (느림/상한) {len(slow)}개 — 상한 늘려 재count")

# --- write outputs ---
with open(AUD/"crawler_health_final.csv","w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f); w.writerow(["site_id","sheet","collected","source_total","exact","kind","health"])
    for r in sorted(rows,key=lambda x:-(x["source_total"] or 0)):
        w.writerow([r["site_id"],r["sheet"],r["collected"],r["source_total"],r["exact"],r["kind"],r["health"]])
(AUD/"FINAL_capacity.md").write_text("\n".join(lines),encoding="utf-8")
P(f"\n-> {AUD}/FINAL_capacity.md, crawler_health_final.csv 저장")
