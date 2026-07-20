# -*- coding: utf-8 -*-
"""Playwright-based HTML fetcher for Cloudflare-protected and JS-heavy sites.

Provides:
  fetch_html(url, ...) -> Optional[str]
  fetch_html_batch(urls, ...) -> dict[str, Optional[str]]
"""

import sys
import time
import threading
from typing import Optional

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CF_MARKERS = [
    "just a moment",
    "checking your browser",
    "_cf_chl_opt",
    "cf-chl-bypass",
    "cf-browser-verification",
    "enable javascript and cookies",
]


def is_cf_challenge(html: str) -> bool:
    """Return True if the HTML looks like a Cloudflare challenge page."""
    lowered = html.lower()
    return any(m in lowered for m in _CF_MARKERS)


def fetch_html(
    url: str,
    *,
    timeout_seconds: int = 30,
    wait_for_selector: Optional[str] = None,
    extra_wait_seconds: float = 2.0,
    user_agent: Optional[str] = None,
    block_resources: bool = True,
) -> Optional[str]:
    """Load URL in headless Chromium and return final rendered HTML.

    - Waits for ``networkidle`` or ``wait_for_selector`` (whichever first).
    - Detects Cloudflare "Just a moment..." challenge and waits up to
      ``timeout_seconds`` total for it to clear (polls every 2 s).
    - Blocks images/fonts/CSS by default (``block_resources=True``) for speed.
    - Returns the rendered DOM HTML string, or ``None`` on failure.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[playwright_fetcher] ERROR: playwright not installed. "
              "Run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return None

    ua = user_agent or _DEFAULT_UA
    timeout_ms = timeout_seconds * 1000

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=ua,
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page = context.new_page()

            # Block heavy resources for speed
            if block_resources:
                def _route_handler(route):
                    if route.request.resource_type in ("image", "font", "stylesheet", "media"):
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", _route_handler)

            # Navigate
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except PWTimeout:
                # networkidle timeout is common on CF-challenged pages; continue
                pass
            except Exception as exc:
                print(f"[playwright_fetcher] goto error for {url}: {exc}", file=sys.stderr)
                browser.close()
                return None

            html = page.content()

            # --- Cloudflare challenge handling ---
            if is_cf_challenge(html):
                print(f"[playwright_fetcher] CF challenge detected for {url}, waiting...",
                      file=sys.stderr)
                waited = 0.0
                max_wait = min(extra_wait_seconds * 5, 15.0)  # up to 15 s extra
                poll_interval = 2.0
                while waited < max_wait and is_cf_challenge(html):
                    time.sleep(poll_interval)
                    waited += poll_interval
                    html = page.content()
                if is_cf_challenge(html):
                    print(f"[playwright_fetcher] CF challenge NOT cleared after "
                          f"{waited:.0f}s for {url}", file=sys.stderr)
                else:
                    print(f"[playwright_fetcher] CF challenge cleared after "
                          f"{waited:.0f}s for {url}", file=sys.stderr)

            # Optional: wait for specific selector after challenge clears
            if wait_for_selector and not is_cf_challenge(html):
                try:
                    page.wait_for_selector(wait_for_selector, timeout=5000)
                    html = page.content()
                except PWTimeout:
                    pass  # selector not found; return what we have

            # Small extra settle wait
            if extra_wait_seconds > 0 and not is_cf_challenge(html):
                time.sleep(min(extra_wait_seconds, 3.0))
                html = page.content()

            browser.close()
            return html

    except Exception as exc:
        print(f"[playwright_fetcher] Unexpected error for {url}: {exc}", file=sys.stderr)
        return None


def fetch_html_stealth(
    url: str,
    *,
    timeout_seconds: int = 45,
    extra_wait_seconds: float = 5.0,
    user_agent: Optional[str] = None,
    block_resources: bool = False,
) -> Optional[str]:
    """Same as fetch_html but applies playwright_stealth.Stealth on the page
    to evade headless fingerprinting (navigator.webdriver, chrome runtime, etc.).

    Heavier (slower, larger memory) than fetch_html. Use only for sites
    where standard Playwright still hits CF challenges.

    - block_resources defaults to False: stealth mode should look like a real
      browser that loads all resource types.
    - extra_wait_seconds defaults to 5.0 (longer settle time for CF clearance).
    - timeout defaults to 45 s.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[playwright_fetcher] ERROR: playwright not installed. "
              "Run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return None

    try:
        from playwright_stealth import Stealth
    except ImportError:
        print("[playwright_fetcher] ERROR: playwright-stealth not installed. "
              "Run: pip install playwright-stealth",
              file=sys.stderr)
        return None

    ua = user_agent or _DEFAULT_UA
    timeout_ms = timeout_seconds * 1000

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=ua,
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page = context.new_page()

            # Apply stealth evasions before navigation
            Stealth(
                navigator_user_agent_override=ua,
            ).apply_stealth_sync(page)

            # Optionally block heavy resources
            if block_resources:
                def _route_handler(route):
                    if route.request.resource_type in ("image", "font", "stylesheet", "media"):
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", _route_handler)

            # Navigate
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except PWTimeout:
                pass
            except Exception as exc:
                print(f"[playwright_fetcher] stealth goto error for {url}: {exc}",
                      file=sys.stderr)
                browser.close()
                return None

            html = page.content()

            # --- Cloudflare challenge handling ---
            if is_cf_challenge(html):
                print(f"[playwright_fetcher] stealth CF challenge detected for {url}, waiting...",
                      file=sys.stderr)
                waited = 0.0
                max_wait = min(extra_wait_seconds * 5, 25.0)  # up to 25 s extra
                poll_interval = 2.0
                while waited < max_wait and is_cf_challenge(html):
                    time.sleep(poll_interval)
                    waited += poll_interval
                    html = page.content()
                if is_cf_challenge(html):
                    print(f"[playwright_fetcher] stealth CF challenge NOT cleared after "
                          f"{waited:.0f}s for {url}", file=sys.stderr)
                else:
                    print(f"[playwright_fetcher] stealth CF challenge cleared after "
                          f"{waited:.0f}s for {url}", file=sys.stderr)

            # Small extra settle wait
            if extra_wait_seconds > 0 and not is_cf_challenge(html):
                time.sleep(min(extra_wait_seconds, 5.0))
                html = page.content()

            browser.close()
            return html

    except Exception as exc:
        print(f"[playwright_fetcher] stealth unexpected error for {url}: {exc}",
              file=sys.stderr)
        return None


def fetch_html_batch(
    urls: list,
    *,
    max_concurrent: int = 4,
    **kwargs,
) -> dict:
    """Fetch multiple URLs concurrently using threading.

    Opens one Playwright browser per thread. Keeps concurrency low
    (default 4) due to Playwright memory overhead.

    Returns dict mapping url -> Optional[str].
    """
    results: dict = {}
    lock = threading.Lock()
    semaphore = threading.Semaphore(max_concurrent)

    def _worker(url: str):
        with semaphore:
            html = fetch_html(url, **kwargs)
            with lock:
                results[url] = html

    threads = [threading.Thread(target=_worker, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results
