# -*- coding: utf-8 -*-
"""능동 최신화 확인 (2번): 사이트 1페이지를 열어 최신 항목을 우리 DB와 대조.

count-only 하네스의 캡무력화 + PDF 차단 가드를 재활용하되, `_save_paper` 를
'카운트'가 아니라 '항목 식별자(external_id/url) 캡처'로 바꿔 앞쪽 N개만 잡고 멈춘다.
그 N개 중 우리 DB에 없는 항목 수 = "새 글 있음" 신호. 저장/다운로드 일절 없음(RO).

    PYTHONPATH=. .venv/bin/python scripts/audit/freshness_probe.py --only site-a site-b ...
    (--sites-file 로 목록 파일, --cap N 캡처수, --timeout 초)
"""
import argparse
import csv
import signal
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
sys.path.insert(0, str(ROOT))

import scripts.audit.count_only_harness as coh  # noqa: E402  (재사용)
from crawler.sites import CRAWLERS  # noqa: E402
from crawler import base_crawler as _base  # noqa: E402

DB = str(ROOT / "libertree-app/data/libertree.db")
CAP = 12  # 앞쪽에서 이만큼 잡으면 정지 (대략 1~2페이지)


class _Stop(BaseException):
    """크롤러의 broad except에 안 삼켜지도록 BaseException."""


_CAPTURED = []  # 현재 사이트가 뱉은 항목 [(external_id, url), ...]


def _capture_from_paper(paper_dict, *_a, **_k):
    eid = str(paper_dict.get("external_id") or "").strip()
    url = str(paper_dict.get("url") or "").strip()
    _CAPTURED.append((eid, url))
    if len(_CAPTURED) >= CAP:
        raise _Stop()
    return -1


def _capture_from_v2(doc, *_a, **_k):
    eid = str(doc.get("post_number") or "").strip()
    url = str(doc.get("meta_url") or doc.get("pdf_url") or "").strip()
    _CAPTURED.append((eid, url))
    if len(_CAPTURED) >= CAP:
        raise _Stop()
    return -1


def _install_capture():
    _base.BaseCrawler._save_paper = _capture_from_paper
    _base.BaseCrawler._save_paper_v2 = _capture_from_v2
    _base.BaseCrawler._save_paper_legacy = _capture_from_paper
    if hasattr(_base.BaseCrawler, "_save_document"):
        _base.BaseCrawler._save_document = _capture_from_paper


def _existing(site_id):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ids, urls = set(), set()
    for (pn,) in conn.execute(
        "select post_number from documents where site_id=? and post_number is not null", (site_id,)):
        ids.add(str(pn).strip())
    for (mu,) in conn.execute(
        "select meta_url from documents where site_id=? and meta_url is not null", (site_id,)):
        urls.add(str(mu).strip())
    conn.close()
    return ids, urls


class _Timeout(BaseException):
    pass


def _probe_one(site_id, timeout):
    global _CAPTURED
    _CAPTURED = []
    if site_id not in CRAWLERS:
        return {"site_id": site_id, "captured": 0, "new": 0,
                "verdict": "no_crawler", "sample": ""}
    ids, urls = _existing(site_id)
    cls = CRAWLERS[site_id]
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    inst = None
    err = ""
    t0 = time.monotonic()

    def _alarm(*_):
        raise _Timeout()

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(timeout))
    try:
        inst = cls(db_conn=conn, delay=0)
        coh.neutralize_caps(cls)
        try:
            inst.crawl(limit=CAP)
        except (_Stop, _Timeout):
            raise
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:80]
    except _Stop:
        pass
    except _Timeout:
        err = f"timeout({timeout}s)"
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:80]
    finally:
        signal.alarm(0)
        try:
            conn.close()
        except Exception:
            pass
        try:
            if inst is not None and getattr(inst, "_session", None):
                inst._session.close()
        except Exception:
            pass

    captured = list(_CAPTURED)
    new = [c for c in captured if c[0] and c[0] not in ids and (not c[1] or c[1] not in urls)]
    if not captured:
        verdict = "no_items" + (f":{err}" if err else "")
    elif new:
        verdict = "NEW"
    else:
        verdict = "up_to_date"
    return {"site_id": site_id, "captured": len(captured), "new": len(new),
            "verdict": verdict, "elapsed_s": round(time.monotonic() - t0, 1),
            "sample": " | ".join(c[0][:40] for c in new[:3]), "error": err}


def main():
    global CAP
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--sites-file")
    ap.add_argument("--cap", type=int, default=CAP)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--out-csv", default=str(ROOT / "scripts/audit/freshness_probe.csv"))
    args = ap.parse_args()
    CAP = args.cap
    sites = list(args.only)
    if args.sites_file:
        sites += [l.strip() for l in open(args.sites_file) if l.strip()]

    _install_capture()
    rows = []
    for i, sid in enumerate(sites, 1):
        r = _probe_one(sid, args.timeout)
        rows.append(r)
        print(f"[{i}/{len(sites)}] {sid:38s} captured={r['captured']:>2} "
              f"new={r['new']:>2} -> {r['verdict']}  {r.get('sample','')}", flush=True)

    with open(args.out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["site_id", "captured", "new", "verdict",
                                          "elapsed_s", "sample", "error"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})

    nnew = sum(1 for r in rows if r["verdict"] == "NEW")
    nutd = sum(1 for r in rows if r["verdict"] == "up_to_date")
    nno = sum(1 for r in rows if r["verdict"].startswith("no_items"))
    print(f"\n=== 요약: NEW {nnew} / 최신 {nutd} / 확인불가 {nno} (총 {len(rows)}) ===")


if __name__ == "__main__":
    main()
