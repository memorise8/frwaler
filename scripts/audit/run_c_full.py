# -*- coding: utf-8 -*-
"""C 전수 측정 (html_list.csv 529개) — 2시간/사이트 상한, count-only 무저장, **재개 지원**.

문제: harness main()은 --out-csv를 mode="w"로 매번 새로 덮어씀 → 재시작하면 결과 소실.
해결: 이 런처가 누적(MASTER)/임시(PART) 2파일로 분리 관리.
  - 시작 시: 남아있는 PART(이전 크래시 잔여)를 MASTER로 fold → PART 삭제
  - MASTER에 이미 있는 site_id는 done → remaining만 harness에 --only 로 전달
  - harness는 PART(임시)에만 씀(덮어써도 무해) → 끝나면 PART를 MASTER로 fold
  - 중단 후 다시 이 스크립트를 돌리면 done을 건너뛰고 이어서 진행
zsh $(cat) 단어분리 회피용 python 런처 — argv 직접 전달."""
import csv, os, sys, importlib.util
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
AUD = ROOT / "scripts/audit"
CSV_IN = ROOT / "html_list.csv"
MASTER = AUD / "count_only_c_full.csv"
PART = AUD / "count_only_c_part.csv"
SITE_TIMEOUT = "7200"   # 2h/site
WORKERS = "12"
DELAY = "0.4"


def read_rows(path):
    if not path.exists():
        return [], None
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        return list(rd), rd.fieldnames


def fold(part_path, master_path):
    """PART의 데이터행을 MASTER로 병합(site_id 기준, PART=최신 우선). 원자적 재작성."""
    prows, pfields = read_rows(part_path)
    if not prows:
        return
    mrows, mfields = read_rows(master_path)
    fields = mfields or pfields
    by_id = {r["site_id"]: r for r in mrows}
    for r in prows:
        by_id[r["site_id"]] = r
    tmp = master_path.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for sid in by_id:
            w.writerow({k: by_id[sid].get(k, "") for k in fields})
    os.replace(tmp, master_path)


# --- 0) 이전 크래시 잔여 PART fold ---
fold(PART, MASTER)
try:
    PART.unlink()
except OSError:
    pass

# --- 1) 대상/완료 산정 ---
all_ids = []
with open(CSV_IN, encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        sid = (r.get("site_id") or "").strip()
        if sid:
            all_ids.append(sid)

done_rows, _ = read_rows(MASTER)
done = {r["site_id"] for r in done_rows}
remaining = [s for s in all_ids if s not in done]

print(f"[C-full] 전체 {len(all_ids)} / 완료 {len(done)} / 잔여 {len(remaining)} "
      f"(상한 {SITE_TIMEOUT}s, workers {WORKERS})", flush=True)

if not remaining:
    print("[C-full] 잔여 0 — 모든 C 사이트 측정 완료. finalize 준비됨.", flush=True)
    sys.exit(0)

# --- 2) 잔여만 harness 실행 → PART(임시)에 기록 ---
sys.argv = [
    "count_only_harness.py",
    "--only", *remaining,
    "--site-timeout", SITE_TIMEOUT,
    "--workers", WORKERS,
    "--delay", DELAY,
    "--out-csv", str(PART),
    "--out-md", str(AUD / "count_only_c_part.md"),
]
spec = importlib.util.spec_from_file_location("coh", str(AUD / "count_only_harness.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    m.main()
finally:
    # --- 3) PART → MASTER fold (정상 종료/예외 무관하게 지금까지분 보존) ---
    fold(PART, MASTER)
    try:
        PART.unlink()
    except OSError:
        pass
    _dr, _ = read_rows(MASTER)
    print(f"[C-full] fold 완료. MASTER 누적 {len(_dr)}/{len(all_ids)}", flush=True)
