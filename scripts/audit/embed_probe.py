# -*- coding: utf-8 -*-
"""임베딩 처리량 + peak VRAM 실측 (OOM 안전성 검증용).

- 길이 구간별로 초록을 표본추출(긴 이상치 포함)
- CHAR_CAP 로 truncation 후 배치로 임베딩
- 배치마다 GPU1 사용 VRAM을 샘플링해 peak 추적
- 처리량으로 481,669건 총 소요시간 투영

읽기 전용. 모델: qwen3-embedding:0.6b @ GPU1(:11435)
"""
import sqlite3, time, subprocess, urllib.request, json

DB = "/data_raid/ruci_workspace/frwaler_job/libertree-app/data/libertree.db"
URL = "http://127.0.0.1:11435/api/embed"
MODEL = "qwen3-embedding:0.6b"
CHAR_CAP = 2000       # truncation 상한(문자)
BATCH = 64
TOTAL_DOCS = 481669


def gpu1_used_mib():
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-i", "1"]
    )
    return int(out.decode().strip())


def embed(batch):
    data = json.dumps({"model": MODEL, "input": batch}).encode()
    req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


def main():
    con = sqlite3.connect(DB)
    q = con.cursor().execute
    # 구간별 표본 (긴 이상치 반드시 포함)
    sample = []
    for lo, hi, n in [(0,500,300),(500,2000,300),(2000,8000,250),(8000,32000,120),(32000,10**12,30)]:
        rows = q("""SELECT abstract FROM documents
                    WHERE LENGTH(COALESCE(abstract,''))>=? AND LENGTH(COALESCE(abstract,''))<?
                    AND abstract IS NOT NULL LIMIT ?""", (lo, hi, n)).fetchall()
        sample += [r[0] for r in rows]
    con.close()
    print(f"표본 {len(sample)}건 (긴 이상치 포함), CHAR_CAP={CHAR_CAP}, BATCH={BATCH}")

    base = gpu1_used_mib()
    peak = base
    dim = 0
    t0 = time.time()
    done = 0
    for i in range(0, len(sample), BATCH):
        batch = [ (a or "")[:CHAR_CAP] for a in sample[i:i+BATCH] ]
        e = embed(batch)
        dim = len(e[0]) if e else dim
        done += len(batch)
        peak = max(peak, gpu1_used_mib())
    dt = time.time() - t0

    rate = done / dt
    print(f"\n처리 {done}건 / {dt:.1f}s  ->  {rate:.0f} docs/s")
    print(f"차원: {dim}")
    print(f"GPU1 VRAM: 시작 {base} MiB -> peak {peak} MiB  (증가 {peak-base} MiB)")
    eta = TOTAL_DOCS / rate
    print(f"\n481,669건 투영: {eta/60:.1f}분  (~{eta/3600:.2f}시간)")
    print(f"임베딩 저장(481,669 x {dim} float32): {TOTAL_DOCS*dim*4/1e9:.2f} GB (RAM/디스크)")


if __name__ == "__main__":
    main()
