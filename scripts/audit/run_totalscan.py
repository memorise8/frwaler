# -*- coding: utf-8 -*-
"""total_candidates.txt 사이트들을 짧은 상한(45s)으로 훑어 server_total만 빠르게 확보.
count는 하한이어도 무방 — 목적은 서버가 알려주는 total 포착."""
import sys, importlib.util
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
ids = [l.strip() for l in open(ROOT / "scripts/audit/total_candidates.txt") if l.strip()]
print(f"[totalscan] {len(ids)}개 사이트 server_total 스캔 (상한 45s)", flush=True)

sys.argv = [
    "count_only_harness.py",
    "--only", *ids,
    "--site-timeout", "45",
    "--workers", "8",
    "--delay", "0.3",
    "--out-csv", str(ROOT / "scripts/audit/count_only_totalscan.csv"),
    "--out-md", str(ROOT / "scripts/audit/count_only_totalscan.md"),
]
spec = importlib.util.spec_from_file_location("coh", str(ROOT / "scripts/audit/count_only_harness.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.main()
