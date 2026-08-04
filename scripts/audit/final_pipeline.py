# -*- coding: utf-8 -*-
"""단일 최종 파이프라인 (count-only·무저장):
  1) 1h 재측정(run_capped_1h) 완료 대기
  2) totalscan: total_candidates 93개 server_total 빠른 확보 (45s)
  3) crawler_cap 전체 재식별 (완전 데이터)
  4) 개선판 하네스로 crawler_cap 재측정 (캡무력화+server_total)
  5) finalize → capacity_final_report.xlsx
"""
import csv, glob, os, re, subprocess, sys, time

ROOT = "/data_raid/ruci_workspace/frwaler_job"
AUD = f"{ROOT}/scripts/audit"
LOGDIR = f"{AUD}/out/count_only_logs"
HARNESS = f"{AUD}/count_only_harness.py"
ENV = dict(os.environ, PYTHONPATH=ROOT)

def log(m): print(f"[final {time.strftime('%H:%M:%S')}] {m}", flush=True)

def running(marker):
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me: continue
        try:
            cl = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if marker in cl and "final_pipeline" not in cl:
            return True
    return False

def wait_gone(marker):
    while running(marker):
        time.sleep(30)

def num(x):
    try: return int(float(x))
    except: return None

# 1) 1h 재측정 완료 대기 (+ 잔여 harness)
log("1h 재측정 완료 대기...")
wait_gone("run_capped_1h.py")
wait_gone("count_only_harness.py --only")
log("선행 종료 확인")
time.sleep(3)

# 2) totalscan (server_total 빠른 확보)
cands = [l.strip() for l in open(f"{AUD}/total_candidates.txt") if l.strip()]
log(f"totalscan {len(cands)}개 (45s)")
subprocess.run([sys.executable, HARNESS, "--only", *cands,
                "--site-timeout", "45", "--workers", "8", "--delay", "0.3",
                "--out-csv", f"{AUD}/count_only_totalscan.csv",
                "--out-md", f"{AUD}/count_only_totalscan.md"], env=ENV)

# 3) crawler_cap 전체 재식별
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
log(f"crawler_cap: {len(crawler_cap)}개")
open(f"{AUD}/crawler_cap_sites_full.txt", "w").write("\n".join(crawler_cap))

# 4) 개선판 재측정
if crawler_cap:
    log(f"crawler_cap 재측정 (1800s, 10w)")
    subprocess.run([sys.executable, HARNESS, "--only", *crawler_cap,
                    "--site-timeout", "1800", "--workers", "10", "--delay", "0.4",
                    "--out-csv", f"{AUD}/count_only_uncapped.csv",
                    "--out-md", f"{AUD}/count_only_uncapped.md"], env=ENV)

# 5) finalize
log("finalize")
subprocess.run([sys.executable, f"{AUD}/finalize_capacity.py"], env=ENV)
log("=== 최종 파이프라인 완료 ===")
