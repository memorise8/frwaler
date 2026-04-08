from fastapi import APIRouter
import requests as req_lib
import cloudscraper
from playwright.sync_api import sync_playwright
import time
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api", tags=["url-test"])

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def is_success(status, size):
    return status and status < 400 and size >= 500


def test_requests(url, timeout):
    session = req_lib.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    start = time.time()
    try:
        resp = session.get(url, timeout=timeout)
        content = resp.text[:500].lower()
        return {
            "method": "requests", "status": resp.status_code, "size": len(resp.content),
            "time": round(time.time() - start, 2),
            "captcha": "captcha" in content or "challenge" in content, "error": None
        }
    except Exception as e:
        return {"method": "requests", "status": None, "size": 0, "time": round(time.time() - start, 2), "captcha": False, "error": str(e)[:120]}


def test_cloudscraper(url, timeout):
    scraper = cloudscraper.create_scraper()
    start = time.time()
    try:
        resp = scraper.get(url, timeout=timeout)
        content = resp.text[:500].lower()
        return {
            "method": "cloudscraper", "status": resp.status_code, "size": len(resp.content),
            "time": round(time.time() - start, 2),
            "captcha": "captcha" in content or "challenge" in content, "error": None
        }
    except Exception as e:
        return {"method": "cloudscraper", "status": None, "size": 0, "time": round(time.time() - start, 2), "captcha": False, "error": str(e)[:120]}


def test_browser(url, timeout):
    start = time.time()
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        resp = page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        status = resp.status if resp else None
        content = page.content()
        result = {
            "method": "browser", "status": status, "size": len(content),
            "time": round(time.time() - start, 2),
            "captcha": "captcha" in content[:500].lower() or "challenge" in content[:500].lower(), "error": None
        }
        page.close()
        browser.close()
        pw.stop()
        return result
    except Exception as e:
        return {"method": "browser", "status": None, "size": 0, "time": round(time.time() - start, 2), "captcha": False, "error": str(e)[:120]}


class UrlTestRequest(BaseModel):
    url: str
    timeout: int = 15


def _run_tests(url, timeout):
    results = []
    best_method = None
    for test_fn in [test_requests, test_cloudscraper, test_browser]:
        r = test_fn(url, timeout)
        results.append(r)
        if not best_method and is_success(r["status"], r["size"]) and not r["captcha"]:
            best_method = r["method"]
    return {"url": url, "results": results, "best_method": best_method}


@router.post("/url-test")
async def url_test(req: UrlTestRequest):
    import asyncio
    results = await asyncio.get_event_loop().run_in_executor(None, lambda: _run_tests(req.url, req.timeout))
    return results
