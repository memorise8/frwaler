# -*- coding: utf-8 -*-
"""V4: 내용 정합성 LLM 표본 검증 (읽기 전용, 양쪽 GPU).

구조검사(필드 채움)가 못 잡는 '채워졌지만 무의미/엉뚱'을 표본으로 확인.
데이터 보유 크롤러마다 최대 N건 표본 → qwen3:8b 판정:
  OK       : 제목·초록(·본문)이 일치하는 실제 문서 레코드
  JUNK     : 제목/초록이 실제 문서가 아님(네비/에러/무의미)
  MISMATCH : 메타는 멀쩡하나 본문(추출텍스트)이 딴 문서

OOM 안전: ABS_CAP/TXT_CAP, num_ctx=4096, 엔드포인트 라운드로빈.
출력: scripts/audit/out/v4_content_llm.csv + 요약
"""
import sqlite3, json, csv, random, urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
BLOB = Path(f"{REPO}/libertree")
ENDPOINTS = ["http://127.0.0.1:11435/api/generate", "http://127.0.0.1:11436/api/generate"]
MODEL = "qwen3:8b"
ABS_CAP, TXT_CAP, NUM_CTX = 800, 800, 4096
PER = 2   # 크롤러당 표본 수

def bpath(seq, ext):
    s = str(seq).zfill(12)
    return BLOB / s[:4] / s[4:8] / f"{s}.{ext}"

con = sqlite3.connect(f"file:{REPO}/libertree-app/data/libertree.db?mode=ro", uri=True)
sites = [r[0] for r in con.execute("SELECT DISTINCT site_id FROM documents")]
rng = random.Random(42)
samples = []   # (site, seq, title, abstract, text_extracted)
for s in sites:
    rows = con.execute(
        "SELECT seq_id,title,abstract,text_extracted FROM documents WHERE site_id=? ORDER BY seq_id", (s,)
    ).fetchall()
    pick = rng.sample(rows, min(PER, len(rows)))
    for seq, ti, ab, te in pick:
        samples.append((s, seq, ti or "", ab or "", te))
con.close()
print(f"표본 {len(samples)}건 ({len(sites)} 크롤러 x ~{PER})", flush=True)

def readtxt(seq):
    p = bpath(seq, "txt")
    try: return p.read_text("utf-8", "replace")[:TXT_CAP]
    except OSError: return ""

def judge(item):
    idx, (site, seq, ti, ab, te) = item
    txt = readtxt(seq) if te == 1 else ""
    body = f"\n본문 발췌: {txt}" if txt else "\n(추출 본문 없음)"
    prompt = (f"수집된 문서 레코드를 평가하라.\n제목: {ti[:200]}\n초록/서지: {ab[:ABS_CAP]}{body}\n\n"
              "이 레코드가 실제 문서로 타당한가? 한 단어로만 답하라:\n"
              "OK(제목·초록·본문이 서로 맞는 실제 문서) / "
              "JUNK(제목·초록이 실제 문서가 아님: 네비게이션·에러·무의미) / "
              "MISMATCH(메타는 멀쩡하나 본문이 전혀 다른 문서).")
    d = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "think": False,
                    "options": {"num_predict": 8, "num_ctx": NUM_CTX}}).encode()
    url = ENDPOINTS[idx % len(ENDPOINTS)]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=d, headers={"Content-Type": "application/json"}), timeout=180) as r:
            resp = json.loads(r.read()).get("response", "").upper()
    except Exception:
        return (site, seq, "ERR")
    v = "MISMATCH" if "MISMATCH" in resp else "JUNK" if "JUNK" in resp else "OK" if "OK" in resp else "?"
    return (site, seq, v)

items = list(enumerate(samples))
res = []
done = 0
with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
    for r in ex.map(judge, items):
        res.append(r); done += 1
        if done % 300 == 0: print(f"  {done}/{len(items)}", flush=True)

verd = Counter(v for _, _, v in res)
by_site = defaultdict(Counter)
for s, _, v in res: by_site[s][v] += 1
with open(f"{OUT}/v4_content_llm.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["site_id", "seq_id", "verdict"]); w.writerows(res)

n = len(res)
print("\n=== V4 내용 정합성 결과 ===")
for k in ("OK", "JUNK", "MISMATCH", "?", "ERR"):
    print(f"  {k:9s}: {verd[k]:>5} ({verd[k]/n*100:.1f}%)")
# 문제 크롤러(표본 전부 JUNK/MISMATCH)
bad = [s for s, c in by_site.items() if c["OK"] == 0 and (c["JUNK"] + c["MISMATCH"]) > 0]
print(f"\n표본이 전부 JUNK/MISMATCH인 크롤러: {len(bad)}")
for s in sorted(bad)[:30]: print(f"  {s}: {dict(by_site[s])}")
print(f"\n[out] {OUT}/v4_content_llm.csv")
