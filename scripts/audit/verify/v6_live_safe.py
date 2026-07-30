# -*- coding: utf-8 -*-
"""V6(안전): 라이브 진단 — 프로덕션 무오염(읽기전용 네트워크 + 코드 정적분석).

A. DEFECT 14개: 저장된 pdf_url을 지금 GET → 실제로 뭐가 오는지(PDF/HTML/로그인/403).
   왜 HTML을 받았는지 근본원인 특정. (파일 저장 안 함; 헤더+첫 바이트만)
B. NO_DATA custom 크롤러: 파일을 읽어 '테스트 스텁 vs 실크롤러' 정적 분류.

DB/블롭/프로덕션에 아무것도 쓰지 않는다.
"""
import sqlite3, subprocess, os, glob, re
from collections import defaultdict

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"

DEFECT = ["gri-re-kr-web","book-ioj-go-kr-library","dtic-dimensions-ai-discover","comwel-or-kr-comwel",
          "research-thea-ie-browse","rdr-kuleuven-be-dataverse","pps-go-kr-kor","krihs-re-kr-krihslibraryreport",
          "sodha-be-dataverse","economie-fgov-be-nl","idiap-ch-en","didaktorika-gr-eadd",
          "en-ird-fr-sections","issnationallab-org-about"]

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print("=== V6-A: DEFECT 크롤러 pdf_url 실시간 GET (지금 뭐가 오나) ===")
for site in DEFECT:
    row = con.execute("SELECT pdf_url FROM documents WHERE site_id=? AND COALESCE(pdf_url,'')<>'' LIMIT 1", (site,)).fetchone()
    if not row:
        print(f"  {site[:34]:34s}: pdf_url 없음"); continue
    url = row[0]
    try:
        # 헤더
        h = subprocess.run(["curl","-sIL","--max-time","15","-A","Mozilla/5.0","-o","/dev/null",
                            "-w","%{http_code} %{content_type}", url], capture_output=True, text=True, timeout=25)
        code_ct = h.stdout.strip()
        # 첫 바이트
        b = subprocess.run(["curl","-sL","--max-time","15","-A","Mozilla/5.0","--range","0-300", url],
                           capture_output=True, timeout=25).stdout[:120]
        sig = "PDF" if b[:5]==b"%PDF-" else "HTML" if (b"<html" in b[:120].lower() or b"<!doctype" in b[:120].lower()) else b[:40]
        print(f"  {site[:34]:34s}: {code_ct[:40]:40s} | 실제:{sig}")
    except Exception as e:
        print(f"  {site[:34]:34s}: FETCH_ERR {str(e)[:30]}")
con.close()

# --- V6-B: NO_DATA custom 크롤러 정적 분류 ---
NO_DATA = ["alrc-gov-au","better-fsc-go-kr-fsc-new","collections-nlm-nih-gov","compareschoolrankings-org",
           "data-ademe-fr","data-cso-ie","digar-ee","eia-gov-reports","fsc","fsc-better-testcodex",
           "ga-gov-au-search","healthnz-figshare-com","ir-amolf-nl","mohw","mpi-govt-nz-forestry",
           "news-admin-ch-en","news-va-gov-va-press-room","observa-minciencia-gob-cl","openstarts-units-it",
           "order-nia-nih-gov-view-all-publication","repository-rcsi-com","sozialministerium-gv-at",
           "statbel-fgov-be-nl","state-gov-plans-performance-bu","ntrs"]
print("\n=== V6-B: NO_DATA custom 크롤러 정적 분류 ===")
CUSTOM = f"{REPO}/crawler/sites/custom"
def find_file(sid):
    for cand in [f"{CUSTOM}/{sid}.py", f"{CUSTOM}/{sid.replace('-','_')}.py",
                 f"{REPO}/crawler/sites/{sid}.py"]:
        if os.path.exists(cand): return cand
    hits = glob.glob(f"{CUSTOM}/{sid.replace('-','?')}.py")
    return hits[0] if hits else None
for sid in NO_DATA:
    fp = find_file(sid)
    if not fp: print(f"  {sid[:36]:36s}: 파일못찾음"); continue
    txt = open(fp, encoding="utf-8", errors="replace").read()
    lines = txt.count("\n")
    is_test = bool(re.search(r"test|codex|stub|todo|placeholder|NotImplemented", txt, re.I)) and lines < 60
    has_main = "__main__" in txt or "def crawl" in txt or "BaseCrawler" in txt
    tag = "스텁/테스트?" if (is_test or lines < 40) else ("실크롤러" if has_main else "구조미약")
    print(f"  {os.path.basename(fp)[:34]:34s} {lines:>4}줄  {tag}")
