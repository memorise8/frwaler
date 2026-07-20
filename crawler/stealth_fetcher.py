# -*- coding: utf-8 -*-
"""Stealth fetcher with 3-layer fallback for blocked sites.

Layers (in order):
  1. curl_cffi  — Chrome TLS fingerprint impersonation (fast)
  2. playwright + playwright_stealth — full browser (JS challenge bypass)
  3. requests   — last-resort vanilla request

Public:
  StealthSession.fetch_html(url) -> (html_text|None, reason)
  classify_response(html, status) -> reason string

Design spec: docs/superpowers/specs/2026-05-29-stealth-fetcher-poc-design.md
"""
from __future__ import annotations

import random
import time
from typing import Optional

CHROME_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]
CHALLENGE_KEYWORDS = (
    "Just a moment",
    "Checking your browser",
    "cf-browser-verification",
    "Enable JavaScript and cookies to continue",
    "Attention Required! | Cloudflare",
)


def classify_response(html: Optional[str], status: int) -> str:
    """Classify HTTP response into a reason string."""
    if status in (403,):
        return "403_forbidden"
    if status in (404, 410):
        return "dead"
    if status >= 500:
        return "server_error"
    if not html:
        return "empty"
    if any(k in html for k in CHALLENGE_KEYWORDS):
        return "cloudflare_challenge"
    if status == 200 and len(html) > 1000 and "<" in html:
        return "ok"
    return "unknown"


class StealthSession:
    """Fetcher with TLS impersonation + browser fallback for blocked sites."""

    def __init__(self, timeout: int = 15, playwright_timeout: int = 45):
        self.timeout = timeout
        self.playwright_timeout = playwright_timeout
        self._browser = None
        self._context = None

    # --- Layer 1: curl_cffi (Chrome TLS impersonation) ---
    def _fetch_curl_cffi(self, url: str) -> tuple[Optional[str], int, str]:
        try:
            from curl_cffi import requests as curl_requests
            ua = random.choice(CHROME_UAS)
            r = curl_requests.get(
                url,
                impersonate="chrome131",
                timeout=self.timeout,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=True,
            )
            html = r.text if r.status_code == 200 else None
            return html, r.status_code, classify_response(html, r.status_code)
        except ImportError:
            return None, 0, "curl_cffi_not_installed"
        except Exception as e:
            return None, 0, f"curl_cffi_error:{type(e).__name__}"

    # --- Layer 2: playwright + stealth ---
    def _fetch_playwright(self, url: str) -> tuple[Optional[str], int, str]:
        try:
            from playwright.sync_api import sync_playwright
            try:
                from playwright_stealth import Stealth
                _stealth = Stealth()
                _has_stealth = True
            except ImportError:
                _stealth = None
                _has_stealth = False

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                ua = random.choice(CHROME_UAS)
                context = browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="Asia/Seoul",
                )
                page = context.new_page()
                if _has_stealth:
                    try:
                        _stealth.apply_stealth_sync(page)
                    except Exception:
                        pass
                try:
                    resp = page.goto(url, timeout=self.playwright_timeout * 1000,
                                     wait_until="domcontentloaded")
                    # wait briefly for challenges
                    page.wait_for_timeout(2000)
                    status = resp.status if resp else 0
                    html = page.content()
                    reason = classify_response(html, status)
                    return html, status, reason
                except Exception as e:
                    return None, 0, f"playwright_error:{type(e).__name__}"
                finally:
                    browser.close()
        except ImportError:
            return None, 0, "playwright_not_installed"
        except Exception as e:
            return None, 0, f"playwright_setup_error:{type(e).__name__}"

    # --- Layer 3: requests fallback ---
    def _fetch_requests(self, url: str) -> tuple[Optional[str], int, str]:
        try:
            import requests
            r = requests.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": random.choice(CHROME_UAS)},
                allow_redirects=True,
            )
            html = r.text if r.status_code == 200 else None
            return html, r.status_code, classify_response(html, r.status_code)
        except Exception as e:
            return None, 0, f"requests_error:{type(e).__name__}"

    # --- public API ---
    def fetch_html(self, url: str) -> tuple[Optional[str], dict]:
        """Attempt 3 layers in order. Return (html|None, info_dict)."""
        attempts = []

        # Layer 1
        html, status, reason = self._fetch_curl_cffi(url)
        attempts.append({"layer": "curl_cffi", "status": status, "reason": reason})
        if reason == "ok":
            return html, {"final_layer": "curl_cffi", "final_reason": "ok", "attempts": attempts}

        # Layer 2 (only if Layer 1 didn't get clean ok)
        html, status, reason = self._fetch_playwright(url)
        attempts.append({"layer": "playwright", "status": status, "reason": reason})
        if reason == "ok":
            return html, {"final_layer": "playwright", "final_reason": "ok", "attempts": attempts}

        # Layer 3 — last resort
        html, status, reason = self._fetch_requests(url)
        attempts.append({"layer": "requests", "status": status, "reason": reason})
        if reason == "ok":
            return html, {"final_layer": "requests", "final_reason": "ok", "attempts": attempts}

        # all failed — return the html from the most informative attempt
        best_html = None
        best_reason = "all_layers_failed"
        for a in attempts:
            if a["reason"] in ("cloudflare_challenge", "403_forbidden", "dead"):
                best_reason = a["reason"]
                break
        return best_html, {
            "final_layer": None,
            "final_reason": best_reason,
            "attempts": attempts,
        }
