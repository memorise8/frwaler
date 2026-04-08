#!/usr/bin/env python3
"""Test Australian government URLs with multiple fetch methods.

Tests 41 URLs (33 timeout + 8 blocked) using three methods sequentially:
  1. requests (standard HTTP)
  2. cloudscraper (Cloudflare JS challenge bypass — does NOT bypass Akamai Bot Manager)
  3. browser (Playwright headless Chrome — full JS rendering)

Stops on first success per URL. Outputs JSON report + summary with decision tree.

Usage:
    python scripts/test_au_sites.py [timeout_seconds]
    # Default timeout: 60s
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import cloudscraper
from playwright.sync_api import sync_playwright

# --- URL Lists ---

AU_URLS_TIMEOUT = [
    ("ag.gov.au/integrity", "https://www.ag.gov.au/integrity/publications"),
    ("ag.gov.au/intl", "https://www.ag.gov.au/international-relations/publications"),
    ("ag.gov.au/families", "https://www.ag.gov.au/families-and-marriage/publications"),
    ("ag.gov.au/rights", "https://www.ag.gov.au/rights-and-protections/publications"),
    ("ag.gov.au/legal", "https://www.ag.gov.au/legal-system/publications"),
    ("ag.gov.au/crime", "https://www.ag.gov.au/crime/publications"),
    ("ag.gov.au/security", "https://www.ag.gov.au/national-security/publications"),
    ("agriculture.gov.au", "https://www.agriculture.gov.au/about/publications/list"),
    ("defence.gov.au/annual", "https://www.defence.gov.au/about/accessing-information/annual-reports"),
    ("defence.gov.au/census", "https://www.defence.gov.au/about/accessing-information/defence-census"),
    ("defence.gov.au/discipline", "https://www.defence.gov.au/about/accessing-information/defence-force-discipline-reports"),
    ("defence.gov.au/export", "https://www.defence.gov.au/about/accessing-information/export-permit-statistics"),
    ("finance.gov.au/annual", "https://www.finance.gov.au/publications/annual-report"),
    ("finance.gov.au/reports", "https://www.finance.gov.au/publications/reports"),
    ("dfat.gov.au", "https://www.dfat.gov.au/about-us/publications?page=1"),
    ("health.gov.au", "https://www.health.gov.au/resources/publications"),
    ("acma.gov.au/media", "https://www.acma.gov.au/media-releases"),
    ("acma.gov.au/pubs", "https://www.acma.gov.au/publications"),
    ("nhmrc.gov.au/pubs", "https://www.nhmrc.gov.au/about-us/publications"),
    ("nhmrc.gov.au/res", "https://www.nhmrc.gov.au/about-us/resources"),
    ("industry.gov.au/search", "https://www.industry.gov.au/search?search=pdf"),
    ("ag.gov.au/search", "https://www.ag.gov.au/search?query=pdf"),
    ("agriculture.gov.au/search", "https://www.agriculture.gov.au/search?search_api_fulltext=pdf"),
    ("defence.gov.au/search", "https://www.defence.gov.au/search?keywords=pdf&cat=all"),
    ("finance.gov.au/search", "https://www.finance.gov.au/search?search=pdf"),
    ("dfat.gov.au/search", "https://www.dfat.gov.au/search?keys=pdf"),
    ("health.gov.au/search", "https://www.health.gov.au/node/44804?query=pdf&search_scope=0"),
    ("dss.gov.au/search", "https://www.dss.gov.au/search?search=pdf"),
    ("dva.gov.au/search", "https://www.dva.gov.au/search/node?keys=pdf"),
    ("acma.gov.au/search", "https://www.acma.gov.au/search?search_api_fulltext=pdf"),
    ("afp.gov.au/search", "https://afp.gov.au/search?keys=pdf&content_type_id=All"),
    ("nhmrc.gov.au/search", "https://www.nhmrc.gov.au/search?search=pdf"),
    ("aims.gov.au/search", "https://www.aims.gov.au/search?keys=pdf"),
]

AU_URLS_BLOCKED = [
    ("aihw.gov.au/download", "https://www.aihw.gov.au/reports-data/downloadable-resources"),
    ("aihw.gov.au/latest", "https://www.aihw.gov.au/reports-data/latest-reports"),
    ("aihw.gov.au/corporate", "https://www.aihw.gov.au/reports-data/corporate-publications/reports"),
    ("aihw.gov.au/media", "https://www.aihw.gov.au/news-media/media-releases"),
    ("afp.gov.au", "https://www.afp.gov.au/news-centre"),
    ("ansto.gov.au/repo", "https://apo.ansto.gov.au/search?spc.page=1&spc.sf=dc.date.accessioned&spc.sd=DESC&f.has_content_in_original_bundle=true,equals"),
    ("pmc.gov.au/search", "https://www.pmc.gov.au/search?term=pdf"),
    ("aihw.gov.au/search", "https://www.aihw.gov.au/search?%7B%22ContentType%22:%5B%22Releases%22%5D,%22SearchText%22:%22pdf%22%7D"),
]

ALL_URLS = AU_URLS_TIMEOUT + AU_URLS_BLOCKED

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def is_success(status, size):
    """Check if response indicates real content (not empty/blocked)."""
    if not status or status >= 400:
        return False
    if size < 500:
        return False
    return True


def test_requests_method(url, timeout=60):
    """Try with plain requests."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    start = time.time()
    try:
        resp = session.get(url, timeout=timeout)
        elapsed = time.time() - start
        content = resp.text[:200].lower()
        has_captcha = "captcha" in content or "challenge" in content
        return {
            "method": "requests",
            "status": resp.status_code,
            "size": len(resp.content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "requests",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }


def test_cloudscraper_method(url, timeout=60):
    """Try with cloudscraper. NOTE: Does NOT bypass Akamai Bot Manager."""
    scraper = cloudscraper.create_scraper()
    start = time.time()
    try:
        resp = scraper.get(url, timeout=timeout)
        elapsed = time.time() - start
        content = resp.text[:200].lower()
        has_captcha = "captcha" in content or "challenge" in content
        return {
            "method": "cloudscraper",
            "status": resp.status_code,
            "size": len(resp.content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "cloudscraper",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }


def test_browser_method(url, browser, timeout=60):
    """Try with Playwright headless Chrome. Reuses existing browser instance."""
    start = time.time()
    page = browser.new_page(user_agent=USER_AGENT)
    try:
        resp = page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        status = resp.status if resp else None
        content = page.content()
        elapsed = time.time() - start
        has_captcha = "captcha" in content[:500].lower() or "challenge" in content[:500].lower()
        return {
            "method": "browser",
            "status": status,
            "size": len(content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "browser",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }
    finally:
        page.close()


def main():
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    print(f"Testing {len(ALL_URLS)} Australian URLs with {timeout}s timeout...\n")

    results = []

    # Launch ONE browser instance for all tests
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    try:
        for name, url in ALL_URLS:
            print(f"\n--- [{name}] {url} ---")
            row = {"name": name, "url": url, "results": [], "best_method": None}

            for test_fn in [test_requests_method, test_cloudscraper_method]:
                r = test_fn(url, timeout)
                row["results"].append(r)
                ok = is_success(r["status"], r["size"]) and not r["captcha"]
                icon = "OK" if ok else "FAIL"
                print(f"  {r['method']:15s} {icon:4s}  status={r['status']}  size={r['size']:>8,}  time={r['time']}s"
                      + (f"  err={r['error'][:60]}" if r["error"] else "")
                      + ("  [CAPTCHA]" if r["captcha"] else ""))
                if ok:
                    row["best_method"] = r["method"]
                    break
            else:
                # Only try browser if requests and cloudscraper both failed
                r = test_browser_method(url, browser, timeout)
                row["results"].append(r)
                ok = is_success(r["status"], r["size"]) and not r["captcha"]
                icon = "OK" if ok else "FAIL"
                print(f"  {r['method']:15s} {icon:4s}  status={r['status']}  size={r['size']:>8,}  time={r['time']}s"
                      + (f"  err={r['error'][:60]}" if r["error"] else "")
                      + ("  [CAPTCHA]" if r["captcha"] else ""))
                if ok:
                    row["best_method"] = r["method"]

            results.append(row)
    finally:
        browser.close()
        pw.stop()

    # Save JSON report
    report = {
        "timestamp": datetime.now().isoformat(),
        "timeout": timeout,
        "total": len(results),
        "results": results,
    }
    report_dir = Path(__file__).resolve().parent.parent / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"au_test_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport saved: {report_path}")

    # Summary
    methods = {"requests": [], "cloudscraper": [], "browser": [], None: []}
    for r in results:
        methods[r["best_method"]].append(r["name"])

    print(f"\n{'='*60}")
    print(f"SUMMARY ({len(results)} URLs tested, timeout={timeout}s)")
    print(f"{'='*60}")
    print(f"  Category A (requests):     {len(methods['requests'])}")
    for n in methods["requests"]:
        print(f"    - {n}")
    print(f"  Category B (cloudscraper): {len(methods['cloudscraper'])}")
    for n in methods["cloudscraper"]:
        print(f"    - {n}")
    print(f"  Category C (browser):      {len(methods['browser'])}")
    for n in methods["browser"]:
        print(f"    - {n}")
    print(f"  Category D (FAILED):       {len(methods[None])}")
    for n in methods[None]:
        print(f"    - {n}")

    # Decision tree
    failed_count = len(methods[None])
    total_tested = len(results)
    print(f"\n{'='*60}")
    print("DECISION TREE")
    print(f"{'='*60}")
    if failed_count > 20:
        print(f"  {failed_count}/{total_tested} URLs failed -- likely GEO-BLOCKED.")
        print("  >>> ACTION: These sites require an Australian proxy.")
        print("  >>> Set 'options.proxy' in site configs to an AU proxy.")
    elif failed_count > 10:
        print(f"  {failed_count}/{total_tested} URLs failed -- MIXED causes.")
        print("  >>> ACTION: Create configs for successes. Document proxy need for rest.")
    elif failed_count > 0:
        print(f"  Only {failed_count}/{total_tested} URLs failed -- proxy NOT essential.")
        print("  >>> ACTION: Focus on per-site timeout/browser/cloudscraper tuning.")
    else:
        print("  All URLs accessible! No proxy needed.")


if __name__ == "__main__":
    main()
