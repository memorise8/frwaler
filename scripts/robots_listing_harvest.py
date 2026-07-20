#!/usr/bin/env python3
"""robots 허가 38곳 — listup+URL 인벤토리 수집 (2026-07-19).

허가받은 사이트에서 "제목 + 상세 URL (+ 날짜)"만 목록 페이지를 넘기며 수집.
상세 페이지 진입/PDF 다운로드/텍스트 추출은 하지 않는다 (가벼운 1차 인벤토리).
**운영 DB에 쓰지 않는다** — 결과는 사이트별 CSV로만.

정적 fetch로 후보가 0이면 playwright 단일 렌더로 1페이지만 재시도(JS 사이트).
페이지네이션은 ?page=/&spc.page= 등 흔한 파라미터를 증가시키며 최대 _MAX_PAGES까지.

결과:
  data/audit/robots_listing/<host>.csv  (title,url,date)
  data/audit/robots_listing_summary_20260719.csv  (host,items,pages,mode,note)
"""
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

sys.path.insert(0, "scripts")
sys.path.insert(0, ".")
from reaudit_deep import extract_detail_candidates, TAG_RX, BLOCK_RX, get, HREF_RX  # noqa: E402

ALLOW_JSON = "data/audit/robots_allow_hosts.json"
REQ_CSV = "data/audit/robots_허가요청_38.csv"
OUT_DIR = "data/audit/robots_listing"
SUMMARY = "data/audit/robots_listing_summary_20260719.csv"

_MAX_PAGES = 40          # 사이트당 목록 페이지 상한 (silent truncation 방지 — 도달 시 로그)
_MAX_ITEMS = 2000        # 사이트당 수집 상한
_PAGE_PARAMS = ["page", "spc.page", "p", "pageIndex", "pageNo", "start", "offset"]
DATE_RX = re.compile(r"\b((?:19|20)\d{2})[-./](\d{1,2})[-./](\d{1,2})\b")
TITLE_RX = re.compile(r"<(title|h1)[^>]*>(.*?)</\1>", re.I | re.S)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"{ts} {msg}", flush=True)


def _clean(s):
    return TAG_RX.sub(" ", s or "").strip()


def _items_from_html(base_url, html):
    """(title, abs_url, date) 리스트. 앵커 텍스트를 제목으로, 근처 텍스트에서 날짜 추출."""
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    out, seen = [], set()
    for href, text in HREF_RX.findall(html):
        href = href.strip()
        absu = urljoin(base_url, href)
        pu = urlparse(absu)
        if pu.scheme not in ("http", "https"):
            continue
        if pu.netloc.lower().removeprefix("www.") != base_host:
            continue
        if absu.rstrip("/") == base_url.rstrip("/") or absu in seen:
            continue
        title = _clean(text)
        # 목록 항목다운 링크만: 제목이 어느 정도 길고 상세형 URL
        if len(title) < 12:
            continue
        if not (re.search(r"\d{3,}", absu) or re.search(
                r"view|detail|article|publication|report|item|record|handle|"
                r"notice|bbs|nttId|artId|pub|doc|research|id=", absu, re.I)):
            continue
        seen.add(absu)
        dm = DATE_RX.search(title) or DATE_RX.search(_clean(text))
        date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
        out.append((title[:200], absu, date))
    return out


def _with_page(url, param, value):
    pu = urlparse(url)
    q = parse_qs(pu.query)
    q[param] = [str(value)]
    return urlunparse(pu._replace(query=urlencode({k: v[0] for k, v in q.items()})))


def _detect_page_param(url, first_html):
    """목록 URL 또는 페이지네이션 링크에서 페이지 파라미터를 추정."""
    q = parse_qs(urlparse(url).query)
    for p in _PAGE_PARAMS:
        if p in q:
            return p
    # HTML에서 ?page=2 류 링크 탐색
    for href, _ in HREF_RX.findall(first_html):
        hq = parse_qs(urlparse(href).query)
        for p in _PAGE_PARAMS:
            if p in hq:
                return p
    return None


def harvest_static(host, listing_url):
    r, err = get(listing_url)
    if r is None or r.status_code != 200 or BLOCK_RX.search((r.text or "")[:20000]):
        return None, 0, f"목록접근실패({err or (r.status_code if r else '?')})"
    html = r.text or ""
    first = _items_from_html(listing_url, html)
    if not first:
        return None, 1, "정적후보없음"
    param = _detect_page_param(listing_url, html)
    items, seen_urls = list(first), {u for _, u, _ in first}
    pages = 1
    if param:
        # 페이지 파라미터 시작값 추정 (0-base vs 1-base)
        start = parse_qs(urlparse(listing_url).query).get(param, ["1"])[0]
        try:
            base_n = int(start)
        except ValueError:
            base_n = 1
        step = 10 if param in ("start", "offset") and base_n % 10 == 0 else 1
        for i in range(1, _MAX_PAGES):
            if len(items) >= _MAX_ITEMS:
                log(f"[{host}] item cap {_MAX_ITEMS} 도달 — 이후 페이지 미수집")
                break
            nxt = _with_page(listing_url, param, base_n + i * step)
            rr, _ = get(nxt)
            if rr is None or rr.status_code != 200:
                break
            page_items = _items_from_html(nxt, rr.text or "")
            fresh = [it for it in page_items if it[1] not in seen_urls]
            if not fresh:
                break
            for it in fresh:
                seen_urls.add(it[1]); items.append(it)
            pages += 1
            time.sleep(0.4)
        if pages >= _MAX_PAGES:
            log(f"[{host}] page cap {_MAX_PAGES} 도달 — 더 있을 수 있음")
    note = f"static(param={param or 'none'})"
    return items[:_MAX_ITEMS], pages, note


def harvest_js(host, listing_url):
    try:
        from crawler.playwright_fetcher import fetch_html
    except Exception as e:
        return None, 0, f"playwright불가({e})"
    html = fetch_html(listing_url, timeout_seconds=45)
    if not html:
        return None, 0, "js렌더실패"
    items = _items_from_html(listing_url, html)
    if not items:
        return None, 1, "js후보없음(수동확인)"
    return items, 1, "js(1페이지만)"


def process(row):
    host = row["host"]
    listing_url = (row.get("수집대상_URL") or row.get("url") or "").strip().strip('"')
    items, pages, note = harvest_static(host, listing_url)
    mode = "static"
    if not items:
        items, pages, note = harvest_js(host, listing_url)
        mode = "js"
    n = len(items) if items else 0
    if items:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(f"{OUT_DIR}/{host}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["title", "url", "date"]); w.writerows(items)
    log(f"[{host}] items={n} pages={pages} {note}")
    return {"host": host, "items": n, "pages": pages, "mode": mode, "note": note,
            "listing_url": listing_url}


def main():
    allow = set(json.load(open(ALLOW_JSON)))
    raw = open(REQ_CSV, "rb").read()
    txt = raw.decode("utf-8-sig")
    rows = [r for r in csv.DictReader(txt.splitlines()) if (r.get("host") or "").strip() in allow]
    log(f"listing harvest: {len(rows)} permitted sites (NO DB writes)")
    results = []
    for row in rows:                      # 순차 — 사이트 부하 배려
        try:
            results.append(process(row))
        except Exception as e:
            results.append({"host": row["host"], "items": 0, "pages": 0,
                            "mode": "error", "note": f"{type(e).__name__}: {e}",
                            "listing_url": row.get("수집대상_URL", "")})
            log(f"[{row['host']}] ERROR {e}")
    results.sort(key=lambda r: -r["items"])
    with open(SUMMARY, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["host", "items", "pages", "mode", "note", "listing_url"])
        w.writeheader(); w.writerows(results)
    total = sum(r["items"] for r in results)
    ok = sum(1 for r in results if r["items"] > 0)
    log(f"DONE — {ok}/{len(results)}곳 수집, 총 {total}건 URL 인벤토리")
    log(f"saved: {SUMMARY} + {OUT_DIR}/*.csv")


if __name__ == "__main__":
    sys.exit(main())
