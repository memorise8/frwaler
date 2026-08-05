# -*- coding: utf-8 -*-
"""크롤러 전체 건강검진 (2026-08-05, count-only·무저장).

전략: 어제 C+E sweep에서 counted>=3 인 470개는 "지금 작동" 실증 → 재검사 불필요.
나머지 308개(health_targets.txt)만 실전 probe:
  - harness --probe 3 (캡 원상태, 항목 3개 저장 시도까지만) --site-timeout 300
  - counted>=1 → OK
  - counted==0 → PDF의존(health_pdfdep.txt)이면 'pdf_artifact'(측정한계, 로그로 2차판정),
                 아니면 로그의 HTTP/차단 신호로 broken/blocked/unknown 분류
출력: crawler_health_probe308.csv → 종합 crawler_health_full.csv + CRAWLER_HEALTH.md
"""
import csv, glob, os, re, sys, importlib.util, time
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
AUD = ROOT / "scripts/audit"
LOGDIR = AUD / "out/count_only_logs"

def log(m):
    print(f"[health {time.strftime('%H:%M:%S')}] {m}", flush=True)

targets = [l.strip() for l in open(AUD / "health_targets.txt") if l.strip()]
proven = [l.strip() for l in open(AUD / "health_proven.txt") if l.strip()]
nocrawler = [l.strip() for l in open(AUD / "health_nocrawler.txt") if l.strip()]
pdfdep = set(l.strip() for l in open(AUD / "health_pdfdep.txt") if l.strip())

# ---- 1) 308개 probe (별도 프로세스 아님 — harness main() 직접) ----
log(f"probe {len(targets)}개 (limit 3, timeout 300s, workers 12)")
sys.argv = [
    "count_only_harness.py",
    "--only", *targets,
    "--probe", "3",
    "--site-timeout", "300",
    "--workers", "12",
    "--delay", "0.3",
    "--out-csv", str(AUD / "crawler_health_probe308.csv"),
    "--out-md", str(AUD / "crawler_health_probe308.md"),
]
spec = importlib.util.spec_from_file_location("coh", str(AUD / "count_only_harness.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.main()
log("probe 완료 — 종합 분류 시작")

# ---- 2) 종합 분류 ----
def num(x):
    try:
        return int(float(x))
    except Exception:
        return 0

BLOCK_RE = re.compile(r"403|429|challenge|cloudflare|captcha|denied|blocked", re.I)
NET_RE = re.compile(r"timeout|timed out|connection|refused|SSL|certificate|Could not fetch|empty response", re.I)

def classify_zero(sid):
    logs = glob.glob(str(LOGDIR / f"{sid}.log"))
    txt = open(logs[0], errors="replace").read() if logs else ""
    if sid in pdfdep:
        return "pdf_artifact", "PDF의존 크롤러 — count-only 가드 탓 0일 수 있음(측정한계). 실수집으로만 확진 가능"
    if BLOCK_RE.search(txt):
        return "blocked", "차단 신호(403/429/CF challenge 등) — 사이트가 봇 차단 중"
    if NET_RE.search(txt):
        return "net_fail", "네트워크/접속 실패(타임아웃·SSL·연결거부)"
    return "broken", "요청은 되나 0건 — 셀렉터/파싱 고장 의심"

rows = []
for sid in proven:
    rows.append({"site_id": sid, "health": "ok", "evidence": "sweep_2026-08-04 counted>=3",
                 "detail": "전수측정에서 실수집 확인"})
probe_res = {}
for r in csv.DictReader(open(AUD / "crawler_health_probe308.csv", encoding="utf-8", newline="")):
    probe_res[r["site_id"]] = r
for sid in targets:
    r = probe_res.get(sid)
    if r is None:
        rows.append({"site_id": sid, "health": "unknown", "evidence": "probe_missing", "detail": "probe 결과 없음"})
        continue
    cnt = num(r.get("counted"))
    completed = str(r.get("completed", "")).lower() == "true"
    if cnt >= 1:
        rows.append({"site_id": sid, "health": "ok", "evidence": f"probe counted={cnt}",
                     "detail": "지금 fetch+파싱 정상"})
    elif not completed:
        rows.append({"site_id": sid, "health": "slow_unknown", "evidence": "probe timeout(300s)",
                     "detail": "5분 내 1건도 못 저장 — 매우 느리거나 고장"})
    else:
        h, why = classify_zero(sid)
        rows.append({"site_id": sid, "health": h, "evidence": "probe counted=0", "detail": why})
for sid in nocrawler:
    rows.append({"site_id": sid, "health": "no_crawler", "evidence": "-", "detail": "크롤러 미제작(D, datos-gob-mx 계열 등)"})

out = AUD / "crawler_health_full.csv"
with open(out, "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["site_id", "health", "evidence", "detail"])
    w.writeheader()
    for r in sorted(rows, key=lambda x: (x["health"], x["site_id"])):
        w.writerow(r)

from collections import Counter
dist = Counter(r["health"] for r in rows)
with open(AUD / "CRAWLER_HEALTH.md", "w", encoding="utf-8") as f:
    f.write(f"# 크롤러 전체 건강검진 ({time.strftime('%Y-%m-%d %H:%M')})\n\n")
    f.write(f"- 대상: 사이트 {len(rows)}개 (크롤러 보유 {len(proven)+len(targets)}, 미보유 {len(nocrawler)})\n")
    f.write(f"- 방식: sweep 실증({len(proven)}) + 실전 probe({len(targets)}, limit3/300s) — count-only 무저장\n\n")
    f.write("| health | 수 | 의미 |\n|---|---:|---|\n")
    MEAN = {"ok": "정상 작동", "broken": "고장 의심(셀렉터/파싱)", "blocked": "사이트가 차단 중",
            "net_fail": "접속 실패", "pdf_artifact": "PDF의존 — 측정한계로 판정보류",
            "slow_unknown": "5분내 무저장(느림/고장)", "no_crawler": "크롤러 미제작", "unknown": "결과 없음"}
    for h, cnt in dist.most_common():
        f.write(f"| {h} | {cnt} | {MEAN.get(h, '')} |\n")
    f.write("\n## 수리 우선순위 (전량 다운로드 전)\n")
    for h in ("broken", "blocked", "net_fail", "slow_unknown"):
        lst = sorted(r["site_id"] for r in rows if r["health"] == h)
        if lst:
            f.write(f"\n### {h} ({len(lst)})\n")
            for s in lst:
                f.write(f"- {s}\n")
log(f"완료: {dict(dist)}")
log(f"-> {out} / CRAWLER_HEALTH.md")
