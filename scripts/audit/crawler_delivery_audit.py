# -*- coding: utf-8 -*-
"""크롤러 납품 검증 전수 감사 (읽기 전용).

각 크롤러(사이트)를 '코드 + 수집 + 품질' 세 축으로 전수 확인한다.

축1 코드   : crawler/sites/custom/<site>.py 존재? 구문 유효(py_compile)? BaseCrawler 구조?
축2 수집   : DB에 해당 site_id 문서가 있는가 / 몇 건
축3 품질   : crawler_validation_final.csv 판정 (GOOD/FORMAT_MISMATCH/TRUE_DEFECT)

교차 상태:
  OK             코드+데이터+GOOD/FORMAT_MISMATCH
  DEFECT         코드+데이터지만 TRUE_DEFECT(HTML 저장)
  NO_DATA        코드는 있으나 수집 데이터 0 (미실행/실패 의심)
  ORPHAN_DATA    데이터는 있으나 크롤러 파일 없음
  BAD_CODE       구문 오류/구조 이상

출력: scripts/audit/out/crawler_delivery_audit.csv + 요약
"""
import os, csv, sqlite3, py_compile, glob

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
DB = f"{REPO}/libertree-app/data/libertree.db"
CUSTOM = f"{REPO}/crawler/sites/custom"

# --- 코드 인벤토리 ---
def normkey(s):
    return s.replace("_", "-").lower()

files = glob.glob(f"{CUSTOM}/*.py")
code = {}   # norm(site_id) -> {...}
for fp in files:
    base = os.path.basename(fp)[:-3]
    if base.startswith("__"):
        continue                      # __init__.py 등 패키지 파일 제외
    sid = normkey(base)
    ok = True
    try:
        py_compile.compile(fp, doraise=True)
    except py_compile.PyCompileError:
        ok = False
    try:
        txt = open(fp, encoding="utf-8", errors="replace").read()
    except OSError:
        txt = ""
    has_base = ("BaseCrawler" in txt) or ("def crawl" in txt) or ("class " in txt)
    code[sid] = {"syntax_ok": ok, "has_base": has_base, "lines": txt.count("\n")}

# --- DB 수집량 ---
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
dbc = {}
for sid, n in con.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id"):
    dbc[normkey(sid)] = dbc.get(normkey(sid), 0) + n
con.close()

# --- 품질 판정 ---
verdict = {}
vf = f"{OUT}/crawler_validation_final.csv"
if os.path.exists(vf):
    for r in csv.DictReader(open(vf)):
        verdict[normkey(r["site_id"])] = r["verdict"]

# --- 통합 ---
all_sids = set(code) | set(dbc)
rows = []
for sid in sorted(all_sids):
    c = code.get(sid)
    n = dbc.get(sid, 0)
    v = verdict.get(sid, "")
    if c is None:
        status = "ORPHAN_DATA"
    elif not c["syntax_ok"] or not c["has_base"]:
        status = "BAD_CODE"
    elif n == 0:
        status = "NO_DATA"
    elif v == "TRUE_DEFECT":
        status = "DEFECT"
    else:
        status = "OK"
    rows.append({
        "site_id": sid,
        "has_code": int(c is not None),
        "syntax_ok": int(c["syntax_ok"]) if c else "",
        "lines": c["lines"] if c else "",
        "docs": n,
        "quality": v,
        "status": status,
    })

rows.sort(key=lambda r: (-r["docs"] if isinstance(r["docs"], int) else 0))
with open(f"{OUT}/crawler_delivery_audit.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

from collections import Counter
st = Counter(r["status"] for r in rows)
docs_by = Counter()
for r in rows:
    if isinstance(r["docs"], int):
        docs_by[r["status"]] += r["docs"]

print(f"크롤러 파일 {len(code)}개 / DB 사이트 {len(dbc)}개 / 총문서 {sum(dbc.values()):,}\n")
print("== 납품 검증 상태 ==")
for s in ("OK", "DEFECT", "NO_DATA", "ORPHAN_DATA", "BAD_CODE"):
    print(f"  {s:12s}: {st[s]:>4} 크롤러 / {docs_by[s]:>8,}건")
delivered_ok = st["OK"]
print(f"\n  ✅ 정상 납품가능(OK): {delivered_ok}/{len(all_sids)} 크롤러 ({docs_by['OK']:,}건)")

for label, key in [("코드는 있으나 데이터 0 (NO_DATA)", "NO_DATA"),
                   ("데이터는 있으나 크롤러 파일 없음 (ORPHAN_DATA)", "ORPHAN_DATA"),
                   ("구문/구조 이상 (BAD_CODE)", "BAD_CODE"),
                   ("진짜 결함 (DEFECT, HTML저장)", "DEFECT")]:
    lst = [r for r in rows if r["status"] == key]
    if lst:
        print(f"\n== {label}: {len(lst)}개 ==")
        for r in lst[:20]:
            print(f"  {str(r['site_id'])[:44]:44s} docs={r['docs']} code={r['has_code']} q={r['quality']}")
print(f"\n[out] {OUT}/crawler_delivery_audit.csv")
