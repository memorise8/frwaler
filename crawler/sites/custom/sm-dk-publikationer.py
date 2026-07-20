# -*- coding: utf-8 -*-
"""Crawler for sm.dk/publikationer (Social- og Boligministeriet publications).

Discovery:
  The list page uses a GoBasic CMS `itemlist` component that loads items
  via POST to `{page_url}/proxy.gba` with content-type `versus/callback`.
  The context + hash are embedded in the page HTML as a base64-encoded JSON blob.

Pagination:
  arg1 in the request body is the (1-indexed) page number.
  10 items per page. Stop when the items list is empty.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.sm.dk"
_LIST_URL = f"{_BASE}/publikationer"
_PROXY_URL = f"{_LIST_URL}/proxy.gba"
_CONTROL = "GoBasic.Presentation.Controls.ListHelper, GoBasic.Presentation"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """GET via curl with TLS-max 1.3 and retry."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.7",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[sm-dk-publikationer] GET error {url}: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[sm-dk-publikationer] GET failed after {retries}: {exc}")
    return None


def _curl_post_proxy(context_b64: str, hash_val: str, page_num: int,
                     *, timeout: int = 30, retries: int = 3) -> dict | None:
    """POST to proxy.gba and return parsed JSON, or None on failure."""
    payload = {
        "control": _CONTROL,
        "method": "GetPage",
        "path": "/publikationer",
        "query": "",
        "args": {
            "arg0": {
                "options": {
                    "generator": _CONTROL,
                    "dateRange": True,
                },
                "context": context_b64,
                "hash": hash_val,
            },
            "arg1": page_num,
            "arg2": {"categorizations": []},
            "arg3": "",
        },
    }
    body_str = json.dumps(payload, ensure_ascii=False)
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", str(timeout),
        "-X", "POST", _PROXY_URL,
        "-H", "Content-Type: versus/callback; charset=UTF-8",
        "-H", "X-Requested-With: XMLHttpRequest",
        "-H", "X-Cacheable: true",
        "-H", "Accept: application/json, text/javascript, */*; q=0.01",
        "-H", f"Referer: {_LIST_URL}",
        "-d", body_str,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if not raw.strip():
                raise ValueError("empty response")
            data = json.loads(raw)
            return data
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[sm-dk-publikationer] proxy.gba error page {page_num}: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[sm-dk-publikationer] proxy.gba failed after {retries}: {exc}")
    return None


def _extract_context_and_hash(html: str) -> tuple[str, str]:
    """Extract base64 context and hash from the itemlist script in page HTML."""
    m = re.search(
        r"application\.script\.register\('itemlist',\s*(\{.*?\"hash\":\s*\"([a-f0-9]+)\"[^}]*\})",
        html, re.DOTALL
    )
    if not m:
        return "", ""
    config_str = m.group(1)
    hash_val = m.group(2)
    ctx_m = re.search(r'"context":\s*"([A-Za-z0-9+/=]+)"', config_str)
    context_b64 = ctx_m.group(1) if ctx_m else ""
    return context_b64, hash_val


def _parse_list_html(html: str) -> list[dict]:
    """Parse proxy.gba HTML page into a list of item dicts."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[sm-dk-publikationer] list parse error: {exc}")
        return []

    items = []
    for div in soup.find_all("div", class_="item"):
        try:
            url = div.get("data-url", "").strip()
            if not url:
                continue

            title_el = div.find(class_="heading")
            title = title_el.get_text(" ", strip=True) if title_el else ""

            date_el = div.find(class_="date")
            date_raw = date_el.get_text(strip=True) if date_el else ""

            # Categories from labels
            labels = [s.get_text(strip=True) for s in div.find_all("span", class_="label")]

            # Teaser
            teaser_el = div.find("p")
            teaser = teaser_el.get_text(" ", strip=True) if teaser_el else ""

            items.append({
                "url": url,
                "title": title,
                "date_raw": date_raw,
                "labels": labels,
                "teaser": teaser,
            })
        except Exception as exc:
            print(f"[sm-dk-publikationer] item parse error: {exc}")
            continue

    return items


def _parse_date(raw: str) -> str:
    """Convert DD-MM-YYYY → YYYY-MM-DD. Returns '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d. %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _date_to_post_number(iso_date: str) -> str | None:
    """Convert YYYY-MM-DD to YYYYMMDD for sortable post_number, or None."""
    if iso_date and re.match(r"\d{4}-\d{2}-\d{2}", iso_date):
        return iso_date.replace("-", "")
    return None


def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract abstract and pdf_url from a detail page."""
    abstract = ""
    pdf_url = ""
    original_filename = None

    try:
        soup = _make_soup(html)

        # Meta description → first-choice abstract
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            abstract = meta["content"].strip()

        # Find first PDF link
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if ".pdf" in href.lower():
                if not href.startswith("http"):
                    href = _BASE + href
                pdf_url = href
                # Extract original filename from URL path
                fn = href.split("?")[0].split("/")[-1]
                try:
                    from urllib.parse import unquote
                    fn = unquote(fn)
                except Exception:
                    pass
                original_filename = fn if fn.lower().endswith(".pdf") else None
                break

        # If abstract is short, fall back to article body text
        if len(abstract) < 100:
            main = (
                soup.find("main")
                or soup.find(id="content-main")
                or soup.find("div", class_=re.compile(r"content|body|article", re.I))
                or soup.body
            )
            if main:
                for tag in main.find_all(["nav", "header", "footer", "script", "style",
                                          "form", "aside"]):
                    tag.decompose()
                parts = []
                for el in main.find_all(["p", "li", "h2", "h3", "h4"]):
                    t = el.get_text(" ", strip=True)
                    if t and len(t) > 30:
                        parts.append(t)
                if parts:
                    abstract = " ".join(parts)
                    abstract = re.sub(r"\s+", " ", abstract).strip()

    except Exception as exc:
        print(f"[sm-dk-publikationer] detail parse error ({page_url}): {exc}")
        try:
            abstract = re.sub(r"<[^>]+>", " ", html)
            abstract = re.sub(r"\s+", " ", abstract).strip()[:3000]
            m = re.search(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
            if m:
                pdf_url = m.group(1)
                if not pdf_url.startswith("http"):
                    pdf_url = _BASE + pdf_url
        except Exception:
            pass

    return {
        "abstract": abstract,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class SmDkPubikationerCrawler(BaseCrawler):
    """Crawler for sm.dk/publikationer."""

    site_id = "sm-dk-publikationer"
    site_name = "Custom: sm-dk-publikationer"
    base_url = "https://www.sm.dk"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        max_wall = 25 * 60
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        # Step 1: fetch main page to get context + hash
        print(f"[sm-dk-publikationer] Fetching main list page for context/hash...")
        main_html = _curl_get(_LIST_URL)
        if not main_html:
            print("[sm-dk-publikationer] Failed to fetch main page. Aborting.")
            return 0

        context_b64, hash_val = _extract_context_and_hash(main_html)
        if not context_b64 or not hash_val:
            print("[sm-dk-publikationer] Could not extract context/hash from page. Aborting.")
            return 0
        print(f"[sm-dk-publikationer] Got context ({len(context_b64)} chars) and hash.")

        # Step 2: paginate through list
        for page_num in range(1, max_pages + 1):
            if time.time() - crawl_start > max_wall:
                print(f"[sm-dk-publikationer] 25-minute wall clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num % 10 == 1:
                print(f"[sm-dk-publikationer] page {page_num}: saved {saved}/{limit_display}")

            data = _curl_post_proxy(context_b64, hash_val, page_num)
            if not data or not data.get("success"):
                print(f"[sm-dk-publikationer] page {page_num}: API error or empty response.")
                break

            page_html = data.get("value", {}).get("page", "")
            if not page_html:
                print(f"[sm-dk-publikationer] page {page_num}: empty page HTML. Done.")
                break

            items = _parse_list_html(page_html)
            if not items:
                print(f"[sm-dk-publikationer] page {page_num}: no items. Done.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)

                    # Fetch detail page
                    detail_html = _curl_get(item_url)
                    if detail_html:
                        detail = _parse_detail_page(detail_html, item_url)
                    else:
                        detail = {"abstract": item.get("teaser", ""),
                                  "pdf_url": "",
                                  "original_filename": None}

                    abstract = detail["abstract"] or item.get("teaser", "")
                    if len(abstract) < 50:
                        print(f"[sm-dk-publikationer] skipping (abstract <50 chars): "
                              f"{item['title'][:60]}")
                        continue

                    # Parse date
                    published_date = _parse_date(item.get("date_raw", ""))
                    listed_date = published_date  # same source

                    # post_number: date as YYYYMMDD for MAX() incremental crawl
                    post_number = _date_to_post_number(published_date)

                    # external_id: URL path slug
                    path = item_url.replace(_BASE, "").strip("/")
                    external_id = path or item_url

                    labels = item.get("labels", [])
                    category = ", ".join(labels) if labels else ""

                    pdf_url = detail.get("pdf_url", "") or ""
                    original_filename = detail.get("original_filename")

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": item_url,
                        "pdf_url": pdf_url if pdf_url else None,
                        "original_filename": original_filename,
                        "authors": None,
                        "publisher": "Social- og Boligministeriet",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": category,
                        "doi": None,
                        "metadata": json.dumps({
                            "posted_date": item.get("date_raw", ""),
                            "labels": labels,
                            "teaser": item.get("teaser", ""),
                            "list_page": page_num,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[sm-dk-publikationer] saved {saved}/{limit_display}: "
                          f"{item['title'][:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[sm-dk-publikationer] item failed ({item_url}): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[sm-dk-publikationer] page {page_num}: all items already seen. Done.")
                break

        if page_num >= max_pages:
            print(f"[sm-dk-publikationer] safety cap of {max_pages} pages reached.")

        print(f"[sm-dk-publikationer] Done. Total saved: {saved}")
        return saved
