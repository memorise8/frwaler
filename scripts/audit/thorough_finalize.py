# -*- coding: utf-8 -*-
"""제대로: 1h 완주 + 현재 재측정 완료 대기 → crawler_cap 전체 재식별
→ 개선판 하네스(page+wall 캡 무력화)로 재측정 → 최종 병합.

전 과정 count-only(무저장). 프로세스 이름 기준 대기(오래된 PID 문제 회피).
"""
import csv, glob, os, re, subprocess, sys, time

ROOT = "/data_raid/ruci_workspace/frwaler_job"
AUD = f"{ROOT}/scripts/audit"
LOGDIR = f"{AUD}/out/count_only_logs"

def log(m): print(f"[thorough {time.strftime('%H:%M:%S')}] {m}", flush=True)

def running(marker):
    """이름 marker를 가진 프로세스(자기 자신 제외)가 있으면 True."""
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            cl = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if marker in cl and "thorough_finalize" not in cl:
            return True
    return False

# 1) 현재 진행 중인 1h 런 + 조기 재측정 오케스트레이터가 끝날 때까지 대기
log("1h 재측정 + 조기 오케스트레이터 종료 대기...")
while running("run_capped_1h.py") or running("finish_capacity_pipeline.py"):
    time.sleep(60)
# 조기 오케스트레이터가 띄운 harness도 확실히 끝나도록
while running("count_only_harness.py --only"):
    time.sleep(30)
log("모든 선행 재측정 종료 확인")
time.sleep(3)

# 2) crawler_cap 전체 재식별 (완전한 데이터 기준)
def num(x):
    try: return int(float(x))
    except: return None

CAP_RE = re.compile(r"safety cap|MAX_PAGES|budget reached|wall-clock|budget exceeded", re.I)
completed = set()
for path in ["count_only_totals.csv", "count_only_capped_1h.csv"]:
    p = f"{AUD}/{path}"
    if not os.path.exists(p): continue
    for r in csv.DictReader(open(p)):
        if r.get("completed", "").lower() == "true" and num(r.get("counted")) is not None:
            completed.add(r["site_id"])

crawler_cap = sorted(sid for sid in completed
                     if glob.glob(f"{LOGDIR}/{sid}.log")
                     and CAP_RE.search(open(glob.glob(f"{LOGDIR}/{sid}.log")[0], errors="replace").read()))
log(f"crawler_cap 전체(완전데이터): {len(crawler_cap)}개")
open(f"{AUD}/crawler_cap_sites_full.txt", "w").write("\n".join(crawler_cap))

# 3) 개선판 하네스로 재측정 (neutralize_caps 자동, argv 직접 전달)
if crawler_cap:
    log(f"개선판 재측정 시작 (site-timeout 1800, workers 10)")
    cmd = [sys.executable, f"{AUD}/count_only_harness.py",
           "--only", *crawler_cap,
           "--site-timeout", "1800", "--workers", "10", "--delay", "0.4",
           "--out-csv", f"{AUD}/count_only_uncapped.csv",
           "--out-md", f"{AUD}/count_only_uncapped.md"]
    subprocess.run(cmd, env=dict(os.environ, PYTHONPATH=ROOT))
    log("개선판 재측정 종료")
else:
    log("crawler_cap 없음")

# 4) 최종 병합/보고서
log("finalize_capacity 실행")
subprocess.run([sys.executable, f"{AUD}/finalize_capacity.py"],
               env=dict(os.environ, PYTHONPATH=ROOT))
log("=== 파이프라인 완료 ===")
