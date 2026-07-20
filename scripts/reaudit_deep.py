#!/usr/bin/env python3
"""미수집 152곳 재실사 — 2차 심층 프로브 (2026-07-18).

1차에서 verdict=ok인 사이트 대상. 로그인 실사 2차 정밀과 같은 방법론:
  ① 목록 URL 재접속 → 상세(본문) 후보 링크 추출
  ② 상세 페이지 1건 실제 접속 → 본문 차단 여부 판정
  ③ 상세에서 PDF 링크 발견 시 실제 다운로드 시도(첫 2KB) → %PDF 매직 확인
  ④ robots.txt를 목록 경로에 대해 정식 파싱 (urllib.robotparser)
결과: data/audit/reaudit_tier2_20260718.csv
"""
import csv
import re
import sys
import threading
import concurrent.futures as cf
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import urllib3

urllib3.disable_warnings()

IN_CSV = "data/audit/reaudit_tier1_20260718.csv"
OUT_CSV = "data/audit/reaudit_tier2_20260718.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
}
TIMEOUT = 25

BLOCK_RX = re.compile(
    r"cf-browser-verification|challenge-platform|Just a moment|_cf_chl|"
    r"captcha|hcaptcha|recaptcha|access denied|not authorized", re.I)

HREF_RX = re.compile(r"""<a[^>]+href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
TAG_RX = re.compile(r"<[^>]+>")

DETAIL_KEYWORDS = re.compile(
    r"view|detail|article|publication|report|paper|item|record|handle|notice|"
    r"press|news|bbs|board|pubdetail|document|research|resource|post|story|"
    r"release|bulletin|working|study|nttId|artId|seq|idx|wr_id|docId", re.I)
SKIP_RX = re.compile(
    r"\.(css|js|png|jpe?g|gif|svg|ico|xml|zip|mp4|webp)([?#]|$)|"
    r"mailto:|javascript:|tel:|/login|/signin|/search$|/tag/|/category$|"
    r"facebook|twitter|linkedin|youtube|instagram", re.I)

_tls = threading.local()


def sess():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _tls.s = s
    return _tls.s


def get(url, **kw):
    try:
        return sess().get(url, timeout=TIMEOUT, allow_redirects=True, verify=False, **kw), ""
    except Exception as e:
        return None, type(e).__name__


def robots_allows(listing_url):
    p = urlparse(listing_url)
    rp = RobotFileParser()
    try:
        r, err = get(f"{p.scheme}://{p.netloc}/robots.txt")
        if r is None or r.status_code != 200:
            return "no_robots"
        rp.parse(r.text.splitlines())
        return "allowed" if rp.can_fetch("*", listing_url) else "disallowed"
    except Exception:
        return "robots_error"


def extract_detail_candidates(base_url, html):
    """목록 HTML에서 상세 페이지 후보 링크를 점수순으로 반환."""
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    seen, scored = set(), []
    for href, text in HREF_RX.findall(html):
        href = href.strip()
        if not href or SKIP_RX.search(href):
            continue
        absu = urljoin(base_url, href)
        pu = urlparse(absu)
        if pu.scheme not in ("http", "https"):
            continue
        if pu.netloc.lower().removeprefix("www.") != base_host:
            continue
        if absu.rstrip("/") == base_url.rstrip("/") or absu in seen:
            continue
        seen.add(absu)
        label = TAG_RX.sub(" ", text).strip()
        score = 0
        if DETAIL_KEYWORDS.search(absu):
            score += 3
        if re.search(r"\d{3,}", absu):
            score += 2
        if len(label) > 25:
            score += 2
        elif len(label) > 12:
            score += 1
        if absu.lower().endswith(".pdf"):
            score += 1
        depth = pu.path.count("/") + (1 if pu.query else 0)
        score += min(depth, 3) * 0.5
        if score > 0:
            scored.append((score, absu))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:5]]


def probe_pdf(pdf_url, referer):
    r, err = get(pdf_url, headers={"Referer": referer}, stream=True)
    if r is None:
        return f"pdf_error:{err}"
    if r.status_code != 200:
        return f"pdf_http_{r.status_code}"
    try:
        chunk = next(r.iter_content(2048), b"")
    finally:
        r.close()
    if chunk[:5] == b"%PDF-":
        return "pdf_ok"
    ct = r.headers.get("Content-Type", "")
    if "pdf" in ct.lower():
        return "pdf_ok_ct"
    return "pdf_fake"


def probe(row):
    host, listing_url = row["host"], row["final_url"] or row["url"]
    out = {"host": host, "기존분류": row["기존분류"], "listing_url": listing_url,
           "robots_listing": "", "n_candidates": 0, "detail_url": "",
           "detail_verdict": "", "pdf_url": "", "pdf_verdict": "", "판정": ""}

    out["robots_listing"] = robots_allows(listing_url)

    r, err = get(listing_url)
    if r is None or r.status_code != 200:
        out["판정"] = f"목록재접속실패({err or r.status_code})"
        return done(out)
    html = r.text or ""
    if BLOCK_RX.search(html[:20000]):
        out["판정"] = "목록차단마커"
        return done(out)

    cands = extract_detail_candidates(listing_url, html)
    out["n_candidates"] = len(cands)
    if not cands:
        # 링크가 안 뽑히면 JS 렌더 의심 (본문이 스크립트로만 구성)
        text_len = len(TAG_RX.sub(" ", html))
        out["판정"] = "JS렌더필요" if text_len < 3000 or html.count("<script") > 10 else "상세후보없음"
        return done(out)

    detail_html, detail_url = "", ""
    for cand in cands[:3]:
        if cand.lower().endswith(".pdf"):
            out["pdf_url"] = cand
            out["pdf_verdict"] = probe_pdf(cand, listing_url)
            out["detail_verdict"] = "목록직결PDF"
            detail_url = cand
            break
        r2, err2 = get(cand)
        if r2 is None:
            out["detail_verdict"] = f"error:{err2}"
            continue
        if r2.status_code != 200:
            out["detail_verdict"] = f"http_{r2.status_code}"
            continue
        if BLOCK_RX.search((r2.text or "")[:20000]):
            out["detail_verdict"] = "본문차단마커"
            continue
        body_len = len(TAG_RX.sub(" ", r2.text or ""))
        if body_len < 500:
            out["detail_verdict"] = "본문빈약(JS의심)"
            continue
        detail_html, detail_url = r2.text, cand
        out["detail_verdict"] = "ok"
        break
    out["detail_url"] = detail_url

    if detail_html and not out["pdf_url"]:
        m = re.search(r"""href=["']([^"']+\.pdf[^"']*)["']""", detail_html, re.I) or \
            re.search(r"""href=["']([^"']*(?:download|file|attach)[^"']*)["']""", detail_html, re.I)
        if m:
            out["pdf_url"] = urljoin(detail_url, m.group(1))
            out["pdf_verdict"] = probe_pdf(out["pdf_url"], detail_url)

    # 종합 판정
    if out["detail_verdict"] in ("ok", "목록직결PDF"):
        if out["pdf_verdict"].startswith("pdf_ok"):
            out["판정"] = "수집가능(PDF확인)"
        elif out["pdf_verdict"] == "":
            out["판정"] = "수집가능(HTML본문)"
        elif out["pdf_verdict"] == "pdf_fake":
            out["판정"] = "본문OK·PDF가짜"
        else:
            out["판정"] = "본문OK·PDF차단"
    elif out["detail_verdict"] == "본문차단마커":
        out["판정"] = "본문차단(부분봇차단)"
    elif out["detail_verdict"].startswith("http_"):
        out["판정"] = f"본문접근실패({out['detail_verdict']})"
    elif out["detail_verdict"] == "본문빈약(JS의심)":
        out["판정"] = "JS렌더필요"
    else:
        out["판정"] = f"본문접근실패({out['detail_verdict'] or '?'})"
    return done(out)


def done(out):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"{ts} [{out['host']}] {out['판정']} robots={out['robots_listing']} "
          f"detail={out['detail_verdict']} pdf={out['pdf_verdict']}", flush=True)
    return out


def main():
    rows = [r for r in csv.DictReader(open(IN_CSV, encoding="utf-8-sig"))
            if r["verdict"] == "ok"]
    print(f"tier-2 targets: {len(rows)}", flush=True)
    results = []
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(probe, rows):
            results.append(res)
    results.sort(key=lambda r: (r["기존분류"], r["판정"], r["host"]))
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    from collections import Counter
    print("\n== 판정 분포 ==", flush=True)
    for k, v in Counter(r["판정"] for r in results).most_common():
        print(f"  {k}: {v}", flush=True)
    print(f"\nsaved: {OUT_CSV}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
