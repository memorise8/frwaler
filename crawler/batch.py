# -*- coding: utf-8 -*-
"""Batch URL analysis and auto-add with failure reporting."""

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def _check_access(url, timeout=12):
    """Check if a URL is accessible. Returns (status_code, size, error, method)."""
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-o", "/dev/null",
             "-w", "%{http_code} %{size_download}", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        parts = result.stdout.strip().split()
        code = int(parts[0]) if parts else 0
        size = int(parts[1]) if len(parts) > 1 else 0
        if code == 200 and size > 500:
            return code, size, None, "requests"

        # Try cloudscraper for 403
        if code == 403 or code == 0:
            try:
                import cloudscraper
                scraper = cloudscraper.create_scraper()
                r = scraper.get(url, timeout=timeout)
                if r.status_code == 200 and len(r.text) > 5000:
                    return 200, len(r.text), None, "cloudscraper"
            except Exception:
                pass

        # Try browser for remaining failures
        if code != 200 or size < 500:
            try:
                html = _browser_fetch_html(url)
                if html and len(html) > 5000:
                    return 200, len(html), None, "browser"
            except Exception:
                pass

        if code == 403:
            return code, size, "403 Forbidden - 봇 차단 (Bot blocked)", None
        elif code == 0:
            return code, size, "연결 실패 - 타임아웃 또는 IP 차단", None
        else:
            return code, size, f"HTTP {code}", None
    except Exception as e:
        return 0, 0, f"연결 실패: {str(e)[:50]}", None


def _fetch_html(url, method=None, timeout=12):
    """Fetch HTML content. Uses method hint from access check."""
    # If cloudscraper worked in access check, use it
    if method == "cloudscraper":
        try:
            import cloudscraper
            scraper = cloudscraper.create_scraper()
            r = scraper.get(url, timeout=timeout)
            return r.text
        except Exception:
            pass

    # If browser worked in access check, use it
    if method == "browser":
        html = _browser_fetch_html(url)
        if html:
            return html

    # Default: curl
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        html = result.stdout
        # If HTML is too small or has no links, try browser fetch
        if html and len(html) < 5000:
            soup = BeautifulSoup(html, "html.parser")
            if len(soup.find_all("a", href=True)) < 3:
                browser_html = _browser_fetch_html(url)
                if browser_html and len(browser_html) > len(html):
                    return browser_html
        return html
    except Exception:
        return ""


def _browser_fetch_html(url):
    """Fetch HTML using headless browser for SPA sites."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page.goto(url, timeout=30000, wait_until="networkidle")
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
            return html
    except Exception:
        return ""


def _analyze_structure(url, html):
    """Analyze page structure. Returns dict with type, details."""
    if not html or len(html) < 500:
        return {"type": "empty", "reason": "페이지 내용 없음 - SPA(JavaScript 렌더링) 가능성"}

    soup = BeautifulSoup(html, "html.parser")

    # Count indicators
    all_links = soup.find_all("a", href=True)
    pdf_links = [a for a in all_links if ".pdf" in a.get("href", "").lower()]
    doc_links = [a for a in all_links
                 if any(ext in a.get("href", "").lower()
                        for ext in [".pdf", ".doc", ".docx", ".xlsx", ".xls", ".hwp"])]

    has_pagination = bool(
        soup.find(class_=lambda c: c and ("pager" in str(c).lower() or "pagination" in str(c).lower()))
        or soup.find("a", string=lambda s: s and "Next" in str(s))
        or soup.find("a", {"rel": "next"})
    )

    # Detect SPA (very small HTML with few links = JS-rendered)
    if len(html) < 5000 and len(all_links) < 5:
        return {
            "type": "spa",
            "reason": f"SPA/API 기반 - HTML {len(html)}B, 링크 {len(all_links)}개 (JavaScript 렌더링 필요)",
            "viable": False,
        }

    # Large HTML but no meaningful links = also likely SPA
    if len(html) > 100000 and len(all_links) < 5:
        return {
            "type": "spa",
            "reason": f"SPA 의심 - HTML {len(html)//1024}KB이지만 링크 {len(all_links)}개만 발견",
            "viable": False,
        }

    # Single-page with document links
    if len(doc_links) >= 3 and not has_pagination:
        return {
            "type": "single-page",
            "reason": f"단일 페이지에 문서 링크 {len(doc_links)}개 (PDF: {len(pdf_links)}개)",
            "viable": True,
            "doc_count": len(doc_links),
            "pdf_count": len(pdf_links),
        }

    # List-detail with pagination
    if has_pagination:
        return {
            "type": "list-detail",
            "reason": f"목록+상세 구조 (페이지네이션 있음, 링크 {len(all_links)}개)",
            "viable": True,
            "link_count": len(all_links),
        }

    # Single page with many links but no docs
    if len(all_links) >= 10:
        return {
            "type": "single-page",
            "reason": f"단일 페이지, 링크 {len(all_links)}개 (문서 링크: {len(doc_links)}개)",
            "viable": len(doc_links) >= 1 or len(all_links) >= 15,
            "link_count": len(all_links),
            "doc_count": len(doc_links),
        }

    return {
        "type": "static",
        "reason": f"정적 페이지 - 링크 {len(all_links)}개, 문서 {len(doc_links)}개",
        "viable": len(doc_links) >= 1,
    }


def _generate_site_id(url):
    """Generate a site_id from URL."""
    parsed = urlparse(url)
    domain = parsed.hostname or "unknown"
    domain = re.sub(r"^(www\.|m\.)", "", domain)
    site_id = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")
    path = parsed.path.strip("/").split("/")
    if path and path[-1]:
        hint = re.sub(r"[^a-z0-9]+", "-", path[-1].lower().split(".")[0]).strip("-")
        if hint and hint != site_id:
            site_id = f"{site_id}-{hint}"[:30]
    return site_id


def _generate_site_name(url):
    """Generate a human-readable site name from URL."""
    parsed = urlparse(url)
    domain = parsed.hostname or "unknown"
    domain = re.sub(r"^(www\.|m\.)", "", domain)
    path = parsed.path.strip("/")
    if path:
        last = path.split("/")[-1].replace("-", " ").replace("_", " ").title()
        return f"{domain} - {last}"[:60]
    return domain


def batch_analyze(urls, check_only=False, verbose=False, force_browser=False):
    """Analyze a batch of URLs and optionally run auto-add.

    Returns a report dict with results grouped by status.
    """
    report = {
        "timestamp": datetime.now().isoformat(),
        "total": len(urls),
        "results": [],
    }

    # Phase 1: Check accessibility (parallel, no cost)
    print(f"\n[Phase 1] 접근 가능 여부 확인 ({len(urls)}개 URL)...")
    with ThreadPoolExecutor(max_workers=5) as pool:
        access_results = list(pool.map(
            lambda u: (u, *_check_access(u)),
            urls,
        ))

    accessible = []
    url_methods = {}  # Track which fetch method works for each URL
    for url, code, size, error, method in access_results:
        if error:
            report["results"].append({
                "url": url,
                "site_id": _generate_site_id(url),
                "status": "ACCESS_FAILED",
                "reason": error,
                "http_code": code,
            })
            print(f"  X  [{code:>3}] {url[:70]}")
        else:
            accessible.append(url)
            url_methods[url] = method
            tag = f" [{method}]" if method != "requests" else ""
            print(f"  OK [{code:>3}] {url[:60]}{tag} ({size//1024}KB)")

    print(f"\n  접근 가능: {len(accessible)}/{len(urls)}")

    # Phase 2: Analyze structure (parallel, no cost)
    print(f"\n[Phase 2] 사이트 구조 분석 ({len(accessible)}개)...")

    def fetch_and_analyze(url):
        method = url_methods.get(url)
        html = _fetch_html(url, method=method)
        analysis = _analyze_structure(url, html)
        analysis["fetch_method"] = method
        return url, analysis

    with ThreadPoolExecutor(max_workers=5) as pool:
        structure_results = list(pool.map(fetch_and_analyze, accessible))

    viable = []
    for url, analysis in structure_results:
        site_id = _generate_site_id(url)
        is_viable = analysis.get("viable", False)
        symbol = "O" if is_viable else "X"
        print(f"  {symbol}  [{analysis['type']:<12}] {url[:55]} → {analysis['reason'][:40]}")

        if is_viable:
            viable.append(url)
        else:
            report["results"].append({
                "url": url,
                "site_id": site_id,
                "status": "STRUCTURE_FAIL",
                "site_type": analysis["type"],
                "reason": analysis["reason"],
            })

    print(f"\n  크롤링 가능: {len(viable)}/{len(accessible)}")

    if check_only:
        # Add viable as READY status
        for url in viable:
            analysis = dict(structure_results)  # won't work as dict, fix below
        for url, analysis in structure_results:
            if analysis.get("viable"):
                report["results"].append({
                    "url": url,
                    "site_id": _generate_site_id(url),
                    "status": "READY",
                    "site_type": analysis["type"],
                    "reason": analysis["reason"],
                })
        _print_report(report)
        return report

    # Phase 3: Run auto-add on viable sites (GPT cost)
    if not viable:
        print("\n크롤링 가능한 사이트가 없습니다.")
        _print_report(report)
        return report

    print(f"\n[Phase 3] auto-add 실행 ({len(viable)}개, GPT 비용 ~${len(viable) * 0.01:.2f})...")

    from .agent import AutoAddAgent

    for i, url in enumerate(viable, 1):
        site_id = _generate_site_id(url)
        site_name = _generate_site_name(url)
        print(f"\n--- [{i}/{len(viable)}] {site_id} ---")

        try:
            agent = AutoAddAgent(max_iterations=10, verbose=verbose, force_browser=force_browser)
            result = agent.run(url, site_id=site_id, site_name=site_name)

            if result.get("success"):
                report["results"].append({
                    "url": url,
                    "site_id": result.get("site_id", site_id),
                    "status": "SUCCESS",
                    "method": result.get("method", "config"),
                    "reason": result.get("reason", "")[:100],
                })
            else:
                report["results"].append({
                    "url": url,
                    "site_id": site_id,
                    "status": "AUTO_ADD_FAIL",
                    "reason": result.get("reason", "Unknown error")[:200],
                })
        except Exception as e:
            report["results"].append({
                "url": url,
                "site_id": site_id,
                "status": "ERROR",
                "reason": str(e)[:200],
            })

        # Small delay between GPT calls
        time.sleep(1)

    _print_report(report)
    _save_report(report)
    return report


def _print_report(report):
    """Print a formatted report to stdout."""
    results = report["results"]

    success = [r for r in results if r["status"] == "SUCCESS"]
    ready = [r for r in results if r["status"] == "READY"]
    failed_access = [r for r in results if r["status"] == "ACCESS_FAILED"]
    failed_structure = [r for r in results if r["status"] == "STRUCTURE_FAIL"]
    failed_auto = [r for r in results if r["status"] in ("AUTO_ADD_FAIL", "ERROR")]

    print("\n" + "=" * 70)
    print(f"  BATCH REPORT  ({report['timestamp'][:19]})")
    print(f"  총 {report['total']}개 URL 분석")
    print("=" * 70)

    if success:
        print(f"\n SUCCESS ({len(success)}개) — 크롤러 생성 완료:")
        for r in success:
            print(f"  + {r['site_id']:<25} {r.get('method', ''):<8} {r['url'][:50]}")

    if ready:
        print(f"\n READY ({len(ready)}개) — auto-add 실행 가능:")
        for r in ready:
            print(f"  ? {r['site_id']:<25} {r.get('site_type', ''):<12} {r['reason'][:40]}")

    if failed_auto:
        print(f"\n AUTO-ADD FAILED ({len(failed_auto)}개) — GPT 분석 실패:")
        for r in failed_auto:
            print(f"  X {r['site_id']:<25} {r['reason'][:50]}")

    if failed_structure:
        print(f"\n STRUCTURE FAIL ({len(failed_structure)}개) — 자동 크롤링 불가:")
        for r in failed_structure:
            print(f"  X {r['site_id']:<25} [{r.get('site_type', '?'):<6}] {r['reason'][:45]}")

    if failed_access:
        print(f"\n ACCESS FAIL ({len(failed_access)}개) — 접근 불가:")
        for r in failed_access:
            print(f"  X {r['site_id']:<25} [{r.get('http_code', '?'):>3}] {r['reason'][:45]}")

    print("=" * 70)


def _save_report(report):
    """Save report as JSON file."""
    report_dir = os.path.join(os.path.dirname(__file__), "..", "reports")
    os.makedirs(report_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(report_dir, f"batch_report_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n리포트 저장: {path}")
