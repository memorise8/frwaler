# -*- coding: utf-8 -*-
"""Consolidate total capacity (API + custom measured) and build a crawler health list."""
import csv, re, sqlite3, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
AUD = ROOT / "scripts" / "audit"

def num(x):
    try: return int(float(x))
    except: return None

# --- collected counts from DB ---
c = sqlite3.connect(f"file:{ROOT}/libertree-app/data/libertree.db?mode=ro", uri=True)
collected = dict(c.execute(
    "SELECT s.site_id, COUNT(d.seq_id) FROM sites s LEFT JOIN documents d ON d.site_id=s.site_id GROUP BY s.site_id"
).fetchall())
sheet = dict(c.execute("SELECT site_id, sheet FROM sites").fetchall())

# --- measured source_total: prefer custom CSV, then coverage CSV; drop HAL globals(>1.5M) ---
measured = {}   # site_id -> (source_total, method)
def load(path):
    if not path.exists(): return []
    return list(csv.DictReader(open(path)))

cov = load(AUD / "coverage_report.csv")
cust = load(AUD / "custom_crawler_totals.csv")

for r in cov:
    s = num(r.get("source_total")); m = r.get("method","")
    if s and s > 0:
        if m == "hal_solr" and s > 1_500_000:   # global-HAL scope error
            continue
        measured[r["site_id"]] = (s, m)
# custom pass overrides/adds (these were 'unknown' in cov)
for r in cust:
    s = num(r.get("source_total")); m = r.get("method","")
    if s and s > 0:
        measured[r["site_id"]] = (s, m)

# --- totals ---
all_sites = set(collected) | set(sheet)
tot_collected = sum(collected.values())
meas_sites = [(sid, measured[sid][0], collected.get(sid,0), measured[sid][1]) for sid in measured]
meas_src = sum(x[1] for x in meas_sites)
meas_col = sum(x[2] for x in meas_sites)
unmeasured = [sid for sid in all_sites if sid not in measured]
unmeas_collected = sum(collected.get(sid,0) for sid in unmeasured)

print("="*64)
print("전체 총량(케파) 집계")
print("="*64)
print(f"총 사이트: {len(all_sites)}")
print(f"보유(수집): {tot_collected:,}건")
print(f"측정된 사이트: {len(measured)}개  (API {len(cov and [r for r in cov if num(r.get('source_total'))])} + custom {len([r for r in cust if num(r.get('source_total'))])}, HAL글로벌 제외)")
print(f"  측정분 가용(소스) 합계: {meas_src:,}건")
print(f"  측정분 우리 수집:       {meas_col:,}건  → 커버리지 {100*meas_col/meas_src:.1f}%")
print(f"  측정분 부족분:          {meas_src-meas_col:,}건")
print(f"미측정 사이트: {len(unmeasured)}개  (보유 floor {unmeas_collected:,}건 — 실제 총량은 더 큼)")

# --- crawler health from custom CSV notes ---
DEAD = ("http_5","http_4","timeout","conn","dns","refused","ssl","error")
ALIVE = ("not_json","http_2","http_3","no_total","html")
def classify(note):
    if not note: return "미상"
    reasons = re.findall(r":([a-z0-9_]+)", note.lower())
    if not reasons: reasons = re.findall(r"[a-z_]+\d*", note.lower())
    reach = any(any(a in r for a in ALIVE) for r in reasons)
    if reach: return "정상(살아있음,못셈)"
    dead = any(any(d in r for d in DEAD) for r in reasons)
    return "죽음/도달불가" if dead else "미상"

health = collections.Counter()
dead_sites = []
for r in cust:
    if num(r.get("source_total")):
        health["측정됨(정상)"] += 1; continue
    cls = classify(r.get("note",""))
    health[cls]+=1
    if cls=="죽음/도달불가":
        dead_sites.append((r["site_id"], sheet.get(r["site_id"],""), collected.get(r["site_id"],0), r.get("note","")[:90]))

# collected==0 / low from DB
zero=[sid for sid in all_sites if collected.get(sid,0)==0]
low=[sid for sid in all_sites if 0<collected.get(sid,0)<=5]

print("\n"+"="*64); print("크롤러 건강검진"); print("="*64)
for k,v in health.most_common(): print(f"  {k}: {v}")
print(f"  수집 0건(완전실패): {len(zero)}개 -> {zero}")
print(f"  수집 1~5건(저조): {len(low)}개")
print(f"\n죽음/도달불가 후보 {len(dead_sites)}개 (상위 20):")
for sid,sh,col,note in dead_sites[:20]:
    print(f"  {sid[:32]:<32} 수집{col:>5} [{sh[:14]}] {note}")

# --- write health CSV ---
out = AUD / "crawler_health.csv"
with open(out,"w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f); w.writerow(["site_id","sheet","collected","health","note"])
    for r in cust:
        sid=r["site_id"]
        cls = "측정됨(정상)" if num(r.get("source_total")) else classify(r.get("note",""))
        w.writerow([sid, sheet.get(sid,""), collected.get(sid,0), cls, r.get("note","")])
    for sid in zero:
        w.writerow([sid, sheet.get(sid,""), 0, "수집0건(완전실패)", ""])
print(f"\n-> {out} 저장 ({len(cust)+len(zero)}행)")
