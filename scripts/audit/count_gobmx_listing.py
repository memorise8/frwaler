# -*- coding: utf-8 -*-
"""gob.mx 18개 아카이브 케파 = 리스트페이지 기사링크 합계 (상세 미취득, 경량).

각 사이트: Akamai 챌린지 1회 솔브 → curl_cffi 쿠키로 ?page=N 리스트만 끝까지
walk하며 unique 기사 slug 수 합산. per-site wall cap 900s. count-only(무저장).
출력: scripts/audit/count_only_gobmx.csv
"""
import csv, os, sys, time
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LIBERTREE_MAX_PAGES", "100000")

from crawler.sites import CRAWLERS  # noqa: E402

SITES = [l.strip() for l in open(ROOT / "scripts/audit/health_nocrawler.txt")
         if l.strip().startswith("gob-mx-")]
OUT = ROOT / "scripts/audit/count_only_gobmx.csv"
WALL = 900


def measure(sid):
    cls = CRAWLERS[sid]
    inst = cls(db_conn=None, delay=0)
    seen = set()
    page = 1
    empties = 0
    deadline = time.monotonic() + WALL
    completed = False
    try:
        while time.monotonic() < deadline:
            html = inst._get(inst._archive_url(page), context=f"list {page}")
            if html is None:
                break
            paths = inst._article_paths(html)
            fresh = [p for p in paths if p not in seen]
            if not fresh:
                empties += 1
                if empties >= 2:  # two consecutive empty/dup pages = end
                    completed = True
                    break
            else:
                empties = 0
                seen.update(fresh)
            page += 1
    finally:
        try:
            if inst._cffi:
                inst._cffi.close()
        except Exception:
            pass
    return len(seen), page, completed


def main():
    rows = []
    for i, sid in enumerate(SITES, 1):
        t0 = time.monotonic()
        try:
            n, pages, done = measure(sid)
            err = ""
        except Exception as exc:  # noqa: BLE001
            n, pages, done, err = 0, 0, False, str(exc)[:120]
        dt = round(time.monotonic() - t0, 1)
        print(f"[{i}/{len(SITES)}] {sid}: counted={n} pages={pages} "
              f"completed={done} {dt}s {err}", flush=True)
        rows.append({"site_id": sid, "counted": n, "pages": pages,
                     "completed": done, "elapsed_s": dt, "error": err})
        with open(OUT, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["site_id", "counted", "pages",
                                              "completed", "elapsed_s", "error"])
            w.writeheader()
            w.writerows(rows)
    tot = sum(r["counted"] for r in rows)
    print(f"=== gob.mx 18곳 케파 합계(리스트 기준): {tot} ===", flush=True)


if __name__ == "__main__":
    main()
