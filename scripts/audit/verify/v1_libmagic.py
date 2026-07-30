# -*- coding: utf-8 -*-
"""V1: 파일유형 독립 재분류 (libmagic vs 손수 매직바이트).

- pdf_garbage.csv(17,584 '깨진 PDF') 전량을 `file --mime-type`로 재분류
- 정상 %PDF- 문서 표본 2000건도 file로 확인(위양성 점검)
- 내 이전 분류(HWP/Office/이미지/HTML) 집계와 교차대조
"""
import csv, subprocess, sqlite3, random
from pathlib import Path
from collections import Counter

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
BLOB = Path(f"{REPO}/libertree")

def bpath(seq):
    s = str(seq).zfill(12)
    return BLOB / s[:4] / s[4:8] / f"{s}.pdf"

def file_mimes(paths):
    """batched `file --mime-type`; returns list of mime strings aligned to paths."""
    out = subprocess.run(["file", "-b", "--mime-type"] + [str(p) for p in paths],
                         capture_output=True, text=True, timeout=120)
    return [l.strip() for l in out.stdout.splitlines()]

def bucket(mime):
    if mime == "application/pdf": return "PDF"
    if mime.startswith("image/"): return "이미지"
    if mime in ("text/html", "application/xml", "text/xml"): return "HTML/XML"
    if "msword" in mime or "officedocument" in mime or mime == "application/zip": return "Office/ZIP(hwpx)"
    if mime in ("application/x-hwp", "application/haansofthwp") or "hwp" in mime: return "HWP"
    if mime == "application/octet-stream" or "cdfv2" in mime or mime=="application/vnd.ms-office": return "OLE(hwp/octet)"
    if mime.startswith("text/"): return "text"
    return f"기타({mime})"

# --- 깨진 PDF 전량 재분류 ---
rows = list(csv.DictReader(open(f"{OUT}/pdf_garbage.csv")))
paths = [bpath(r["seq_id"]) for r in rows]
cnt = Counter()
B = 200
for i in range(0, len(paths), B):
    chunk = paths[i:i+B]
    exist = [p for p in chunk if p.exists()]
    for m in file_mimes(exist):
        cnt[bucket(m)] += 1
tot = sum(cnt.values())
print(f"=== V1: '깨진 PDF' {tot:,}건 libmagic 재분류 ===")
for k, c in cnt.most_common():
    print(f"  {k:22s}: {c:>7,} ({c/tot*100:4.1f}%)")
content = sum(c for k, c in cnt.items() if k not in ("HTML/XML", "text") and not k.startswith("기타"))
html = cnt["HTML/XML"]
print(f"\n  콘텐츠 보유(PDF/이미지/HWP/Office): {content:,} ({content/tot*100:.1f}%)")
print(f"  진짜 결함(HTML/XML): {html:,} ({html/tot*100:.1f}%)")
print("  [이전 손수분류: 콘텐츠 82%(14,406) / HTML+미상 18%(3,178)] ← 대조")

# --- 정상 PDF 표본 위양성 점검 ---
con = sqlite3.connect(f"file:{REPO}/libertree-app/data/libertree.db?mode=ro", uri=True)
allpdf = [r[0] for r in con.execute("SELECT seq_id FROM documents WHERE pdf_downloaded=1")]
con.close()
brokenset = set(int(r["seq_id"]) for r in rows)
sample = random.Random(1).sample([s for s in allpdf if s not in brokenset], 2000)
spaths = [bpath(s) for s in sample]
scnt = Counter()
for i in range(0, len(spaths), B):
    chunk = [p for p in spaths[i:i+B] if p.exists()]
    for m in file_mimes(chunk):
        scnt[bucket(m)] += 1
st = sum(scnt.values())
print(f"\n=== '정상 PDF' 표본 {st} libmagic 확인(위양성 점검) ===")
for k, c in scnt.most_common():
    print(f"  {k:22s}: {c:>5,} ({c/st*100:.1f}%)")
print(f"  → application/pdf 아닌 비율: {(st-scnt['PDF'])/st*100:.2f}% (0에 가까워야 정상)")
