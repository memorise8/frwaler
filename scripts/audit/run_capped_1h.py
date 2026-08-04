# -*- coding: utf-8 -*-
"""capped_sites.txt의 사이트만 1시간 상한으로 재측정 (셸 단어분리 우회 런처)."""
import sys, importlib.util
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
ids = [l.strip() for l in open(ROOT / "scripts/audit/capped_sites.txt") if l.strip()]
print(f"[launcher] {len(ids)}개 사이트 --only 주입", flush=True)

sys.argv = [
    "count_only_harness.py",
    "--only", *ids,
    "--site-timeout", "3600",
    "--workers", "12",
    "--delay", "0.4",
    "--out-csv", str(ROOT / "scripts/audit/count_only_capped_1h.csv"),
    "--out-md", str(ROOT / "scripts/audit/count_only_capped_1h.md"),
]
spec = importlib.util.spec_from_file_location("coh", str(ROOT / "scripts/audit/count_only_harness.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.main()
