# -*- coding: utf-8 -*-
"""libertree 전 문서 초록 임베딩 생성 (양쪽 GPU, 읽기 전용).

- DB의 모든 문서(seq_id, abstract||title)를 CHAR_CAP로 truncation 후 임베딩.
- GPU1(:11435) + GPU0(:11436) 두 ollama 러너에 배치를 번갈아 보내 병렬.
- 결과를 증분 저장(정렬 유지): embeddings.f32 (float32 raw), embed_seqids.i64 (int64 raw).
- 재실행 시 이미 완료된 seq_id는 건너뜀(resume).

산출:
    scripts/audit/out/embeddings.f32     # N x 1024 float32
    scripts/audit/out/embed_seqids.i64   # N int64, 행 정렬 동일
나중에 venv에서: np.fromfile('embeddings.f32',dtype='float32').reshape(-1,1024)
"""
import sqlite3, time, json, array, threading, os, sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
OUT = f"{REPO}/scripts/audit/out"
ENDPOINTS = ["http://127.0.0.1:11435/api/embed", "http://127.0.0.1:11436/api/embed"]
MODEL = "qwen3-embedding:0.6b"
CAP = 2000
BATCH = 64
DIM = 1024

emb_f = f"{OUT}/embeddings.f32"
seq_f = f"{OUT}/embed_seqids.i64"
lock = threading.Lock()
_count = [0]
_t0 = [0.0]


def embed(batch_texts, url):
    d = json.dumps({"model": MODEL, "input": batch_texts}).encode()
    req = urllib.request.Request(url, data=d, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["embeddings"]


def worker(args):
    idx, rows = args                      # rows: list[(seq_id, text)]
    url = ENDPOINTS[idx % len(ENDPOINTS)]
    texts = [(t or "")[:CAP] for _, t in rows]
    try:
        vecs = embed(texts, url)
    except Exception as e:
        with lock:
            sys.stderr.write(f"[batch {idx}] ERROR {e}\n"); sys.stderr.flush()
        return
    with lock:
        with open(emb_f, "ab") as ef, open(seq_f, "ab") as sf:
            for (seq, _), v in zip(rows, vecs):
                array.array("f", v).tofile(ef)
                array.array("q", [int(seq)]).tofile(sf)
        _count[0] += len(rows)
        n = _count[0]
        if n % 6400 < BATCH:
            el = time.time() - _t0[0]
            rate = n / el if el else 0
            print(f"  {n:>7,} embedded  {rate:5.0f} docs/s  ({el/60:.1f}m elapsed)", flush=True)


def main():
    # resume: 이미 완료된 seq_id
    done = set()
    if os.path.exists(seq_f):
        a = array.array("q"); a.frombytes(open(seq_f, "rb").read()); done = set(a)
        print(f"[resume] 기존 {len(done):,}건 건너뜀", flush=True)

    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT seq_id, COALESCE(NULLIF(TRIM(abstract),''), title, '') FROM documents ORDER BY seq_id"
    ).fetchall()
    con.close()
    rows = [(s, t) for (s, t) in rows if s not in done]
    print(f"[start] 대상 {len(rows):,}건, 배치 {BATCH}, 엔드포인트 {len(ENDPOINTS)}개(GPU0+GPU1)", flush=True)

    batches = [(bi, rows[i:i+BATCH]) for bi, i in enumerate(range(0, len(rows), BATCH))]
    _t0[0] = time.time()
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
        list(ex.map(worker, batches))

    el = time.time() - _t0[0]
    total = (len(done) + _count[0])
    print(f"\n[done] 이번 실행 {_count[0]:,}건 / {el/60:.1f}분  (누적 {total:,})", flush=True)
    # 정합성 체크
    nseq = os.path.getsize(seq_f) // 8
    nemb = os.path.getsize(emb_f) // (4 * DIM)
    print(f"[check] seq_ids={nseq:,}  embeddings={nemb:,}  {'OK' if nseq==nemb else 'MISMATCH!'}", flush=True)


if __name__ == "__main__":
    main()
