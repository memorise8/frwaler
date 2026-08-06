# -*- coding: utf-8 -*-
"""718 ok 사이트를 delivery_health(실제 미니크롤)로 엄밀 재검증 — 16 병렬 청크.

각 청크를 별도 프로세스로 실행(delivery_health.py --targets --out), 완료 후 병합.
최종: HEALTHY=된다, 그 외=안된다(사유). 무저장(temp만).
"""
import csv
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
AUD = ROOT / "scripts/audit"
NCHUNK = 16

ok = [r["site_id"] for r in csv.DictReader(open(AUD / "working_sites.csv"))
      if r["evidence"].startswith("ok")]
print(f"재검증 대상(ok): {len(ok)}곳, 청크 {NCHUNK}", flush=True)

wdir = AUD / "ok_verify_chunks"
wdir.mkdir(exist_ok=True)
chunks = [ok[i::NCHUNK] for i in range(NCHUNK)]

procs = []
for i, ch in enumerate(chunks):
    if not ch:
        continue
    tf = wdir / f"targets_{i}.txt"
    tf.write_text("\n".join(ch) + "\n")
    oc = wdir / f"out_{i}.csv"
    lf = open(wdir / f"log_{i}.txt", "w")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.Popen(
        [sys.executable, str(AUD / "delivery_health.py"), "--targets", str(tf), "--out", str(oc)],
        stdout=lf, stderr=lf, env=env)
    procs.append((p, oc, lf))

# 진행 폴링 (python time.sleep — 백그라운드 프로세스 내부라 무방)
while any(p.poll() is None for p, _, _ in procs):
    done = sum(1 for p, _, _ in procs if p.poll() is not None)
    n = 0
    for _, oc, _ in procs:
        if oc.exists():
            try:
                n += max(0, sum(1 for _ in open(oc)) - 1)
            except Exception:
                pass
    print(f"[{time.strftime('%H:%M:%S')}] 청크완료 {done}/{len(procs)}, 검증 ~{n}/{len(ok)}", flush=True)
    time.sleep(30)

# 병합
rows = []
for p, oc, lf in procs:
    lf.close()
    if oc.exists():
        rows += list(csv.DictReader(open(oc)))

out = AUD / "ok_verify.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["site_id", "prev_bucket", "verdict", "saved",
                                      "elapsed_s", "signal", "err", "out_tail"])
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in w.fieldnames})

import collections
d = collections.Counter(r["verdict"] for r in rows)
works = d.get("HEALTHY", 0)
print(f"\n=== ok 재검증 완료: {len(rows)}곳 ===", flush=True)
print(f"  된다(HEALTHY): {works}")
print(f"  안된다: {len(rows) - works}  {dict(d)}")
print(f"-> {out}", flush=True)
