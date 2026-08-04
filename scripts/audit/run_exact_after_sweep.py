# -*- coding: utf-8 -*-
"""sweep(count_only_c_full) 결과에서 '캡 걸림'+'의심' 사이트를 골라 exact_probe 실행.

대상 선정 (master + part 병합 기준):
  - 캡: completed == False           (2h 상한에 잘린 하한값)
  - 의심: completed == True & counted < 3  (크롤러가 거의 못 긁음)
정확값은 scripts/audit/exact_probe.csv 에 누적(--resume 로 재개 가능).
"""
import csv, sys, importlib.util
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
AUD = ROOT / "scripts/audit"
SUSPECT_MAX = 3
WORKERS = "6"
WALK_WALL = "1200"   # 사이트당 walk 최대 20분 (배치병렬이라 대개 그 전에 완주)


def num(x):
    try:
        return int(float(x))
    except Exception:
        return None


def load(p):
    p = Path(p)
    if not p.exists():
        return []
    return list(csv.DictReader(open(p, encoding="utf-8", newline="")))


# master + part 병합(part=최신 우선)
by = {}
for r in load(AUD / "count_only_c_full.csv"):
    by[r["site_id"]] = r
for r in load(AUD / "count_only_c_part.csv"):
    by[r["site_id"]] = r

capped, suspect = [], []
for sid, r in by.items():
    completed = str(r.get("completed", "")).lower() == "true"
    cnt = num(r.get("counted"))
    if not completed:
        capped.append(sid)
    elif cnt is not None and cnt < SUSPECT_MAX:
        suspect.append(sid)

targets = list(dict.fromkeys(capped + suspect))
print(f"[exact-runner] 캡 {len(capped)} + 의심 {len(suspect)} = 대상 {len(targets)}개", flush=True)
if not targets:
    print("[exact-runner] 대상 없음 — 종료", flush=True)
    sys.exit(0)

sys.argv = [
    "exact_probe.py",
    "--only", *targets,
    "--workers", WORKERS,
    "--walk-wall", WALK_WALL,
    "--resume",
    "--out-csv", str(AUD / "exact_probe.csv"),
]
spec = importlib.util.spec_from_file_location("ep", str(AUD / "exact_probe.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.main()
print("[exact-runner] 완료 → scripts/audit/exact_probe.csv", flush=True)
