# -*- coding: utf-8 -*-
"""납품 건강 패스: not-ok 60곳을 실제 미니크롤(임시 DB·임시 blob, 3건)로 최종 판정.

클라이언트와 동일한 실제 수집 경로(fetch→파싱→저장)를 임시 위치에 돌려본다.
count-only가 아니라 진짜 크롤이라 pdf_artifact(가드 오탐)도 정확히 갈린다.
운영 DB/blob은 절대 안 건드림(temp만 사용).

판정:
  HEALTHY       3건 시도 중 1건이라도 정상 저장  -> 정상(클라이언트에서 작동)
  NEEDS_KEY     0건 + 인증/키 신호               -> API키 필요
  BLOCKED_HERE  0건 + 403/429/challenge/봇 신호   -> 우리 IP 차단(클라이언트 egress선 될 수도)
  DEAD_OR_NET   0건 + DNS/연결/SSL/영구 실패       -> 사이트 폐쇄/네트워크
  SLOW          0건 + 시간초과                    -> 매우 느림(고장 미확정)
  BROKEN        0건 + 코드/파싱 에러 신호          -> 진짜 코드 수리 대상
  INCONCLUSIVE  0건 + 뚜렷한 신호 없음             -> 수동 확인
"""
import csv
import io
import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import contextlib
from pathlib import Path

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LIBERTREE_MAX_PAGES", "5")     # 미니크롤: 앞쪽만
os.environ.setdefault("LIBERTREE_MAX_WALL_S", "170")

# API 키가 필요한 사이트(교육부NZ/INRAE/transparency-au 등)가 실제로 작동하도록
# crawler/.env 를 로드. 없으면 그 사이트들이 키 없이 403 -> 오탐(BLOCKED)이 됨.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / "crawler/.env")
except Exception:
    pass

from crawler.sites import CRAWLERS  # noqa: E402
from crawler import db_libertree as ldb  # noqa: E402

LIMIT = 3
TIMEOUT = int(os.environ.get("DH_TIMEOUT", "180"))

BLOCK = re.compile(r"\b403\b|\b429\b|forbidden|challenge|cloudflare|captcha|denied|akamai|\bbot\b|access denied|verify you are human", re.I)
NET = re.compile(r"timeout|timed out|connection|refused|SSL|certificate|name resolution|getaddrinfo|max retries|could not fetch|failed to fetch|empty response|no response|no route|unreachable|\b404\b|\b410\b|\b500\b|\b503\b", re.I)
KEY = re.compile(r"\b401\b|unauthorized|api[_ -]?key|apikey|missing key|invalid key", re.I)
CODE = re.compile(r"AttributeError|KeyError|IndexError|TypeError|ValueError|NoneType|has no attribute|list index|JSONDecode|Traceback|'NoneType'", re.I)
# 페이지는 받았으나 파싱/셀렉터 실패 = 진짜 코드 수리 신호
PARSE = re.compile(r"could not extract|could not find|couldn't find|unable to (?:find|extract|parse)|aborting|view_dom_id|no (?:results|items|records|publications|papers|articles|data) found|selector|parse error", re.I)


class _TO(BaseException):
    pass


def _probe(site_id, blob_root, dbpath):
    if site_id not in CRAWLERS:
        return {"verdict": "NO_CRAWLER", "saved": 0, "signal": ""}
    cls = CRAWLERS[site_id]
    # 사이트마다 새 temp DB
    if os.path.exists(dbpath):
        os.remove(dbpath)
    conn = ldb.open_db(dbpath)
    ldb.init_db(conn)
    ldb.upsert_site(conn, getattr(cls, "site_id", site_id),
                    getattr(cls, "site_name", site_id) or site_id,
                    getattr(cls, "base_url", "") or "https://x")
    buf = io.StringIO()
    err = ""
    timed_out = False
    t0 = time.monotonic()

    def _alarm(*_):
        raise _TO()

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(TIMEOUT)
    try:
        inst = cls(db_conn=conn, delay=0)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                inst.crawl(limit=LIMIT)
            except _TO:
                raise
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
    except _TO:
        timed_out = True
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    finally:
        signal.alarm(0)

    saved = 0
    try:
        saved = conn.execute("select count(*) from documents where site_id=?", (site_id,)).fetchone()[0]
    except Exception:
        pass
    try:
        conn.close()
        if getattr(inst, "_session", None):
            inst._session.close()
    except Exception:
        pass

    raw = buf.getvalue()
    out = raw + " " + err
    elapsed = round(time.monotonic() - t0, 1)
    if saved >= 1:
        verdict = "HEALTHY"
    elif timed_out:
        verdict = "SLOW"
    elif KEY.search(out) and not BLOCK.search(out):
        verdict = "NEEDS_KEY"
    elif BLOCK.search(out):
        verdict = "BLOCKED_HERE"
    elif CODE.search(out) or PARSE.search(out):
        verdict = "BROKEN"
    elif NET.search(out):
        verdict = "DEAD_OR_NET"
    elif not raw.strip() and elapsed > 45:
        verdict = "SLOW"          # 조용히 오래 걸림(멈춤/느림)
    else:
        verdict = "INCONCLUSIVE"

    sig = ""
    for pat, lbl in ((KEY, "key"), (BLOCK, "block"), (PARSE, "parse"), (CODE, "code"), (NET, "net")):
        m = pat.search(out)
        if m:
            sig = f"{lbl}:{m.group(0)[:40]}"
            break
    tail = re.sub(r"\s+", " ", raw)[-260:]
    return {"verdict": verdict, "saved": saved, "elapsed_s": elapsed,
            "signal": sig, "err": err[:120], "out_tail": tail}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=str(ROOT / "scripts/audit/health_verify_targets.txt"))
    ap.add_argument("--out", default=str(ROOT / "scripts/audit/delivery_health.csv"))
    ap.add_argument("--sample", type=int, default=0)
    args = ap.parse_args()

    targets = [l.strip() for l in open(args.targets) if l.strip()]
    bpath = ROOT / "scripts/audit/health_verify_buckets.csv"
    buckets = ({r["site_id"]: r["prev_bucket"] for r in csv.DictReader(open(bpath))}
               if bpath.exists() else {})
    if args.sample:
        n = args.sample
        seen, picked = set(), []
        for sid in targets:
            b = buckets.get(sid)
            if b not in seen:
                seen.add(b); picked.append(sid)
        picked += [s for s in targets if s not in picked][: max(0, n - len(picked))]
        targets = picked[:n]

    tmp = Path(tempfile.mkdtemp(prefix="delivhealth_", dir="/tmp/claude-1001"))
    blob = tmp / "blob"; blob.mkdir()
    dbpath = str(tmp / "t.db")
    out_csv = Path(args.out)
    os.environ["LIBERTREE_BLOB_ROOT"] = str(blob)  # 혹시 crawler가 참조하면 temp로

    rows = []
    for i, sid in enumerate(targets, 1):
        r = _probe(sid, str(blob), dbpath)
        r["site_id"] = sid
        r["prev_bucket"] = buckets.get(sid, "")
        rows.append(r)
        print(f"[{i}/{len(targets)}] {sid:40s} {r['prev_bucket']:16s} "
              f"-> {r['verdict']:13s} saved={r['saved']} {r.get('signal','')} "
              f"({r.get('elapsed_s','?')}s)", flush=True)
        # 매 스텝 저장(중단 대비)
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["site_id", "prev_bucket", "verdict",
                                              "saved", "elapsed_s", "signal", "err", "out_tail"])
            w.writeheader()
            for x in rows:
                w.writerow({k: x.get(k, "") for k in w.fieldnames})

    shutil.rmtree(tmp, ignore_errors=True)
    import collections
    dist = collections.Counter(r["verdict"] for r in rows)
    print("\n=== 최종 판정 분포 ===")
    for v, n in dist.most_common():
        print(f"  {v:14s} {n}")
    print(f"\n총 {len(rows)}곳 -> {out_csv}")


if __name__ == "__main__":
    main()
