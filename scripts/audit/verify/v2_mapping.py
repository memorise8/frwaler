# -*- coding: utf-8 -*-
"""V2: 크롤러↔사이트 매핑 독립 재구성 (읽기 전용).

원 감사(crawler_delivery_audit)는 custom/*.py 파일명만 셌다.
여기서는 세 소스 모두에서 '실제 등록 site_id'를 추출해 DB와 정합한다:
  - custom/*.py     : 코드 내 `site_id = "..."` (없으면 파일명)
  - configs/*.json  : JSON의 "site_id"
  - sites/*.py      : fsc/mohw/ntrs (코드 내 site_id)

출력: 각 site가 custom/config/data 중 무엇으로 뒷받침되는지 + 불일치.
"""
import os, re, json, glob, sqlite3
from collections import defaultdict

REPO = "/data_raid/ruci_workspace/frwaler_job"
CR = f"{REPO}/crawler"
OUT = f"{REPO}/scripts/audit/out"

def norm(s): return (s or "").replace("_", "-").lower().strip()

SID_RE = re.compile(r"""^\s*site_id\s*=\s*['"]([^'"]+)['"]""", re.M)

# custom + sites 최상위
custom = {}   # norm_sid -> file
for fp in glob.glob(f"{CR}/sites/custom/*.py") + glob.glob(f"{CR}/sites/*.py"):
    b = os.path.basename(fp)
    if b.startswith("__"): continue
    try: txt = open(fp, encoding="utf-8", errors="replace").read()
    except OSError: txt = ""
    m = SID_RE.search(txt)
    sid = norm(m.group(1)) if m else norm(b[:-3])
    custom[sid] = b
    # 파일명과 코드 site_id가 다르면 기록
    if m and norm(m.group(1)) != norm(b[:-3]):
        custom.setdefault("_MISMATCH_", []).append((b, m.group(1)))

# configs
config = {}   # norm_sid -> file
for fp in glob.glob(f"{CR}/sites/configs/*.json"):
    try: sid = json.load(open(fp)).get("site_id", "")
    except Exception: sid = os.path.basename(fp)[:-5]
    config[norm(sid)] = os.path.basename(fp)

mism = custom.pop("_MISMATCH_", [])

# DB
con = sqlite3.connect(f"file:{REPO}/libertree-app/data/libertree.db?mode=ro", uri=True)
db = {}
for sid, n in con.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id"):
    db[norm(sid)] = db.get(norm(sid), 0) + n
con.close()

cset, gset, dset = set(custom), set(config), set(db)
allid = cset | gset | dset

print("=== 크롤러 정의 소스 집계 ===")
print(f"  custom/sites .py : {len(cset):>4}")
print(f"  configs .json    : {len(gset):>4}")
print(f"  custom ∩ config  : {len(cset & gset):>4}  (같은 site를 둘 다 정의)")
print(f"  정의 총 고유 site: {len(cset | gset):>4}")
print(f"  DB 수집 site     : {len(dset):>4}")
print(f"\n  파일명≠코드 site_id 불일치: {len(mism)}건")
for b, real in mism[:10]:
    print(f"    파일 {b}  ->  코드 site_id '{real}'")

# 정합 상태
has_def = cset | gset
data_no_def = dset - has_def       # 진짜 ORPHAN (데이터 있는데 정의 없음)
def_no_data = has_def - dset       # 정의 있는데 데이터 0
print(f"\n=== 정합 ===")
print(f"  데이터 있는데 크롤러 정의 없음(진짜 ORPHAN): {len(data_no_def)}")
for s in sorted(data_no_def)[:20]: print(f"    {s}  (docs={db[s]})")
print(f"\n  크롤러 정의 있는데 데이터 0: {len(def_no_data)}")
# config만 있고 데이터 없는 것 vs custom
c_only_nodata = sorted(s for s in def_no_data if s in gset and s not in cset)
cu_nodata = sorted(s for s in def_no_data if s in cset)
print(f"    ├ config 정의만 있고 데이터 0: {len(c_only_nodata)}")
print(f"    └ custom 정의인데 데이터 0   : {len(cu_nodata)}")
for s in cu_nodata[:25]: print(f"        {s}")

# 원 감사 대비: custom만 783이라 했는데 실제 정의 총수
print(f"\n=== 원 감사(custom 783) 대비 정정 ===")
print(f"  실제 크롤러 정의 총 고유 site_id: {len(has_def)}  (custom {len(cset)} + config 전용 {len(gset-cset)})")
print(f"  이 중 데이터 수집 성공: {len(has_def & dset)}")
PY_END = True
