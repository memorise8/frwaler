# -*- coding: utf-8 -*-
"""전 크롤러 코드 품질 검수 — 결함 안티패턴 자동 스캔 + 헬스 교차."""
import os, re, csv, glob
ROOT="/data_raid/ruci_workspace/frwaler_job"; CUS=f"{ROOT}/crawler/sites/custom"
# 헬스 로드
health={}
for fn in ["crawler_health_probe.csv","health_timeout_recheck.csv"]:
    p=f"{ROOT}/scripts/audit/{fn}"
    if os.path.exists(p):
        for r in csv.DictReader(open(p,encoding="utf-8-sig")):
            health[r["site_id"]]=r.get("health","")   # 재측정이 우선(뒤가 덮음)

def scan(path):
    src=open(path,errors="replace").read()
    sid=os.path.basename(path)[:-3]
    flags=[]
    # 1) PDF만 다운로드(비PDF 데이터 버림) - e-stat 패턴
    if re.search(r'if\s+["\']PDF["\']\s+in\s+.*file_type|content_type.*PDF.*break', src) and "excel" not in src.lower() and "csv" not in src.lower():
        if re.search(r'file_type|download.*type', src, re.I): flags.append("PDF만수집(데이터손실의심)")
    # 2) pdf_url을 .pdf 접미사로만 추출(랜딩 미추적) - DOAJ 패턴
    if re.search(r'endswith\(["\']\.pdf["\']\)', src) and "landing" not in src.lower() and "redirect" not in src.lower():
        flags.append("PDF링크 접미사판정(랜딩미추적)")
    # 3) 초록/본문 길이 필터로 skip
    m=re.search(r'len\(\s*abstract\s*\)\s*<\s*(\d+)', src)
    if m: flags.append(f"초록<{m.group(1)}자 skip")
    # 4) 캡: _MAX_PAGES, wall-clock
    mp=re.search(r'_MAX_PAGES\s*=\s*(?:int\(os\.environ[^)]*?["\'](\d+)["\']\)|(\d+))', src)
    if mp: flags.append(f"MAX_PAGES={mp.group(1) or mp.group(2)}")
    if re.search(r'25\s*\*\s*60|1500\b|max_wall|budget', src): flags.append("25분벽시계")
    # 5) 뉴스/보도자료(문서 아님 의심)
    if re.search(r'wordpress|press.release|news.release|/news/|actualites|comunicados', src, re.I) and "publication" not in sid:
        flags.append("뉴스/보도성")
    # 6) title 없으면 skip (정상), but 과도한 필터 체크
    if re.search(r'if\s+not\s+title', src) and re.search(r'continue|skip', src): pass
    # 7) SPA/JS 의존(fragile)
    if re.search(r'playwright|browser_fetch|render|__NEXT_DATA__|selenium', src, re.I): flags.append("JS렌더의존(취약)")
    return sid, flags

rows=[]
for f in sorted(glob.glob(f"{CUS}/*.py")):
    if os.path.basename(f).startswith("__"): continue
    sid, flags = scan(f)
    rows.append((sid, health.get(sid,"?"), flags))

# 집계
import collections
flagct=collections.Counter()
for sid,h,fl in rows:
    for x in fl: flagct[re.sub(r'=\d+|<\d+자','',x)]+=1
print(f"검수 크롤러: {len(rows)}개\n")
print("=== 헬스 분포 ===")
hc=collections.Counter(h for _,h,_ in rows)
for k,v in sorted(hc.items(),key=lambda x:-x[1]): print(f"  {k:<18}{v}")
print("\n=== 결함 안티패턴 빈도 ===")
for k,v in flagct.most_common(): print(f"  {k:<28}{v}")
# 고장+결함 겹치는 위험군
print("\n=== 위험군: 고장/빈결과 + 결함 (상위) ===")
risky=[(sid,h,fl) for sid,h,fl in rows if h in ("broken_error","empty_zero_parse","timeout") and fl]
print(f"고장/느림 AND 결함보유: {len(risky)}개")
# CSV 저장
with open(f"{ROOT}/scripts/audit/crawler_audit.csv","w",newline="",encoding="utf-8-sig") as fo:
    w=csv.writer(fo); w.writerow(["site_id","health","flag_count","flags"])
    for sid,h,fl in rows: w.writerow([sid,h,len(fl),"; ".join(fl)])
print(f"\n-> crawler_audit.csv 저장 ({len(rows)}행)")
