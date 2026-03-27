# -*- coding: utf-8 -*-
"""Tool functions for the autonomous GPT agent."""

import ast
import glob
import importlib
import json
import os
import sqlite3
import subprocess
import uuid

import requests
from bs4 import BeautifulSoup, Comment

CONFIGS_DIR = os.path.join(os.path.dirname(__file__), "sites", "configs")
CUSTOM_DIR = os.path.join(os.path.dirname(__file__), "sites", "custom")


def fetch_page(url, verify_ssl=True):
    """Fetch a web page. Returns dict with success, html, error, encoding, length."""
    import urllib3
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30, verify=verify_ssl)
        resp.raise_for_status()
        # Detect encoding from meta tag if needed
        detected_encoding = resp.apparent_encoding or resp.encoding
        html = resp.text
        return {
            "success": True,
            "html": html[:15000],
            "full_html": html,  # kept in memory, not sent to GPT
            "encoding": detected_encoding,
            "content_type": resp.headers.get("Content-Type", ""),
            "length": len(html),
            "status_code": resp.status_code,
        }
    except Exception as e:
        return {
            "success": False,
            "html": "",
            "full_html": "",
            "error": str(e)[:200],
        }


def curl_fetch(url, method="GET", data=None, headers=None):
    """Fetch via curl subprocess (bypasses Python SSL issues)."""
    cmd = ["curl", "-sk", "--tlsv1.2", "--max-time", "30"]
    if method == "POST":
        cmd.extend(["-X", "POST"])
    # Default headers
    cmd.extend([
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: ko-KR,ko;q=0.9",
    ])
    if headers:
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
    if data:
        if "Content-Type" not in (headers or {}):
            cmd.extend(["-H", "Content-Type: application/x-www-form-urlencoded"])
        cmd.extend(["-d", data])
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        body = result.stdout
        # Try to detect if response is JSON
        is_json = False
        try:
            json.loads(body)
            is_json = True
        except (json.JSONDecodeError, ValueError):
            pass
        return {
            "success": len(body) > 0,
            "body": body[:15000],
            "full_body": body,
            "is_json": is_json,
            "length": len(body),
        }
    except Exception as e:
        return {"success": False, "body": "", "full_body": "", "error": str(e)[:200]}


def cloudscraper_fetch(url):
    """Fetch a page using cloudscraper (bypasses Cloudflare/WAF). Use when
    fetch_page returns 403 Forbidden. Lighter than browser_fetch."""
    try:
        import cloudscraper
    except ImportError:
        return {"success": False, "error": "cloudscraper not installed"}

    try:
        scraper = cloudscraper.create_scraper()
        r = scraper.get(url, timeout=30)
        html = r.text
        return {
            "success": r.status_code == 200 and len(html) > 500,
            "html": html[:15000],
            "full_html": html,
            "status_code": r.status_code,
            "length": len(html),
        }
    except Exception as e:
        return {"success": False, "html": "", "full_html": "", "error": str(e)[:200]}


def browser_fetch(url, wait_seconds=3):
    """Fetch a page using headless browser (Playwright). Use when fetch_page/curl_fetch
    return empty/minimal HTML (SPA sites that need JavaScript rendering)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"success": False, "error": "Playwright not installed"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page.goto(url, timeout=30000, wait_until="networkidle")
            # Extra wait for JS rendering
            page.wait_for_timeout(int(wait_seconds * 1000))
            html = page.content()
            browser.close()

        return {
            "success": len(html) > 500,
            "html": html[:15000],
            "full_html": html,
            "length": len(html),
        }
    except Exception as e:
        return {"success": False, "html": "", "full_html": "", "error": str(e)[:200]}


def clean_html(html, max_chars=12000):
    """Strip scripts, styles, comments, nav/header/footer. Returns compact HTML for analysis."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "iframe", "link", "meta",
                               "header", "nav", "footer"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(True):
        attrs_to_keep = {}
        for attr in ["class", "id", "href", "src", "action", "name", "type", "value"]:
            if tag.has_attr(attr):
                attrs_to_keep[attr] = tag[attr]
        tag.attrs = attrs_to_keep
    cleaned = str(soup)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n<!-- truncated -->"

    # Auto-detect useful selector candidates to help GPT
    candidates = {}
    for sel in ["table tbody tr", "tbody tr", "ul li", "div.list-item", "div.item",
                "a[href]", "td a", "td.left a", "td.name a", "h3 a", "h4 a",
                "div.title a", "span.title a", ".subject a",
                # Document link selectors (for single-page detection)
                "a[href$='.pdf']", "a[href$='.doc']", "a[href$='.docx']",
                "a[href$='.xlsx']", "a[href$='.hwp']", "a[href$='.hwpx']"]:
        try:
            matches = soup.select(sel)
            if len(matches) >= 3 and len(matches) <= 200:
                sample = matches[0].get_text(strip=True)[:60]
                href_sample = ""
                if matches[0].name == "a":
                    href_sample = matches[0].get("href", "")[:80]
                candidates[sel] = {"count": len(matches), "sample": sample, "href": href_sample}
        except Exception:
            pass

    # Flag if this looks like a single-page document site
    doc_link_count = sum(
        v["count"] for k, v in candidates.items()
        if k.startswith("a[href$=")
    )
    if doc_link_count >= 3:
        candidates["__site_type_hint__"] = {
            "type": "single-page",
            "doc_links": doc_link_count,
            "note": "Many document links found directly on page. Use crawl_type: 'single-page' with the document link selector (e.g. a[href$='.pdf']) as the 'link' selector."
        }

    # Put selector_hints FIRST so it doesn't get truncated when GPT receives the result
    return {"selector_hints": candidates, "length": len(cleaned), "html": cleaned}


def test_selector(html, css_selector):
    """Test a CSS selector against HTML. Returns match count and samples."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select(css_selector)
        samples = []
        for m in matches[:3]:
            text = m.get_text(strip=True)[:100]
            href = m.get("href", "")[:100] if m.name == "a" else ""
            samples.append({"text": text, "href": href} if href else {"text": text})
        return {"matches": len(matches), "ok": len(matches) > 0, "samples": samples}
    except Exception as e:
        return {"matches": 0, "ok": False, "error": str(e)[:100]}


def extract_text(html, css_selector, limit=10):
    """Extract text content matching a CSS selector."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select(css_selector)[:limit]
        texts = []
        for m in matches:
            text = m.get_text(strip=True)[:200]
            if text:
                texts.append(text)
        return {"count": len(texts), "texts": texts}
    except Exception as e:
        return {"count": 0, "texts": [], "error": str(e)[:100]}


def save_config(config):
    """Save a crawler JSON config to the configs directory."""
    site_id = config.get("site_id")
    if not site_id:
        return {"success": False, "error": "Missing site_id"}
    # Validate required fields
    for field in ["site_name", "base_url", "crawl_type"]:
        if not config.get(field):
            return {"success": False, "error": f"Missing required field: {field}"}
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    path = os.path.join(CONFIGS_DIR, f"{site_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return {"success": True, "path": path}


def test_crawl(site_id, limit=3):
    """Test crawl a site using in-memory DB. Returns success and sample data."""
    try:
        # Use in-memory SQLite to avoid polluting production DB
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from . import db as db_module
        db_module.init_db(conn)

        # Try to load from configs
        config_path = os.path.join(CONFIGS_DIR, f"{site_id}.json")
        if os.path.exists(config_path):
            from .generic_crawler import GenericCrawler
            crawler = GenericCrawler(config_path, conn)
            db_module.register_site(conn, crawler.site_id, crawler.site_name, crawler.base_url)
        else:
            # Try custom crawlers
            custom_path = os.path.join(CUSTOM_DIR, f"{site_id}.py")
            if os.path.exists(custom_path):
                spec = importlib.util.spec_from_file_location(f"custom_{site_id}", custom_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Find the crawler class
                crawler_cls = None
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if isinstance(attr, type) and hasattr(attr, 'site_id') and hasattr(attr, 'crawl'):
                        crawler_cls = attr
                        break
                if not crawler_cls:
                    return {"success": False, "count": 0, "error": "No crawler class found in custom file"}
                crawler = crawler_cls(db_conn=conn)
                db_module.register_site(conn, crawler.site_id, crawler.site_name, crawler.base_url)
            else:
                return {"success": False, "count": 0, "error": f"No config or custom crawler found for '{site_id}'"}

        count = crawler.crawl(limit=limit)

        # Get sample data
        rows = conn.execute("SELECT title, url, abstract FROM papers LIMIT 3").fetchall()
        samples = []
        for row in rows:
            samples.append({
                "title": (row["title"] or "")[:80],
                "url": (row["url"] or "")[:100],
                "has_abstract": bool(row["abstract"] and row["abstract"].strip()),
            })
        conn.close()
        return {"success": count > 0, "count": count, "samples": samples}
    except Exception as e:
        return {"success": False, "count": 0, "error": str(e)[:200]}


def write_crawler_file(site_id, code):
    """Write a custom Python crawler file. Validates syntax before saving."""
    # Validate syntax
    try:
        ast.parse(code)
    except SyntaxError as e:
        return {"success": False, "error": f"Syntax error: {e}"}

    # Check for required patterns
    if "BaseCrawler" not in code:
        return {"success": False, "error": "Code must extend BaseCrawler"}
    if "def crawl" not in code:
        return {"success": False, "error": "Code must define a crawl() method"}

    os.makedirs(CUSTOM_DIR, exist_ok=True)
    path = os.path.join(CUSTOM_DIR, f"{site_id}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

    # Verify it can be imported
    try:
        spec = importlib.util.spec_from_file_location(f"custom_{site_id}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {"success": True, "path": path}
    except Exception as e:
        os.remove(path)
        return {"success": False, "error": f"Import failed: {e}"}


# Map of tool name -> function for the agent to dispatch
TOOL_FUNCTIONS = {
    "fetch_page": fetch_page,
    "curl_fetch": curl_fetch,
    "cloudscraper_fetch": cloudscraper_fetch,
    "browser_fetch": browser_fetch,
    "clean_html": clean_html,
    "test_selector": test_selector,
    "extract_text": extract_text,
    "save_config": save_config,
    "test_crawl": test_crawl,
    "write_crawler_file": write_crawler_file,
}
