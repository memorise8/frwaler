# -*- coding: utf-8 -*-
"""Capture network requests during page load to find data APIs."""

import json
import re

from playwright.sync_api import sync_playwright


def capture_api_requests(url, timeout=15, user_agent=None):
    """Load a page in browser and capture all XHR/fetch requests.

    Returns list of dicts: [{url, method, response_type, response_size, response_body}]
    """
    captured = []

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    ua = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    page = browser.new_page(user_agent=ua)

    def handle_response(response):
        try:
            content_type = response.headers.get("content-type", "")
            # Only capture JSON/XML API responses
            if any(t in content_type for t in ["json", "xml", "javascript"]):
                body = None
                try:
                    body = response.text()
                except Exception:
                    pass
                captured.append({
                    "url": response.url,
                    "method": response.request.method,
                    "status": response.status,
                    "content_type": content_type,
                    "size": len(body) if body else 0,
                    "body": body[:5000] if body else None,  # Limit body size
                })
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
    except Exception:
        pass
    finally:
        page.close()
        browser.close()
        pw.stop()

    return captured


def find_data_api(url, min_items=3):
    """Analyze captured requests to find the most likely data API.

    Returns dict with api_url, method, sample_data or None.
    """
    captured = capture_api_requests(url)

    candidates = []
    for req in captured:
        if req["status"] != 200 or not req["body"]:
            continue

        body = req["body"]
        score = 0

        # Try parsing as JSON
        try:
            data = json.loads(body)
            # Look for array responses (lists of items)
            if isinstance(data, list) and len(data) >= min_items:
                score = len(data) * 10
            elif isinstance(data, dict):
                # Look for common patterns: {items: [...], data: [...], list: [...], result: [...]}
                for key in ["items", "data", "list", "result", "results", "rows",
                            "content", "records", "body", "resultList", "bbsList",
                            "nttList", "boardList", "dataList"]:
                    if key in data and isinstance(data[key], list) and len(data[key]) >= min_items:
                        score = len(data[key]) * 10
                        break
                # Also check nested: {response: {body: {items: [...]}}}
                for v in data.values():
                    if isinstance(v, dict):
                        for k2, v2 in v.items():
                            if isinstance(v2, list) and len(v2) >= min_items:
                                score = max(score, len(v2) * 5)

            if score > 0:
                candidates.append({
                    "url": req["url"],
                    "method": req["method"],
                    "content_type": req["content_type"],
                    "score": score,
                    "sample": body[:2000],
                })
        except (json.JSONDecodeError, TypeError):
            # Check for XML with multiple items
            item_count = len(re.findall(r"<item[ >]|<entry[ >]|<record[ >]", body, re.I))
            if item_count >= min_items:
                candidates.append({
                    "url": req["url"],
                    "method": req["method"],
                    "content_type": req["content_type"],
                    "score": item_count * 8,
                    "sample": body[:2000],
                })

    if not candidates:
        return None

    # Return highest scoring candidate
    best = max(candidates, key=lambda c: c["score"])
    return best
