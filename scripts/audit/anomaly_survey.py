# -*- coding: utf-8 -*-
"""전 문서 의미적 이상 전수조사 (LLM, 양쪽 GPU, 읽기전용 판정 + 결과 테이블).

각 문서(전 481,669건)를 qwen3:8b로 판정:
  OK       : 제목·초록(·본문)이 일치하는 실제 문서
  JUNK     : 제목/초록이 실제 문서가 아님(네비게이션·에러·무의미)
  MISMATCH : 메타는 멀쩡하나 본문이 전혀 다른 문서

결과는 libertree.db의 document_anomaly 테이블에 저장(원문 documents 불변).
재개 가능: 이미 판정된 seq_id는 건너뜀.

Usage:
    python scripts/audit/anomaly_survey.py [--limit N]
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time, threading, urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
BLOB = Path(f"{REPO}/libertree")
ENDPOINTS = ["http://127.0.0.1:11435/api/generate", "http://127.0.0.1:11436/api/generate"]
MODEL = "qwen3:8b"
ABS_CAP, TXT_CAP, NUM_CTX = 800, 800, 4096

DDL = """CREATE TABLE IF NOT EXISTS document_anomaly (
    seq_id INTEGER PRIMARY KEY REFERENCES documents(seq_id),
    verdict TEXT NOT NULL,
    model_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""


def bpath(seq, ext):
    s = str(seq).zfill(12)
    return BLOB / s[:4] / s[4:8] / f"{s}.{ext}"


def readtxt(seq):
    try:
        return bpath(seq, "txt").read_text("utf-8", "replace")[:TXT_CAP]
    except OSError:
        return ""


write_lock = threading.Lock()
_stat = {"n": 0, "t0": 0.0}
_counts = {"OK": 0, "JUNK": 0, "MISMATCH": 0, "?": 0, "ERR": 0}


def judge(item):
    idx, (seq, ti, ab, te) = item
    txt = readtxt(seq) if te == 1 else ""
    body = f"\n본문 발췌: {txt}" if txt else "\n(추출 본문 없음)"
    prompt = (f"수집된 문서 레코드를 평가하라.\n제목: {(ti or '')[:200]}\n초록/서지: {(ab or '')[:ABS_CAP]}{body}\n\n"
              "이 레코드가 실제 문서로 타당한가? 한 단어로만: "
              "OK(제목·초록·본문이 맞는 실제 문서) / JUNK(실제 문서가 아님: 네비게이션·에러·무의미) / "
              "MISMATCH(메타는 멀쩡하나 본문이 전혀 다른 문서).")
    d = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "think": False,
                    "options": {"num_predict": 8, "num_ctx": NUM_CTX}}).encode()
    url = ENDPOINTS[idx % len(ENDPOINTS)]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=d, headers={"Content-Type": "application/json"}), timeout=180) as r:
            resp = json.loads(r.read()).get("response", "").upper()
        v = "MISMATCH" if "MISMATCH" in resp else "JUNK" if "JUNK" in resp else "OK" if "OK" in resp else "?"
    except Exception:
        v = "ERR"
    return seq, v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute(DDL)
    conn.commit()

    done = {r[0] for r in conn.execute("SELECT seq_id FROM document_anomaly")}
    rows = [(s, ti, ab, te) for (s, ti, ab, te) in
            conn.execute("SELECT seq_id, title, abstract, text_extracted FROM documents ORDER BY seq_id")
            if s not in done]
    if args.limit:
        rows = rows[:args.limit]
    print(f"[survey] 대상 {len(rows):,}건 (이미 판정 {len(done):,})", flush=True)

    _stat["t0"] = time.time()
    items = list(enumerate(rows))
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
        for seq, v in ex.map(judge, items):
            with write_lock:
                conn.execute("INSERT OR IGNORE INTO document_anomaly(seq_id, verdict, model_version) VALUES(?,?,?)",
                             (seq, v, MODEL))
                _counts[v] = _counts.get(v, 0) + 1
                _stat["n"] += 1
                if _stat["n"] % 500 == 0:
                    conn.commit()
                    el = time.time() - _stat["t0"]
                    print(f"  {_stat['n']:>7,}  {_stat['n']/el:.1f}/s  {dict(_counts)}", flush=True)
    conn.commit()
    print(f"\n[done] {_stat['n']:,}건 / {(time.time()-_stat['t0'])/60:.1f}분", flush=True)
    tot = sum(_counts.values()) or 1
    for k in ("OK", "JUNK", "MISMATCH", "?", "ERR"):
        print(f"  {k:9s}: {_counts.get(k,0):>7,} ({_counts.get(k,0)/tot*100:.1f}%)")
    # 이상 집중 사이트
    print("\n== JUNK/MISMATCH 집중 사이트 TOP15 ==", flush=True)
    for sid, n in conn.execute("""SELECT d.site_id, COUNT(*) c FROM document_anomaly a
        JOIN documents d ON d.seq_id=a.seq_id WHERE a.verdict IN ('JUNK','MISMATCH')
        GROUP BY d.site_id ORDER BY c DESC LIMIT 15"""):
        print(f"  {str(sid)[:44]:44s} {n:,}")
    conn.close()


if __name__ == "__main__":
    main()
