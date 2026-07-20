#!/usr/bin/env python3
"""미수집 152곳 재실사 — 1차 프로브 (2026-07-18).

로그인 실사(20260713)와 같은 방법론:
  ① 브라우저 UA로 접속 → HTTP 상태/최종 URL 확인
  ② 차단 유형 판정 (cloudflare / captcha / 403 / dns / timeout / ssl ...)
  ③ robots.txt 전면 Disallow 여부 확인
결과: data/audit/reaudit_tier1_20260718.csv
2차(목록→상세→PDF 심층)는 1차에서 살아있는 곳만 별도 진행.
"""
import csv
import re
import sys
import socket
import threading
import concurrent.futures as cf
from datetime import datetime, timezone

import requests
import urllib3

urllib3.disable_warnings()

IN_CSV = "data/audit/reaudit_152_targets.csv"
OUT_CSV = "data/audit/reaudit_tier1_20260718.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
}
TIMEOUT = 25

BLOCK_MARKERS = [
    ("cloudflare_challenge", re.compile(r"cf-browser-verification|challenge-platform|Just a moment|_cf_chl", re.I)),
    ("captcha", re.compile(r"captcha|hcaptcha|recaptcha", re.I)),
    ("akamai_block", re.compile(r"akamai|Reference #\d", re.I)),
    ("incapsula_block", re.compile(r"incapsula|imperva", re.I)),
    ("generic_denied", re.compile(r"access denied|forbidden|not authorized|blocked", re.I)),
]

_tls = threading.local()


def sess():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _tls.s = s
    return _tls.s


def classify_body(status, text):
    for name, rx in BLOCK_MARKERS:
        if rx.search(text[:20000]):
            return name
    if status == 200:
        return "ok"
    return f"http_{status}"


def fetch(url):
    """(status, final_url, body, error) — verify=True 실패 시 False 폴백."""
    for verify in (True, False):
        try:
            r = sess().get(url, timeout=TIMEOUT, allow_redirects=True, verify=verify)
            return r.status_code, r.url, r.text or "", ""
        except requests.exceptions.SSLError:
            if verify:
                continue
            return None, "", "", "ssl_error"
        except requests.exceptions.ConnectTimeout:
            return None, "", "", "timeout"
        except requests.exceptions.ReadTimeout:
            return None, "", "", "timeout"
        except requests.exceptions.ConnectionError as e:
            msg = str(e)
            if "Name or service not known" in msg or "getaddrinfo" in msg or "NameResolutionError" in msg:
                return None, "", "", "dns_fail"
            return None, "", "", "conn_error"
        except Exception as e:
            return None, "", "", f"error:{type(e).__name__}"
    return None, "", "", "unreachable"


def robots_disallow_all(base_url):
    try:
        from urllib.parse import urlparse
        p = urlparse(base_url)
        r = sess().get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=15, verify=False)
        if r.status_code != 200:
            return "no_robots"
        agent_all = False
        for line in r.text.splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("user-agent:"):
                agent_all = line.split(":", 1)[1].strip() == "*"
            elif agent_all and low.startswith("disallow:"):
                if line.split(":", 1)[1].strip() == "/":
                    return "disallow_all"
        return "allows_some"
    except Exception:
        return "robots_unreachable"


def probe(row):
    host, url = row["host"], row["url"]
    status, final_url, body, err = fetch(url)
    if err and not url.startswith("https://"):
        # http → https 승격 재시도
        status, final_url, body, err = fetch(url.replace("http://", "https://", 1))
    if err:
        verdict = err
    else:
        verdict = classify_body(status, body)
    robots = robots_disallow_all(final_url or url) if not err else ""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"{ts} [{host}] {verdict} status={status} robots={robots}", flush=True)
    return {
        "host": host,
        "기존분류": row["분류"],
        "기존사유": row["block_reason"],
        "url": url,
        "http_status": status if status is not None else "",
        "final_url": final_url,
        "verdict": verdict,
        "robots": robots,
        "body_bytes": len(body),
    }


def main():
    rows = list(csv.DictReader(open(IN_CSV, encoding="utf-8-sig")))
    print(f"targets: {len(rows)}", flush=True)
    socket.setdefaulttimeout(TIMEOUT)
    results = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(probe, rows):
            results.append(res)
    results.sort(key=lambda r: (r["기존분류"], r["verdict"], r["host"]))
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    from collections import Counter
    print("\n== verdict 분포 ==", flush=True)
    for k, v in Counter(r["verdict"] for r in results).most_common():
        print(f"  {k}: {v}", flush=True)
    print(f"\nsaved: {OUT_CSV}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
