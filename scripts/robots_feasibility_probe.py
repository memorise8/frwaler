#!/usr/bin/env python3
"""robots 금지 38곳 — 소량(3건) 추출 가능성 테스트 (2026-07-19).

목적: robots.txt가 자동수집을 금지한 사이트에서 "허가만 받으면 실제로 데이터가
나오는지"를 소량 표본으로 입증. **DB에 절대 쓰지 않는다** — 결과는 리포트 CSV로만.
회사가 각 기관에 수집 허가를 요청할 때 첨부할 근거자료.

각 사이트당 목록→상세 최대 3건에서 title/date/pdf_url을 실제로 파싱해 남긴다.
결과: data/audit/robots_feasibility_20260719.csv (+ .md 요약)
"""
import csv
import re
import sys
import time
import threading
import concurrent.futures as cf
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

sys.path.insert(0, "scripts")
sys.path.insert(0, ".")
from reaudit_deep import (  # noqa: E402
    extract_detail_candidates, probe_pdf, TAG_RX, BLOCK_RX, get, HREF_RX,
)

IN_CSV = "data/audit/robots_blocked_38.csv"
OUT_CSV = "data/audit/robots_feasibility_20260719.csv"

DATE_RX = re.compile(r"\b((?:19|20)\d{2})[-./](\d{1,2})[-./](\d{1,2})\b")
TITLE_TAG_RX = re.compile(r"<(h1|h2|title)[^>]*>(.*?)</\1>", re.I | re.S)


def parse_item(url, html):
    title = ""
    m = TITLE_TAG_RX.search(html)
    if m:
        title = TAG_RX.sub(" ", m.group(2)).strip()[:120]
    dm = DATE_RX.search(TAG_RX.sub(" ", html[:8000]))
    date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
    pm = re.search(r"""href=["']([^"']+\.pdf[^"']*)["']""", html, re.I)
    pdf = urljoin(url, pm.group(1)) if pm else ""
    return title, date, pdf


def probe_site(row):
    host = row["host"]
    listing = row["detail_url"] or row["url"]
    out = {"host": host, "listing_url": listing, "items_extracted": 0,
           "sample1": "", "sample2": "", "sample3": "", "pdf_check": "", "판정": ""}
    r, err = get(listing)
    if r is None or r.status_code != 200 or BLOCK_RX.search((r.text or "")[:20000]):
        out["판정"] = f"목록접근실패({err or (r.status_code if r else '?')})"
        return log(out)
    cands = extract_detail_candidates(listing, r.text or "")
    if not cands:
        out["판정"] = "상세링크추출불가(JS의심)"
        return log(out)
    samples = []
    pdf_checked = False
    for cand in cands[:6]:
        if len(samples) >= 3:
            break
        if cand.lower().endswith(".pdf"):
            if not pdf_checked:
                out["pdf_check"] = probe_pdf(cand, listing)
                pdf_checked = True
            samples.append((cand.rsplit("/", 1)[-1], "", cand))
            continue
        d, e = get(cand)
        if d is None or d.status_code != 200:
            continue
        if BLOCK_RX.search((d.text or "")[:20000]):
            continue
        title, date, pdf = parse_item(cand, d.text or "")
        if not title:
            continue
        if pdf and not pdf_checked:
            out["pdf_check"] = probe_pdf(pdf, cand)
            pdf_checked = True
        samples.append((title, date, pdf))
        time.sleep(0.5)
    out["items_extracted"] = len(samples)
    for i, (t, dt, pdf) in enumerate(samples, 1):
        out[f"sample{i}"] = f"{t} | {dt} | {'PDF:'+pdf.rsplit('/',1)[-1] if pdf else 'no-pdf'}"[:160]
    if len(samples) >= 1:
        out["판정"] = f"추출가능({len(samples)}건" + (
            f", PDF={out['pdf_check']}" if out["pdf_check"] else "") + ")"
    else:
        out["판정"] = "본문파싱실패"
    return log(out)


def log(out):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"{ts} [{out['host']}] {out['판정']}", flush=True)
    return out


def main():
    rows = list(csv.DictReader(open(IN_CSV, encoding="utf-8")))
    print(f"robots-blocked feasibility test: {len(rows)} sites (NO DB writes)", flush=True)
    results = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(probe_site, rows):
            results.append(res)
    results.sort(key=lambda r: (-r["items_extracted"], r["host"]))
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    from collections import Counter
    ok = sum(1 for r in results if r["items_extracted"] >= 1)
    pdf_ok = sum(1 for r in results if "pdf_ok" in (r["pdf_check"] or ""))
    print(f"\n== 결과: {ok}/{len(results)}곳 추출가능, 그중 PDF다운로드 확인 {pdf_ok}곳 ==", flush=True)
    for k, v in Counter(r["판정"].split("(")[0] for r in results).most_common():
        print(f"  {k}: {v}", flush=True)
    print(f"saved: {OUT_CSV}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
