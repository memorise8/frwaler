#!/usr/bin/env python3
"""
static_probe_recover.py — Phase 1 Auth-Blocked Recovery Probe

Retries auth_blocked URLs from coverage_report.csv using a Chrome 119 browser
User-Agent, capturing WAF/CDN signals and classifying the block reason.

Usage:
    .venv/bin/python scripts/static_probe_recover.py \
        --coverage-csv data/audit/coverage_report.csv \
        --report-md data/audit/auth_recovery_report.md \
        [--concurrency 30] [--per-host-rps 1.0] [--timeout 30]
"""

import argparse
import asyncio
import csv
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Browser headers — Chrome 119 / Windows 10 mimic
# ---------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Google Chrome";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

MAX_CONTENT_SIZE = 2 * 1024 * 1024  # 2 MB
LOGIN_PATTERN = re.compile(r"login|signin|authentication|sso/", re.IGNORECASE)

# WAF / bot-challenge detection patterns
CF_CHALLENGE_BODY = re.compile(
    r"just a moment|checking your browser|_cf_chl_opt|cf-please-wait|enable javascript and cookies",
    re.IGNORECASE,
)
CAPTCHA_PATTERN = re.compile(r"captcha|recaptcha|hcaptcha", re.IGNORECASE)
AKAMAI_PATTERN = re.compile(r"akamai|ak_bmsc|bm_sz|akamai bot", re.IGNORECASE)
GEO_PATTERN = re.compile(
    r"not available in your (country|region)|geographic restriction|access restricted.*country|country.*blocked",
    re.IGNORECASE,
)

# New CSV columns introduced by this script
NEW_COLUMNS = [
    "reprobed",
    "original_render_class",
    "recovery_status",
    "block_reason",
]

# Updated CSV fieldnames (original + new)
CSV_FIELDNAMES_ORIGINAL = [
    "entry_id", "sheet", "host", "url",
    "http_status", "final_url", "redirect_count", "content_type", "content_length",
    "robots_ok", "tls_warning",
    "anchor_count", "script_byte_ratio", "has_noscript_list", "lang_hint",
    "render_class", "error", "elapsed_ms",
]
CSV_FIELDNAMES = CSV_FIELDNAMES_ORIGINAL + NEW_COLUMNS


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RecoveryResult:
    entry_id: str
    host: str
    url: str
    # fetch outcome
    http_status: str = ""
    final_url: str = ""
    redirect_count: int = 0
    content_type: str = ""
    content_length: str = ""
    tls_warning: bool = False
    anchor_count: int = -1
    script_byte_ratio: float = -1.0
    has_noscript_list: bool = False
    lang_hint: str = ""
    render_class: str = "auth_blocked"
    error: str = ""
    elapsed_ms: int = 0
    # recovery-specific
    recovery_status: str = "error"   # recovered | still_blocked | error
    block_reason: str = ""
    # captured WAF signals
    waf_server: str = ""
    cf_ray: str = ""
    cf_mitigated: str = ""
    akamai_headers: str = ""
    x_powered_by: str = ""
    body_snippet: str = ""           # first 1 KB of response body
    _feed_confirmed: bool = field(default=False, repr=False)


# ---------------------------------------------------------------------------
# Classification helpers (reuse logic from static_probe.py)
# ---------------------------------------------------------------------------

def extract_html_metrics(html_text: str) -> dict:
    result = {
        "anchor_count": -1,
        "script_byte_ratio": -1.0,
        "has_noscript_list": False,
        "lang_hint": "",
    }
    soup = None
    for parser in ["lxml", "html5lib", "html.parser"]:
        try:
            soup = BeautifulSoup(html_text, parser)
            break
        except Exception:
            continue
    if soup is None:
        return result
    try:
        anchors = soup.find_all("a", href=True)
        result["anchor_count"] = len(anchors)
        scripts = soup.find_all("script")
        script_bytes = sum(len(str(tag)) for tag in scripts)
        result["script_byte_ratio"] = round(script_bytes / max(1, len(html_text)), 3)
        for noscript in soup.find_all("noscript"):
            if len(noscript.find_all("a", href=True)) >= 3:
                result["has_noscript_list"] = True
                break
        lang = None
        if soup.html and soup.html.get("lang"):
            lang = soup.html.get("lang")
        if not lang:
            meta_charset = soup.find("meta", charset=True)
            if meta_charset:
                lang = meta_charset.get("charset", "")
        if not lang:
            og_locale = soup.find("meta", property="og:locale")
            if og_locale:
                lang = og_locale.get("content", "")
        result["lang_hint"] = lang or ""
    except Exception:
        pass
    return result


def classify_render(r: RecoveryResult) -> str:
    """Same classification logic as static_probe.classify(), minus robots check."""
    status = int(r.http_status) if r.http_status.isdigit() else 0

    if status in (401, 403):
        return "auth_blocked"
    if status == 200 and r.final_url and LOGIN_PATTERN.search(r.final_url):
        return "auth_blocked"
    if r.error:
        return "dead"
    if 400 <= status <= 499 and status not in (401, 403):
        return "dead"
    if 500 <= status <= 599:
        return "dead"

    ct = r.content_type.lower()

    is_feed_ct = any(x in ct for x in ("rss", "atom", "application/xml", "text/xml"))
    is_feed_url = any(r.url.lower().endswith(s) for s in ("/rss", ".rss", ".atom", ".xml"))
    if (is_feed_ct or is_feed_url) and r._feed_confirmed:
        return "feed"

    if ct.startswith("application/pdf"):
        return "pdf_direct"

    if "text/html" in ct and r.anchor_count >= 10 and r.script_byte_ratio < 0.6:
        return "static_list"

    if "text/html" in ct and (r.anchor_count < 10 or r.script_byte_ratio >= 0.6):
        return "spa_likely"

    return "unknown"


def determine_block_reason(r: RecoveryResult) -> str:
    """Infer why a URL is still blocked from response signals."""
    status = int(r.http_status) if r.http_status.isdigit() else 0
    body_lower = r.body_snippet.lower()
    has_cf_ray = bool(r.cf_ray)
    has_cf_mitigated = bool(r.cf_mitigated)

    # Cloudflare challenge (200 with JS challenge page)
    if CF_CHALLENGE_BODY.search(r.body_snippet) or has_cf_mitigated:
        return "cloudflare_challenge"

    # Cloudflare 403
    if has_cf_ray and status == 403:
        return "cloudflare_403"

    # Akamai
    if r.akamai_headers or AKAMAI_PATTERN.search(r.body_snippet):
        return "akamai_bot"

    # CAPTCHA redirect / page
    final_url_lower = r.final_url.lower()
    if CAPTCHA_PATTERN.search(r.body_snippet) or "captcha" in final_url_lower:
        return "captcha_redirect"

    # Geo block
    if GEO_PATTERN.search(r.body_snippet):
        return "geo_block"

    # Login redirect (200 but landed on login page)
    if status == 200 and r.final_url and LOGIN_PATTERN.search(r.final_url):
        return "login_redirect"

    if status == 403:
        return "still_403"
    if status == 401:
        return "still_401"
    if status != 0:
        return f"other_{status}"

    return "error"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class HostRateLimit:
    def __init__(self, per_host_rps: float):
        self._interval = 1.0 / per_host_rps
        self._last: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def wait(self, host: str):
        async with self._locks[host]:
            now = time.monotonic()
            elapsed = now - self._last[host]
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last[host] = time.monotonic()


# ---------------------------------------------------------------------------
# HTTP client factory
# ---------------------------------------------------------------------------

def make_browser_client(timeout: float, verify: bool = True, http2: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(timeout),
        verify=verify,
        http2=http2,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )


def _classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        if "ssl" in msg or "certificate" in msg or "tls" in msg:
            return "ssl"
        if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
            return "dns"
        if "refused" in msg:
            return "connect_refused"
        return "connect_error"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol_error"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too_many_redirects"
    return "error"


async def _do_fetch(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, bytes]:
    async with client.stream("GET", url) as resp:
        chunks = []
        total = 0
        async for chunk in resp.aiter_bytes(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_CONTENT_SIZE:
                break
    body = b"".join(chunks)[:MAX_CONTENT_SIZE]
    return resp, body


# ---------------------------------------------------------------------------
# Core fetch + classify for one URL
# ---------------------------------------------------------------------------

async def probe_one(
    row: dict,
    client: httpx.AsyncClient,
    client_noverify: httpx.AsyncClient,
    client_h1: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    rate_limit: HostRateLimit,
) -> RecoveryResult:
    host = row["host"]
    url = row["url"]
    result = RecoveryResult(entry_id=row["entry_id"], host=host, url=url)

    async with semaphore:
        await rate_limit.wait(host)
        start = time.monotonic()
        tls_warning = False
        resp = None
        body = b""

        try:
            resp, body = await _do_fetch(client, url)
        except httpx.RemoteProtocolError:
            try:
                await rate_limit.wait(host)
                resp, body = await _do_fetch(client_h1, url)
            except Exception as e2:
                result.error = _classify_error(e2)
                result.elapsed_ms = int((time.monotonic() - start) * 1000)
                result.recovery_status = "error"
                result.block_reason = "error"
                return result
        except httpx.ConnectError as e:
            err_msg = str(e).lower()
            if "ssl" in err_msg or "certificate" in err_msg or "tls" in err_msg:
                try:
                    await rate_limit.wait(host)
                    resp, body = await _do_fetch(client_noverify, url)
                    tls_warning = True
                except Exception as e2:
                    result.error = _classify_error(e2)
                    result.elapsed_ms = int((time.monotonic() - start) * 1000)
                    result.recovery_status = "error"
                    result.block_reason = "error"
                    return result
            else:
                result.error = _classify_error(e)
                result.elapsed_ms = int((time.monotonic() - start) * 1000)
                result.recovery_status = "error"
                result.block_reason = "error"
                return result
        except Exception as e:
            result.error = _classify_error(e)
            result.elapsed_ms = int((time.monotonic() - start) * 1000)
            result.recovery_status = "error"
            result.block_reason = "error"
            return result

        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        result.tls_warning = tls_warning
        result.http_status = str(resp.status_code)
        result.final_url = str(resp.url)
        result.redirect_count = len(resp.history)

        ct_raw = resp.headers.get("content-type", "")
        result.content_type = ct_raw.split(";")[0].strip().lower()
        result.content_length = str(len(body))

        # Capture WAF/CDN signal headers
        result.waf_server = resp.headers.get("server", "")
        result.cf_ray = resp.headers.get("cf-ray", "")
        result.cf_mitigated = resp.headers.get("cf-mitigated", "")
        akamai_hdrs = {
            k: v for k, v in resp.headers.items()
            if k.lower().startswith("x-akamai-")
        }
        result.akamai_headers = "; ".join(f"{k}={v}" for k, v in akamai_hdrs.items())
        result.x_powered_by = resp.headers.get("x-powered-by", "")

        # Body snippet (first 1 KB decoded)
        try:
            encoding = resp.encoding or resp.charset_encoding or "utf-8"
            body_text = body[:1024].decode(encoding, errors="replace")
        except Exception:
            body_text = body[:1024].decode("utf-8", errors="replace")
        result.body_snippet = body_text

        # Feed confirmation
        if result.content_type in ("application/rss+xml", "application/atom+xml",
                                    "application/xml", "text/xml") or \
                any(url.lower().endswith(s) for s in ("/rss", ".rss", ".atom", ".xml")):
            snippet_lower = body_text.strip().lower()
            if "<rss" in snippet_lower or "<feed" in snippet_lower:
                result._feed_confirmed = True

        # HTML metrics
        if "text/html" in result.content_type and body:
            try:
                encoding2 = resp.encoding or resp.charset_encoding or "utf-8"
                html_text = body.decode(encoding2, errors="replace")
                metrics = extract_html_metrics(html_text)
                result.anchor_count = metrics["anchor_count"]
                result.script_byte_ratio = metrics["script_byte_ratio"]
                result.has_noscript_list = metrics["has_noscript_list"]
                result.lang_hint = metrics["lang_hint"]
            except Exception:
                pass

        # Classify render
        new_render_class = classify_render(result)
        result.render_class = new_render_class

        # Determine recovery outcome
        if new_render_class not in ("auth_blocked", "dead", "unknown"):
            result.recovery_status = "recovered"
            result.block_reason = ""
        elif new_render_class == "auth_blocked" or result.render_class == "auth_blocked":
            result.recovery_status = "still_blocked"
            result.block_reason = determine_block_reason(result)
        else:
            # dead or unknown after retry
            result.recovery_status = "still_blocked"
            result.block_reason = determine_block_reason(result)

        return result


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_csv(path: str, rows: list[dict]):
    # Determine fieldnames: preserve original order, append new columns at end
    fieldnames = CSV_FIELDNAMES_ORIGINAL[:]
    for col in NEW_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def apply_recovery_to_row(row: dict, result: RecoveryResult) -> dict:
    """Merge RecoveryResult back into CSV row dict (in-place update)."""
    row["reprobed"] = "true"
    row["original_render_class"] = "auth_blocked"
    row["recovery_status"] = result.recovery_status
    row["block_reason"] = result.block_reason

    if result.recovery_status == "recovered":
        # Update all probe fields with fresh data
        row["render_class"] = result.render_class
        row["http_status"] = result.http_status
        row["final_url"] = result.final_url if result.final_url != result.url else ""
        row["redirect_count"] = str(result.redirect_count)
        row["content_type"] = result.content_type
        row["content_length"] = result.content_length
        row["tls_warning"] = str(result.tls_warning).lower()
        row["anchor_count"] = result.anchor_count if result.anchor_count >= 0 else ""
        row["script_byte_ratio"] = f"{result.script_byte_ratio:.3f}" if result.script_byte_ratio >= 0 else ""
        row["has_noscript_list"] = str(result.has_noscript_list).lower()
        row["lang_hint"] = result.lang_hint or ""
        row["error"] = result.error
        row["elapsed_ms"] = str(result.elapsed_ms)
    # still_blocked / error: keep original render_class=auth_blocked, just add new cols

    return row


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _top_hosts(rows: list[dict], n: int = 5) -> str:
    hosts = Counter(r["host"] for r in rows)
    return ", ".join(h for h, _ in hosts.most_common(n))


def _top_hosts_detail(rows: list[dict], n: int = 20) -> list[dict]:
    """Group still_blocked rows by host, return top N by URL count."""
    host_data: dict[str, dict] = {}
    for r in rows:
        h = r["host"]
        if h not in host_data:
            host_data[h] = {
                "host": h,
                "urls": [],
                "reasons": Counter(),
                "waf_servers": set(),
                "cf_ray_sample": "",
                "body_sample": "",
                "block_reason": r.get("block_reason", ""),
            }
        host_data[h]["urls"].append(r["url"])
        host_data[h]["reasons"][r.get("block_reason", "")] += 1
        if r.get("_waf_server"):
            host_data[h]["waf_servers"].add(r["_waf_server"])
        if not host_data[h]["cf_ray_sample"] and r.get("_cf_ray"):
            host_data[h]["cf_ray_sample"] = r["_cf_ray"]
        if not host_data[h]["body_sample"] and r.get("_body_snippet"):
            host_data[h]["body_sample"] = r["_body_snippet"][:200]

    sorted_hosts = sorted(host_data.values(), key=lambda x: len(x["urls"]), reverse=True)
    return sorted_hosts[:n]


def recommend_action(block_reason: str) -> str:
    mapping = {
        "cloudflare_challenge": "Playwright + Cookie 보관 필요 (CF challenge 우회)",
        "cloudflare_403": "Playwright + residential proxy 또는 운영자 API 키 요청",
        "akamai_bot": "Playwright + Akamai 봇 관리 우회 (stealth mode) 또는 운영자 검토",
        "captcha_redirect": "자동화 불가 — 운영자에 API/데이터 제공 요청",
        "geo_block": "다른 IP(KR 서버) 또는 VPN 시도 / 운영자 검토",
        "still_403": "Playwright + 쿠키 보관 또는 다른 IP 시도",
        "still_401": "인증 필요 — API 키 또는 운영자 계약 필요",
        "login_redirect": "로그인 세션 관리 필요 — Playwright + 자격증명 보관",
        "error": "네트워크 오류 — DNS/방화벽 또는 사이트 다운 확인",
    }
    for key, action in mapping.items():
        if block_reason.startswith(key):
            return action
    if block_reason.startswith("other_"):
        return f"HTTP {block_reason.replace('other_', '')} 오류 — 운영자 검토 필요"
    return "원인 미상 — 수동 확인 필요"


def generate_report(
    all_rows: list[dict],
    reprobed_results: dict[str, RecoveryResult],  # entry_id -> result
    report_path: str,
):
    """Generate the markdown analysis report."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    reprobed_rows = [r for r in all_rows if r.get("reprobed") == "true"]
    recovered = [r for r in reprobed_rows if r.get("recovery_status") == "recovered"]
    still_blocked = [r for r in reprobed_rows if r.get("recovery_status") == "still_blocked"]
    error_rows = [r for r in reprobed_rows if r.get("recovery_status") == "error"]

    total_reprobed = len(reprobed_rows)
    n_rec = len(recovered)
    n_still = len(still_blocked)
    n_err = len(error_rows)

    def pct(n):
        return f"{n * 100 / total_reprobed:.1f}%" if total_reprobed else "0%"

    # recovered render_class distribution
    rec_dist = Counter(r["render_class"] for r in recovered)

    # still_blocked by reason
    reason_dist = Counter(r.get("block_reason", "") for r in still_blocked)

    # Build host detail section for still_blocked
    # Attach WAF info from in-memory results
    still_blocked_with_meta = []
    for row in still_blocked:
        eid = row["entry_id"]
        res = reprobed_results.get(eid)
        meta = dict(row)
        if res:
            meta["_waf_server"] = res.waf_server
            meta["_cf_ray"] = res.cf_ray
            meta["_body_snippet"] = res.body_snippet
        still_blocked_with_meta.append(meta)

    host_details = _top_hosts_detail(still_blocked_with_meta, n=20)

    lines = []
    lines.append("# Auth-blocked Recovery Report")
    lines.append(f"생성일: {now_iso}")
    lines.append(f"재프로브 대상: {total_reprobed}건")
    lines.append("방법: Chrome 119 브라우저 헤더 세트로 재시도")
    lines.append("")

    lines.append("## 요약")
    lines.append(f"- 회복(recovered): {n_rec} ({pct(n_rec)})")
    lines.append(f"- 여전히 차단(still_blocked): {n_still} ({pct(n_still)})")
    lines.append(f"- 재프로브 중 에러(error): {n_err} ({pct(n_err)})")
    lines.append("")

    lines.append("## 회복 후 분포 (recovered 만)")
    lines.append("| 새 render_class | 건수 |")
    lines.append("|---|---|")
    for cls, cnt in sorted(rec_dist.items(), key=lambda x: -x[1]):
        lines.append(f"| {cls} | {cnt} |")
    lines.append("")

    lines.append("## 사유별 still_blocked")
    lines.append("| block_reason | 건수 | 대표 host (상위 5개) |")
    lines.append("|---|---|---|")
    for reason, cnt in sorted(reason_dist.items(), key=lambda x: -x[1]):
        sub = [r for r in still_blocked if r.get("block_reason") == reason]
        top5 = _top_hosts(sub, n=5)
        lines.append(f"| {reason} | {cnt} | {top5} |")
    lines.append("")

    lines.append("## host 별 분석 (still_blocked, 상위 20 hosts)")
    lines.append("")
    for hd in host_details:
        host = hd["host"]
        url_count = len(hd["urls"])
        reasons_str = ", ".join(f"{r}×{c}" for r, c in hd["reasons"].most_common(3))
        primary_reason = hd["reasons"].most_common(1)[0][0] if hd["reasons"] else ""
        action = recommend_action(primary_reason)

        lines.append(f"### {host}")
        lines.append(f"- 차단된 URL 수: {url_count}")
        lines.append(f"- 추론된 사유: {reasons_str}")
        waf_info = ", ".join(hd["waf_servers"]) if hd["waf_servers"] else "(없음)"
        lines.append(f"- 응답 헤더 sample: Server={waf_info}" + (f", Cf-Ray={hd['cf_ray_sample']}" if hd['cf_ray_sample'] else ""))
        body_s = hd["body_sample"].replace("\n", " ").replace("\r", "")[:200] if hd["body_sample"] else "(없음)"
        lines.append(f"- 본문 첫 200자 sample: `{body_s}`")
        lines.append(f"- 권장 조치: {action}")
        lines.append("")

    lines.append("## 권장 후속 조치")
    lines.append("")
    lines.append("### Phase 2 정상 진행 (recovered 항목)")
    lines.append(f"- {n_rec}건이 브라우저 UA 로 정상 접근됨 → Phase 2 크롤 대상에 포함")
    lines.append("")

    cf_challenge_cnt = reason_dist.get("cloudflare_challenge", 0)
    cf_403_cnt = reason_dist.get("cloudflare_403", 0)
    akamai_cnt = reason_dist.get("akamai_bot", 0)
    captcha_cnt = reason_dist.get("captcha_redirect", 0)
    auto_impossible = captcha_cnt
    playwright_needed = cf_challenge_cnt + cf_403_cnt + akamai_cnt

    lines.append("### Phase 3 sample 에서 제외 권장 (자동화 불가 사이트들)")
    lines.append(f"- CAPTCHA 리디렉션: {captcha_cnt}건 → 자동화 불가, 운영자 API 협의 필요")
    lines.append(f"- 총 자동화 불가 추정: {auto_impossible}건")
    lines.append("")

    lines.append("### 별도 보강 필요 (Playwright 분기 등)")
    lines.append(f"- Cloudflare challenge/403: {cf_challenge_cnt + cf_403_cnt}건")
    lines.append(f"- Akamai bot management: {akamai_cnt}건")
    lines.append(f"- 소계: {playwright_needed}건 → Playwright stealth mode 또는 운영자 API 키 확보 필요")
    geo_cnt = reason_dist.get("geo_block", 0)
    if geo_cnt:
        lines.append(f"- 지리적 차단: {geo_cnt}건 → KR IP 서버에서 재시도 권장")
    lines.append("")

    report_path_obj = Path(report_path)
    report_path_obj.parent.mkdir(parents=True, exist_ok=True)
    report_path_obj.write_text("\n".join(lines), encoding="utf-8")
    print(f"[recover] report written → {report_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------

async def run(args):
    csv_path = args.coverage_csv
    report_path = args.report_md

    print(f"[recover] loading {csv_path}", file=sys.stderr)
    all_rows = load_csv(csv_path)

    # Ensure new columns exist in all rows (idempotent)
    for row in all_rows:
        for col in NEW_COLUMNS:
            if col not in row:
                row[col] = ""

    # Select targets: auth_blocked and NOT already reprobed
    targets = [
        r for r in all_rows
        if r["render_class"] == "auth_blocked" and r.get("reprobed") != "true"
    ]
    already_done = sum(1 for r in all_rows if r.get("reprobed") == "true")

    print(f"[recover] total rows: {len(all_rows)}", file=sys.stderr)
    print(f"[recover] already reprobed (skip): {already_done}", file=sys.stderr)
    print(f"[recover] new targets: {len(targets)}", file=sys.stderr)

    if not targets:
        print("[recover] nothing to probe — generating report only", file=sys.stderr)
        reprobed_results: dict[str, RecoveryResult] = {}
        generate_report(all_rows, reprobed_results, report_path)
        return

    semaphore = asyncio.Semaphore(args.concurrency)
    rate_limit = HostRateLimit(args.per_host_rps)

    # Build entry_id -> row index map for fast updates
    row_by_id = {r["entry_id"]: r for r in all_rows}

    reprobed_results: dict[str, RecoveryResult] = {}
    done_count = 0
    total = len(targets)
    status_counter: Counter = Counter()

    start_wall = time.monotonic()

    async with make_browser_client(args.timeout) as client, \
               make_browser_client(args.timeout, verify=False) as client_noverify, \
               make_browser_client(args.timeout, http2=False) as client_h1:

        coros = [
            probe_one(row, client, client_noverify, client_h1, semaphore, rate_limit)
            for row in targets
        ]

        for coro in asyncio.as_completed(coros):
            try:
                result = await coro
            except Exception as e:
                print(f"[recover] unexpected error: {e}", file=sys.stderr)
                continue

            # Merge result back into CSV row
            orig_row = row_by_id.get(result.entry_id)
            if orig_row is not None:
                apply_recovery_to_row(orig_row, result)

            reprobed_results[result.entry_id] = result
            done_count += 1
            status_counter[result.recovery_status] += 1

            if done_count % 50 == 0 or done_count == total:
                elapsed = time.monotonic() - start_wall
                pct = done_count * 100 // total
                dist_str = " ".join(f"{k}={v}" for k, v in sorted(status_counter.items()))
                print(
                    f"[recover] {done_count}/{total} ({pct}%) — "
                    f"elapsed {elapsed:.1f}s — {dist_str}",
                    file=sys.stderr,
                )

    elapsed_total = time.monotonic() - start_wall
    print(f"[recover] probe done in {elapsed_total:.1f}s", file=sys.stderr)
    print(f"[recover] recovery_status: {dict(status_counter)}", file=sys.stderr)

    # Save updated CSV in-place
    save_csv(csv_path, all_rows)
    print(f"[recover] CSV updated → {csv_path}", file=sys.stderr)

    # Generate report
    generate_report(all_rows, reprobed_results, report_path)


def parse_args():
    p = argparse.ArgumentParser(description="Auth-blocked recovery probe with browser UA")
    p.add_argument("--coverage-csv", required=True, help="Path to coverage_report.csv (updated in-place)")
    p.add_argument("--report-md", required=True, help="Path to output auth_recovery_report.md")
    p.add_argument("--concurrency", type=int, default=30)
    p.add_argument("--per-host-rps", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=30.0)
    return p.parse_args()


def main():
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
