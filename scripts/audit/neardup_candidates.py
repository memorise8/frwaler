# -*- coding: utf-8 -*-
"""임베딩 기반 근접중복 후보 추출 (faiss, 읽기 전용).

- embeddings.f32 (N x 1024 float32) + embed_seqids.i64 를 로드
- L2 정규화 후 HNSW(inner product=cosine) 인덱스로 각 문서의 top-k 이웃 검색
- cosine >= THRESHOLD 인 쌍을 근접중복 후보로 수집
- DB 메타(title/site/sha256/meta_url)로 보강하고, 이미 '정확중복'인지 표시
- 결과: neardup_candidates.csv  +  임계값별 요약

동시성 안전: 임베딩이 아직 쓰이는 중이면 완결된 행까지만 읽음.

Usage:
    .venv-embed/bin/python scripts/audit/neardup_candidates.py [--thr 0.95] [--k 10]
"""
import argparse, sqlite3, time, csv, os
import numpy as np
import faiss

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
DB = f"{REPO}/libertree-app/data/libertree.db"
DIM = 1024
EMB = f"{OUT}/embeddings.f32"
SEQ = f"{OUT}/embed_seqids.i64"


def load_aligned():
    n_emb = os.path.getsize(EMB) // (4 * DIM)
    n_seq = os.path.getsize(SEQ) // 8
    n = min(n_emb, n_seq)                      # 완결된 행까지만
    emb = np.fromfile(EMB, dtype="float32", count=n * DIM).reshape(n, DIM)
    seq = np.fromfile(SEQ, dtype="int64", count=n)
    return emb, seq, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thr", type=float, default=0.95)
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    t0 = time.time()
    emb, seq, n = load_aligned()
    print(f"[load] {n:,} vectors x {DIM}  ({time.time()-t0:.1f}s)")

    faiss.normalize_L2(emb)                    # cosine = inner product
    idx = faiss.IndexHNSWFlat(DIM, 32, faiss.METRIC_INNER_PRODUCT)
    idx.hnsw.efConstruction = 80
    idx.hnsw.efSearch = 64
    tb = time.time(); idx.add(emb); print(f"[index] HNSW built ({time.time()-tb:.1f}s)")

    ts = time.time()
    D, I = idx.search(emb, args.k + 1)          # +1: 자기 자신 포함
    print(f"[search] top-{args.k} ({time.time()-ts:.1f}s)")

    # 후보쌍 수집(밴드 리포트 정확히 나오도록 0.90까지 수집), export는 args.thr 이상만
    REPORT_MIN = min(args.thr, 0.90)
    allpairs = {}                               # (i<j) -> cosine (>=REPORT_MIN)
    for qi in range(n):
        for rank in range(args.k + 1):
            ni = I[qi, rank]
            if ni == qi or ni < 0:
                continue
            cs = float(D[qi, rank])
            if cs < REPORT_MIN:
                continue
            a, b = (qi, ni) if qi < ni else (ni, qi)
            key = (a, b)
            if key not in allpairs or cs > allpairs[key]:
                allpairs[key] = cs
    band = {0.90: 0, 0.95: 0, 0.97: 0, 0.99: 0}
    for cs in allpairs.values():
        for t in band:
            if cs >= t:
                band[t] += 1
    pairs = {k: v for k, v in allpairs.items() if v >= args.thr}
    print(f"\n[근접중복 후보쌍 수 (>= 임계값)]  (수집 임계 {args.thr})")
    for t in sorted(band, reverse=True):
        print(f"   cos >= {t:.2f} : {band[t]:>8,} 쌍")

    if not pairs:
        print("후보 없음."); return

    # 메타 보강
    con = sqlite3.connect(DB); q = con.cursor().execute
    need = set()
    for a, b in pairs:
        need.add(int(seq[a])); need.add(int(seq[b]))
    meta = {}
    need = list(need)
    for i in range(0, len(need), 900):
        chunk = need[i:i+900]
        ph = ",".join("?" * len(chunk))
        for sid, ti, si, sha, url in q(
            f"SELECT seq_id,title,site_id,pdf_sha256,meta_url FROM documents WHERE seq_id IN ({ph})", chunk):
            meta[sid] = (ti or "", si or "", sha or "", url or "")
    con.close()

    rows = []
    same_site = cross_site = same_sha = exact_dup = 0
    for (a, b), cs in pairs.items():
        sa, sb = int(seq[a]), int(seq[b])
        ta, sia, sha_a, ua = meta.get(sa, ("", "", "", ""))
        tb, sib, sha_b, ub = meta.get(sb, ("", "", "", ""))
        ss = sia == sib
        sh = sha_a and sha_a == sha_b
        ex = (ta == tb and ua == ub and ua != "")     # 이미 정확중복
        same_site += ss; cross_site += (not ss); same_sha += bool(sh); exact_dup += bool(ex)
        rows.append((sa, sb, round(cs, 4), int(ss), int(bool(sh)), int(bool(ex)), sia, sib, ta[:60], tb[:60]))

    rows.sort(key=lambda r: -r[2])
    outp = f"{OUT}/neardup_candidates.csv"
    with open(outp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq_a", "seq_b", "cosine", "same_site", "same_sha256", "already_exact_dup",
                    "site_a", "site_b", "title_a", "title_b"])
        w.writerows(rows)

    total = len(rows)
    print(f"\n[분류] 총 후보쌍 {total:,}")
    print(f"   같은 사이트   : {same_site:,}")
    print(f"   교차 사이트   : {cross_site:,}")
    print(f"   PDF 내용동일  : {same_sha:,}")
    print(f"   이미 정확중복 : {exact_dup:,}  → LLM 판정 불필요")
    print(f"   ** 신규 근접중복(정확중복 아님): {total-exact_dup:,}  ← LLM 판정 대상 **")
    print(f"\n[out] {outp}")


if __name__ == "__main__":
    main()
