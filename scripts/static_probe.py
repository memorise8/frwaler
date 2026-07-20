#!/usr/bin/env python3
"""
static_probe.py — Phase 1 Static Probe
Fetches 1,994 URLs concurrently, measures HTML metrics, classifies render type.

Usage:
    .venv/bin/python scripts/static_probe.py \
        --input data/audit/scroll_index.json \
        --output data/audit/coverage_report.csv \
        [--concurrency 50] [--per-host-rps 1.0] \
        [--timeout 30] [--limit N]
"""

import argparse
import asyncio
import csv
import json
import re
import signal
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "crawler-poc-audit/1.0 (+contact@financenow.co.kr)"
MAX_CONTENT_SIZE = 2 * 1024 * 1024  # 2 MB
LOGIN_PATTERN = re.compile(r"login|signin|authentication|sso/", re.IGNORECASE)
CSV_FIELDNAMES = [
    "entry_id", "sheet", "host", "url",
    "http_status", "final_url", "redirect_count", "content_type", "content_length",
    "robots_ok", "tls_warning",
    "anchor_count", "script_byte_ratio", "has_noscript_list", "lang_hint",
    "render_class", "error",
    "elapsed_ms",
]


@dataclass
class ProbeResult:
    entry_id: str
    sheet: str
    host: str
    url: str
    http_status: str = ""
    final_url: str = ""
    redirect_count: int = 0
    content_type: str = ""
    content_length: str = ""
    robots_ok: bool = True
    tls_warning: bool = False
    anchor_count: int = -1
    script_byte_ratio: float = -1.0
    has_noscript_list: bool = False
    lang_hint: str = ""
    render_class: str = "unknown"
    error: str = ""
    elapsed_ms: int = 0


def parse_args():
    p = argparse.ArgumentParser(description="Phase 1 Static Probe")
    p.add_argument("--input", required=True, help="Path to scroll_index.json")
    p.add_argument("--output", required=True, help="Path to output CSV")
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--per-host-rps", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args()


def load_entries(path: str, limit: Optional[int] = None):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries = data["entries"]
    if limit:
        entries = entries[:limit]
    return entries


def make_client(timeout: float, verify: bool = True, http2: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(timeout),
        verify=verify,
        http2=http2,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )


def extract_html_metrics(html_text: str) -> dict:
    """Parse HTML and extract anchor_count, script_byte_ratio, has_noscript_list, lang_hint."""
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
        result["script_byte_ratio"] = round(
            script_bytes / max(1, len(html_text)), 3
        )

        # has_noscript_list: any noscript with >= 3 <a href> inside
        for noscript in soup.find_all("noscript"):
            ns_anchors = noscript.find_all("a", href=True)
            if len(ns_anchors) >= 3:
                result["has_noscript_list"] = True
                break

        # lang_hint: html lang attr > meta charset > og:locale
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


def classify(result: ProbeResult) -> str:
    """Apply classification rules in priority order."""
    # 1. robots_blocked
    if not result.robots_ok:
        return "robots_blocked"

    status = int(result.http_status) if result.http_status.isdigit() else 0

    # 2. auth_blocked
    if status in (401, 403):
        return "auth_blocked"
    # auth via redirect to login page
    if status == 200 and result.final_url and LOGIN_PATTERN.search(result.final_url):
        return "auth_blocked"

    # 3. dead — 4xx (excl 401/403), 5xx, or any network/fetch error
    if result.error:
        return "dead"
    if 400 <= status <= 499 and status not in (401, 403):
        return "dead"
    if 500 <= status <= 599:
        return "dead"

    ct = result.content_type.lower()

    # 4. feed
    is_feed_ct = any(x in ct for x in ("rss", "atom", "application/xml", "text/xml"))
    url_lower = result.url.lower()
    is_feed_url = any(url_lower.endswith(s) for s in ("/rss", ".rss", ".atom", ".xml"))
    # We'd need body to confirm <rss or <feed — stored in content_type check only
    # The actual XML confirmation happens during fetch via _feed_confirmed flag
    if (is_feed_ct or is_feed_url) and getattr(result, "_feed_confirmed", False):
        return "feed"

    # 5. pdf_direct
    if ct.startswith("application/pdf"):
        return "pdf_direct"

    # 6. static_list
    if "text/html" in ct and result.anchor_count >= 10 and result.script_byte_ratio < 0.6:
        return "static_list"

    # 7. spa_likely
    if "text/html" in ct and (result.anchor_count < 10 or result.script_byte_ratio >= 0.6):
        return "spa_likely"

    return "unknown"


class RobotsCache:
    """Per-host robots.txt cache with rate limiting awareness."""

    def __init__(self):
        self._cache: dict[str, bool] = {}  # host -> can_fetch result
        self._lock = asyncio.Lock()
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def can_fetch(
        self,
        host: str,
        url: str,
        client: httpx.AsyncClient,
        host_rate: "HostRateLimit",
    ) -> bool:
        if host in self._cache:
            return self._cache[host]

        async with self._host_locks[host]:
            # double-check after acquiring
            if host in self._cache:
                return self._cache[host]

            robots_url = f"https://{host}/robots.txt"
            await host_rate.wait(host)
            try:
                resp = await client.get(robots_url)
                if resp.status_code >= 400:
                    # 4xx/5xx → allow
                    self._cache[host] = True
                    return True
                content = resp.text
                rp = RobotFileParser()
                rp.set_url(robots_url)
                rp.parse(content.splitlines())
                result = rp.can_fetch(USER_AGENT, url)
                self._cache[host] = result
                return result
            except Exception:
                # timeout / DNS / SSL → allow
                self._cache[host] = True
                return True


class HostRateLimit:
    """Per-host rate limiter: enforces minimum interval between requests."""

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


async def _do_fetch(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, bytes]:
    """Fetch URL, reading up to MAX_CONTENT_SIZE bytes. Returns (response, body)."""
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


async def fetch_one(
    entry: dict,
    client: httpx.AsyncClient,
    client_noverify: httpx.AsyncClient,
    client_h1: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    robots_cache: RobotsCache,
    rate_limit: HostRateLimit,
    timeout: float,
) -> ProbeResult:
    result = ProbeResult(
        entry_id=entry["entry_id"],
        sheet=entry["sheet"],
        host=entry["host"],
        url=entry["url"],
        final_url=entry["url"],
    )

    # robots.txt check
    robots_ok = await robots_cache.can_fetch(
        entry["host"], entry["url"], client, rate_limit
    )
    result.robots_ok = robots_ok
    if not robots_ok:
        result.render_class = "robots_blocked"
        return result

    async with semaphore:
        await rate_limit.wait(entry["host"])
        start = time.monotonic()
        tls_warning = False
        try:
            resp, body = await _do_fetch(client, entry["url"])
        except httpx.RemoteProtocolError:
            # HTTP/2 stream reset (error_code:2) — server doesn't support H2, retry with H1
            try:
                await rate_limit.wait(entry["host"])
                resp, body = await _do_fetch(client_h1, entry["url"])
            except Exception as e2:
                result.error = _classify_error(e2)
                result.elapsed_ms = int((time.monotonic() - start) * 1000)
                result.render_class = classify(result)
                return result
        except httpx.ConnectError as e:
            # SSL errors surface as ConnectError in httpx
            err_msg = str(e).lower()
            if "ssl" in err_msg or "certificate" in err_msg or "tls" in err_msg:
                try:
                    await rate_limit.wait(entry["host"])
                    resp, body = await _do_fetch(client_noverify, entry["url"])
                    tls_warning = True
                except Exception as e2:
                    result.error = _classify_error(e2)
                    result.elapsed_ms = int((time.monotonic() - start) * 1000)
                    result.render_class = classify(result)
                    return result
            else:
                result.error = _classify_error(e)
                result.elapsed_ms = int((time.monotonic() - start) * 1000)
                result.render_class = classify(result)
                return result
        except Exception as e:
            result.error = _classify_error(e)
            result.elapsed_ms = int((time.monotonic() - start) * 1000)
            result.render_class = classify(result)
            return result

        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        result.tls_warning = tls_warning
        result.http_status = str(resp.status_code)
        result.final_url = str(resp.url)
        result.redirect_count = len(resp.history)

        # content-type (strip charset etc.)
        ct_raw = resp.headers.get("content-type", "")
        result.content_type = ct_raw.split(";")[0].strip().lower()
        result.content_length = str(len(body))

        # feed confirmation: check if XML body starts with <rss or <feed
        if result.content_type in ("application/rss+xml", "application/atom+xml",
                                    "application/xml", "text/xml") or \
                any(result.url.lower().endswith(s) for s in ("/rss", ".rss", ".atom", ".xml")):
            try:
                snippet = body[:500].decode("utf-8", errors="replace").strip().lower()
                if "<rss" in snippet or "<feed" in snippet:
                    result._feed_confirmed = True
            except Exception:
                pass

        # HTML metrics
        if "text/html" in result.content_type and body:
            try:
                encoding = resp.encoding or resp.charset_encoding or "utf-8"
                html_text = body.decode(encoding, errors="replace")
                metrics = extract_html_metrics(html_text)
                result.anchor_count = metrics["anchor_count"]
                result.script_byte_ratio = metrics["script_byte_ratio"]
                result.has_noscript_list = metrics["has_noscript_list"]
                result.lang_hint = metrics["lang_hint"]
            except Exception:
                pass

        result.render_class = classify(result)
        return result


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


def result_to_row(r: ProbeResult) -> dict:
    return {
        "entry_id": r.entry_id,
        "sheet": r.sheet,
        "host": r.host,
        "url": r.url,
        "http_status": r.http_status,
        "final_url": r.final_url if r.final_url != r.url else "",
        "redirect_count": r.redirect_count,
        "content_type": r.content_type,
        "content_length": r.content_length,
        "robots_ok": str(r.robots_ok).lower(),
        "tls_warning": str(r.tls_warning).lower(),
        "anchor_count": r.anchor_count if r.anchor_count >= 0 else "",
        "script_byte_ratio": f"{r.script_byte_ratio:.3f}" if r.script_byte_ratio >= 0 else "",
        "has_noscript_list": str(r.has_noscript_list).lower(),
        "lang_hint": r.lang_hint or "",
        "render_class": r.render_class,
        "error": r.error,
        "elapsed_ms": r.elapsed_ms,
    }


async def run(args):
    entries = load_entries(args.input, args.limit)
    total = len(entries)
    print(f"[probe] loaded {total} entries", file=sys.stderr)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(args.concurrency)
    robots_cache = RobotsCache()
    rate_limit = HostRateLimit(args.per_host_rps)

    results: list[ProbeResult] = []
    done_count = 0
    class_counter: Counter = Counter()

    interrupted = False

    def handle_sigint(sig, frame):
        nonlocal interrupted
        print("\n[probe] SIGINT received — saving partial results...", file=sys.stderr)
        interrupted = True

    signal.signal(signal.SIGINT, handle_sigint)

    start_wall = time.monotonic()

    csv_file = open(output_path, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
    writer.writeheader()

    try:
        async with make_client(args.timeout) as client, \
                   make_client(args.timeout, verify=False) as client_noverify, \
                   make_client(args.timeout, http2=False) as client_h1:

            tasks = [
                fetch_one(
                    entry, client, client_noverify, client_h1,
                    semaphore, robots_cache, rate_limit, args.timeout
                )
                for entry in entries
            ]

            for coro in asyncio.as_completed(tasks):
                if interrupted:
                    break
                try:
                    result = await coro
                except Exception as e:
                    # Should not happen — fetch_one catches internally
                    print(f"[probe] unexpected error: {e}", file=sys.stderr)
                    continue

                results.append(result)
                writer.writerow(result_to_row(result))
                done_count += 1
                class_counter[result.render_class] += 1

                if done_count % 100 == 0:
                    elapsed = time.monotonic() - start_wall
                    dist_str = " ".join(
                        f"{k}={v}" for k, v in sorted(class_counter.items())
                    )
                    pct = done_count * 100 // total
                    print(
                        f"[probe] {done_count}/{total} ({pct}%) — "
                        f"elapsed {elapsed:.1f}s — class dist: {dist_str}",
                        file=sys.stderr,
                    )
                    csv_file.flush()

    finally:
        csv_file.flush()
        csv_file.close()

    elapsed_total = time.monotonic() - start_wall
    print(
        f"[probe] done: {done_count}/{total} in {elapsed_total:.1f}s",
        file=sys.stderr,
    )
    print(f"[probe] render_class distribution: {dict(class_counter)}", file=sys.stderr)
    print(f"[probe] output: {output_path}", file=sys.stderr)


def main():
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
