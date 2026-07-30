# -*- coding: utf-8 -*-
"""근접중복 후보 → 확정 중복 수 산출 (LLM 판정 + union-find, 읽기 전용).

입력:  scripts/audit/out/neardup_candidates.csv  (neardup_candidates.py 산출)
절차:
  1) 자동 확정(LLM 불필요):
       - already_exact_dup=1  (meta_url+title 동일)
       - same_sha256=1        (PDF 내용 바이트 동일)
     → SAME
  2) 제목 동일(정규화) & 미확정 쌍 → qwen3:8b 판정 (SAME/VERSION/DIFFERENT)
  3) 제목 다른 쌍 → boilerplate 오탐으로 간주(중복 아님).
     단, 무작위 SAMPLE개를 LLM으로 검증해 그 가정의 오차를 보고.
  4) 'SAME' 확정 쌍으로 union-find → 클러스터 → 제거가능 잉여 = Σ(size-1)

OOM 안전: ABS_CAP=1200, num_ctx=4096, GPU당 1요청(양쪽 엔드포인트 라운드로빈).

Usage:
    .venv-embed/bin/python scripts/audit/adjudicate_dups.py [--sample 300] [--max-judge 0]
"""
import argparse, csv, sqlite3, json, time, urllib.request, os
from concurrent.futures import ThreadPoolExecutor

REPO = "/data_raid/ruci_workspace/frwaler_job"
OUT = f"{REPO}/scripts/audit/out"
DB = f"{REPO}/libertree-app/data/libertree.db"
CAND = f"{OUT}/neardup_candidates.csv"
ENDPOINTS = ["http://127.0.0.1:11435/api/generate", "http://127.0.0.1:11436/api/generate"]
MODEL = "qwen3:8b"
ABS_CAP = 1200
NUM_CTX = 4096


def norm(s):
    return " ".join((s or "").lower().split())


class UF:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[ra] = rb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=300, help="제목-다른 쌍 검증 표본 수")
    ap.add_argument("--max-judge", type=int, default=0, help="제목-동일 LLM 판정 상한(0=전량)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(CAND)))
    print(f"[load] 후보쌍 {len(rows):,}")

    auto_same = []      # (a,b) 자동 확정
    to_judge = []       # (a,b) 제목동일 LLM 판정 대상
    diff_title = []     # (a,b) 제목다름(오탐 추정)
    for r in rows:
        a, b = int(r["seq_a"]), int(r["seq_b"])
        if r["already_exact_dup"] == "1" or r["same_sha256"] == "1":
            auto_same.append((a, b))
        elif norm(r["title_a"]) == norm(r["title_b"]) and r["title_a"].strip():
            to_judge.append((a, b, r["title_a"]))
        else:
            diff_title.append((a, b))
    print(f"  자동확정(exact/sha256) : {len(auto_same):,}")
    print(f"  제목동일 → LLM 판정    : {len(to_judge):,}")
    print(f"  제목다름 → 오탐추정    : {len(diff_title):,}")

    con = sqlite3.connect(DB); q = con.cursor().execute

    def meta(s):
        r = q("SELECT title,abstract FROM documents WHERE seq_id=?", (int(s),)).fetchone()
        return ((r[0] or "")[:200], (r[1] or "")[:ABS_CAP]) if r else ("", "")

    # 메타 캐시(판정 대상 + 표본만)
    judge_list = to_judge if args.max_judge == 0 else to_judge[:args.max_judge]
    import random
    sample_diff = random.Random(0).sample(diff_title, min(args.sample, len(diff_title)))
    cache = {}
    for a, b, *_ in judge_list:
        cache.setdefault(a, meta(a)); cache.setdefault(b, meta(b))
    for a, b in sample_diff:
        cache.setdefault(a, meta(a)); cache.setdefault(b, meta(b))
    con.close()

    def judge(item):
        idx, a, b = item
        ta, aa = cache[a]; tb, ab = cache[b]
        prompt = (f"문서A 제목: {ta}\n초록: {aa}\n\n문서B 제목: {tb}\n초록: {ab}\n\n"
                  "두 문서의 관계를 한 단어로만 답하라: SAME(동일문서) / VERSION(같은 문서의 다른 판·연도) / DIFFERENT(별개 문서).")
        d = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "think": False,
                        "options": {"num_predict": 8, "num_ctx": NUM_CTX}}).encode()
        url = ENDPOINTS[idx % len(ENDPOINTS)]
        req = urllib.request.Request(url, data=d, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read()).get("response", "").upper()
        except Exception:
            return (a, b, "ERROR")
        v = "SAME" if "SAME" in resp else "VERSION" if "VERSION" in resp else "DIFFERENT" if "DIFFERENT" in resp else "?"
        return (a, b, v)

    # ---- 제목동일 판정 ----
    t0 = time.time()
    verd = {"SAME": 0, "VERSION": 0, "DIFFERENT": 0, "?": 0, "ERROR": 0}
    same_pairs = list(auto_same)
    items = [(i, a, b) for i, (a, b, *_ ) in enumerate(judge_list)]
    print(f"\n[judge] 제목동일 {len(items):,}쌍 LLM 판정 (양쪽 GPU)...")
    done = 0
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
        for a, b, v in ex.map(judge, items):
            verd[v] += 1
            if v == "SAME": same_pairs.append((a, b))
            done += 1
            if done % 500 == 0:
                print(f"    {done:,}/{len(items):,}  ({done/(time.time()-t0):.1f}/s)", flush=True)
    print(f"  판정결과: {dict(verd)}  ({time.time()-t0:.0f}s)")

    # ---- 제목다름 표본 검증(오탐 가정 확인) ----
    sv = {"SAME": 0, "VERSION": 0, "DIFFERENT": 0, "?": 0, "ERROR": 0}
    if sample_diff:
        sitems = [(i, a, b) for i, (a, b) in enumerate(sample_diff)]
        with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
            for a, b, v in ex.map(judge, sitems):
                sv[v] += 1
        tot = sum(sv.values())
        fp_same = sv["SAME"]
        print(f"\n[표본검증] 제목다름 {tot}쌍 중 실제 SAME: {fp_same} ({fp_same/max(1,tot)*100:.1f}%)  → 나머지는 오탐 확인")
        print(f"  상세: {dict(sv)}")

    # ---- union-find → 확정 중복 수 ----
    uf = UF()
    for a, b in same_pairs:
        uf.union(a, b)
    comp = {}
    for x in uf.p:
        comp.setdefault(uf.find(x), set()).add(x)
    clusters = [c for c in comp.values() if len(c) > 1]
    excess = sum(len(c) - 1 for c in clusters)
    docs_in = sum(len(c) for c in clusters)

    print("\n" + "=" * 60)
    print("확정 중복 (SAME 판정 기준)")
    print("=" * 60)
    print(f"  확정 SAME 쌍          : {len(same_pairs):,}  (자동 {len(auto_same):,} + LLM {verd['SAME']:,})")
    print(f"  중복 클러스터 수       : {len(clusters):,}")
    print(f"  클러스터에 속한 문서   : {docs_in:,}")
    print(f"  ** 제거가능 잉여 문서 : {excess:,} **  (각 군에서 1개 원본 유지)")
    print(f"  VERSION(다른 판, 보존): {verd['VERSION']:,}")
    con2 = sqlite3.connect(DB)
    tot = con2.execute("SELECT COUNT(*) FROM documents").fetchone()[0]; con2.close()
    print(f"\n  전체 {tot:,} 대비 확정 잉여 {excess:,} = {excess/tot*100:.2f}%")
    print(f"  → 고유 문서 수 ≈ {tot-excess:,}")

    # 클러스터 저장
    with open(f"{OUT}/confirmed_dup_clusters.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["cluster_id", "size", "seq_ids"])
        for i, c in enumerate(sorted(clusters, key=len, reverse=True)):
            w.writerow([i, len(c), " ".join(map(str, sorted(c)))])
    print(f"\n[out] {OUT}/confirmed_dup_clusters.csv")


if __name__ == "__main__":
    main()
