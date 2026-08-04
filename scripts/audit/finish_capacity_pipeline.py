# -*- coding: utf-8 -*-
"""1시간 재측정 완료 대기 → crawler_cap 사이트 개선판 재측정 → 최종 병합.

전 과정 count-only(무저장). 셸 단어분리 문제 회피 위해 subprocess argv로 직접 전달.
"""
import csv, glob, os, re, subprocess, sys, time

ROOT = "/data_raid/ruci_workspace/frwaler_job"
AUD = f"{ROOT}/scripts/audit"
LOGDIR = f"{AUD}/out/count_only_logs"
PIDFILE = "/tmp/claude-1001/-data-raid-ruci-workspace-frwaler-job/36605e12-154d-421c-898f-01864a4dc07c/scratchpad/capped_pid.txt"

def log(m): print(f"[pipeline {time.strftime('%H:%M:%S')}] {m}", flush=True)

# 1) 1시간 재측정 프로세스 종료 대기
try:
    pid = int(open(PIDFILE).read().strip())
    log(f"1시간 재측정 PID {pid} 종료 대기...")
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(30)
    log("1시간 재측정 종료 확인")
except Exception as e:
    log(f"PID 대기 스킵: {e}")

time.sleep(3)

# 2) crawler_cap 사이트 식별 (completed=True 이면서 로그에 내부캡 신호)
def num(x):
    try: return int(float(x))
    except: return None

CAP_RE = re.compile(r"safety cap|MAX_PAGES|budget reached|wall-clock|budget exceeded", re.I)
completed = {}
for path in ["count_only_totals.csv", "count_only_capped_1h.csv"]:
    p = f"{AUD}/{path}"
    if not os.path.exists(p): continue
    for r in csv.DictReader(open(p)):
        if r.get("completed", "").lower() == "true" and num(r.get("counted")) is not None:
            completed[r["site_id"]] = num(r["counted"])

crawler_cap = []
for sid in completed:
    logs = glob.glob(f"{LOGDIR}/{sid}.log")
    if logs and CAP_RE.search(open(logs[0], errors="replace").read()):
        crawler_cap.append(sid)
crawler_cap = sorted(set(crawler_cap))
log(f"crawler_cap 사이트: {len(crawler_cap)}개")
open(f"{AUD}/crawler_cap_sites.txt", "w").write("\n".join(crawler_cap))

# 3) 개선판 하네스로 재측정 (neutralize_caps 자동 적용) — argv 직접 전달
if crawler_cap:
    log(f"개선판 하네스 재측정 시작 (site-timeout 1800, workers 12)")
    cmd = [sys.executable, f"{AUD}/count_only_harness.py",
           "--only", *crawler_cap,
           "--site-timeout", "1800", "--workers", "12", "--delay", "0.4",
           "--out-csv", f"{AUD}/count_only_uncapped.csv",
           "--out-md", f"{AUD}/count_only_uncapped.md"]
    env = dict(os.environ, PYTHONPATH=ROOT)
    r = subprocess.run(cmd, env=env)
    log(f"재측정 종료 (rc={r.returncode})")
else:
    log("crawler_cap 없음 — 재측정 스킵")

# 4) 최종 병합/보고서
log("finalize_capacity 실행")
subprocess.run([sys.executable, f"{AUD}/finalize_capacity.py"],
               env=dict(os.environ, PYTHONPATH=ROOT))
log("파이프라인 완료")
