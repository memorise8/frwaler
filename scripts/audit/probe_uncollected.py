# -*- coding: utf-8 -*-
"""미수집 URL 내용 확인(프로빙).

test.xlsx 미수집 350개 URL을 라이브로 열어:
  - HTTP 상태 / 죽음 / 차단
  - 페이지 제목
  - PDF 링크 수, 문서 카운트 힌트(있으면)
  - 크롤 가능성 판정 + 미수집 추정 사유
결과: scripts/audit/uncollected_probe.csv
"""
from __future__ import annotations
import csv, re, sys, time, pickle
from pathlib import Path
from urllib.parse import urlparse, urljoin

from curl_cffi import requests as creq

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
OUT = ROOT / "scripts/audit/uncollected_probe.csv"
PKL = "/home/ruci/.claude/jobs/c789169f/tmp/test_url_match.pkl"

COUNT_WORD = (r"results?|résultats?|resultados?|documentos?|documents?|items?|"
              r"records?|Ergebnisse|risultati|resultaten|entries|件|文档|개")
COUNT_RE = re.compile(r"([\d][\d.,]{0,12})\s*(?:%s)\b" % COUNT_WORD, re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def fetch(url):
    last = None
    for attempt in range(2):
        try:
            r = creq.get(url, impersonate="chrome", timeout=15,
                         allow_redirects=True)
            return r.status_code, r.text, None
        except Exception as e:
            last = str(e)[:60]
            time.sleep(1)
    return None, None, last


def analyze(url, status, html):
    if status is None:
        return "죽음/접속불가", 0, None, ""
    if status in (403, 401, 429) or (status == 200 and html and
            ("cf-challenge" in html.lower() or "captcha" in html.lower() or
             "just a moment" in html.lower())):
        return "차단(봇방어)", 0, None, _title(html)
    if status >= 400:
        return f"HTTP{status}", 0, None, _title(html)
    if not html:
        return "빈응답", 0, None, ""
    title = _title(html)
    # pdf 링크 수
    pdfs = len(re.findall(r'href=["\'][^"\']*\.pdf', html, re.I))
    # 문서 카운트 힌트
    cnt = None
    m = COUNT_RE.search(re.sub(r"<[^>]+>", " ", html))
    if m:
        try:
            v = int(m.group(1).replace(",", "").replace(".", ""))
            if 1 <= v <= 50_000_000 and not (1900 <= v <= 2100):
                cnt = v
        except Exception:
            pass
    # 판정
    body_len = len(re.sub(r"<[^>]+>", "", html))
    if pdfs >= 1:
        verdict = "크롤가능(PDF링크有)"
    elif body_len < 500:
        verdict = "JS렌더의심(정적본문빈약)"
    else:
        verdict = "크롤가능(HTML,PDF미검출)"
    return verdict, pdfs, cnt, title


def _title(html):
    if not html:
        return ""
    m = TITLE_RE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else ""


def main():
    res = pickle.load(open(PKL, "rb"))
    # 미수집만: 상태가 '수집'으로 시작하지 않는 것
    targets = [x for x in res if not x[8].startswith("수집")]
    print(f"미수집 프로빙 대상: {len(targets)}개", flush=True)
    rows = []
    for i, x in enumerate(targets, 1):
        sheet, seq, org, url = x[0], x[1], x[2], str(x[3])
        status, html, err = fetch(url)
        verdict, pdfs, cnt, title = analyze(url, status, html)
        rows.append(dict(시트명=sheet, 일련번호=seq, 기관명=org, URL주소=url,
                         상태코드=status if status is not None else f"ERR:{err}",
                         페이지제목=title, PDF링크수=pdfs,
                         문서수힌트=cnt if cnt is not None else "",
                         크롤판정=verdict))
        if i % 25 == 0:
            print(f"[{i}/{len(targets)}] {verdict} <- {url[:50]}", flush=True)
        time.sleep(0.4)
    cols = ["시트명", "일련번호", "기관명", "URL주소", "상태코드",
            "페이지제목", "PDF링크수", "문서수힌트", "크롤판정"]
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    # 요약
    import collections
    vc = collections.Counter(r["크롤판정"] for r in rows)
    print("\n=== 미수집 프로빙 판정 분포 ===", flush=True)
    for k, v in vc.most_common():
        print(f"  {k}: {v}", flush=True)
    print(f"\n저장: {OUT}", flush=True)


if __name__ == "__main__":
    main()
