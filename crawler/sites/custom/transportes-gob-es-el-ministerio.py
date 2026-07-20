# -*- coding: utf-8 -*-
"""Crawler for transportes.gob.es - Sala de Prensa (Ministerio de Transportes).

Listing: https://www.transportes.gob.es/ministerio/comunicacion/sala-prensa
Pagination: Drupal Views AJAX (views/ajax endpoint).
  - Page 0: standard HTML GET
  - Pages 1+: Drupal Views AJAX JSON with command="insert" containing the HTML
Detail pages: fetched for full body text and PDF attachment links.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "transportes-gob-es-el-ministerio"
_BASE_URL = "https://www.transportes.gob.es"
_LIST_URL = f"{_BASE_URL}/ministerio/comunicacion/sala-prensa"
_AJAX_URL = f"{_BASE_URL}/views/ajax"
_VIEW_NAME = "press_release_search"
_VIEW_DISPLAY = "main"
_PUBLISHER = "Ministerio de Transportes y Movilidad Sostenible"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_HTML_HEADERS = [
    "-H", f"User-Agent: {_UA}",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: es-ES,es;q=0.9,en-US;q=0.8",
    "-H", "Connection: keep-alive",
]
_AJAX_HEADERS = [
    "-H", f"User-Agent: {_UA}",
    "-H", "Accept: application/json, text/javascript, */*; q=0.01",
    "-H", "X-Requested-With: XMLHttpRequest",
    "-H", "Accept-Language: es-ES,es;q=0.9",
    "-H", f"Referer: {_LIST_URL}",
]

_BACKOFF = (1, 3, 9)
_MAX_PAGES = 200
_MIN_ABSTRACT = 50
_MAX_RUNTIME_S = 25 * 60


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

def _curl(url: str, headers: list, retries: int = 3) -> bytes | None:
    cmd = ["curl", "-sk", "--tls-max", "1.3", "--max-time", "30", "-L"]
    cmd.extend(headers)
    cmd.append(url)
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            print(
                f"[{_SITE_ID}] curl failed "
                f"(attempt {attempt + 1}/{retries}, code={result.returncode}) "
                f"for {url[:80]}"
            )
        except subprocess.TimeoutExpired:
            print(f"[{_SITE_ID}] curl timeout (attempt {attempt + 1}/{retries}) for {url[:80]}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
    return None


def _make_soup(raw: bytes | None):
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_date(dt_str: str | None) -> str | None:
    if not dt_str:
        return None
    return dt_str[:10]  # "2026-06-01T12:00:00Z" → "2026-06-01"


def _parse_listing(soup) -> list[dict]:
    """Extract item metadata dicts from a listing or AJAX-inserted page soup."""
    items = []
    for row in soup.select(".views-row"):
        try:
            a = row.find("a", href=True)
            if not a:
                continue
            href: str = a["href"]
            if "/sala-prensa/" not in href:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue

            time_el = row.find("time", attrs={"datetime": True})
            date = _parse_date(time_el["datetime"]) if time_el else None

            slug = href.rstrip("/").rsplit("/", 1)[-1]

            # Node ID from article id="node-NNNNN-..."
            article_tag = row.find("article", id=True)
            node_id = None
            if article_tag:
                m = re.search(r"node-(\d+)", article_tag["id"])
                node_id = m.group(1) if m else None

            # Teaser summary (fallback abstract)
            teaser = ""
            for sel in (".field--name-field-summary", ".field--name-body"):
                el = row.select_one(sel)
                if el:
                    teaser = el.get_text(separator=" ", strip=True)
                    if teaser:
                        break

            # Category / thematic area
            cat = None
            for sel in (".field--name-field-new-area", ".field--name-field-scope",
                        ".field--name-field-topic", ".field--name-field-category"):
                el = row.select_one(sel)
                if el:
                    cat = el.get_text(strip=True) or None
                    if cat:
                        break

            url = _BASE_URL + href if not href.startswith("http") else href
            items.append({
                "href": href,
                "url": url,
                "title": title,
                "date": date,
                "slug": slug,
                "node_id": node_id,
                "teaser": teaser,
                "category": cat,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] row parse error: {exc}")
    return items


def _extract_dom_id(raw: bytes) -> str | None:
    """Extract Drupal Views DOM ID from initial HTML (via settings JSON or regex)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        el = soup.find(attrs={"data-drupal-selector": "drupal-settings-json"})
        if el and el.string:
            settings = json.loads(el.string)
            for view_info in settings.get("views", {}).get("ajaxViews", {}).values():
                if view_info.get("view_name") == _VIEW_NAME:
                    return view_info.get("view_dom_id")
    except Exception:
        pass
    m = re.search(r'"view_dom_id"\s*:\s*"([a-f0-9]+)"', text)
    return m.group(1) if m else None


def _fetch_ajax_soup(page: int, dom_id: str):
    """Fetch AJAX page N; return parsed soup of the inserted HTML or None."""
    params = {
        "view_name": _VIEW_NAME,
        "view_display_id": _VIEW_DISPLAY,
        "view_args": "",
        "view_path": "/ministerio/comunicacion/sala-prensa",
        "view_base_path": "ministerio/comunicacion/sala-prensa",
        "view_dom_id": dom_id,
        "pager_element": "0",
        "page": str(page),
        "_drupal_ajax": "1",
    }
    url = _AJAX_URL + "?" + urllib.parse.urlencode(params)
    raw = _curl(url, _AJAX_HEADERS)
    if not raw:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
        data = json.loads(text)
        for cmd in data:
            if cmd.get("command") == "insert":
                html = cmd.get("data", "")
                if html:
                    return _make_soup(html.encode("utf-8"))
    except Exception as exc:
        print(f"[{_SITE_ID}] AJAX JSON parse error (page {page}): {exc}")
    return None


def _fetch_detail(url: str) -> tuple[str, str | None, str | None]:
    """Fetch detail page; return (abstract, pdf_url, original_filename)."""
    raw = _curl(url, _HTML_HEADERS)
    soup = _make_soup(raw)
    if not soup:
        return "", None, None

    # Concatenate all body paragraphs for the full abstract
    abstract = ""
    body = soup.select_one(".node__content")
    if body:
        paras = [p.get_text(separator=" ", strip=True) for p in body.find_all("p")]
        abstract = " ".join(p for p in paras if p)

    # First PDF attachment
    pdf_url = None
    original_filename = None
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.lower().endswith(".pdf"):
            pdf_url = href if href.startswith("http") else _BASE_URL + href
            tail = pdf_url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            try:
                original_filename = urllib.parse.unquote(tail)
            except Exception:
                original_filename = tail
            break

    return abstract, pdf_url, original_filename


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class TransportesGobEsElMinisterioCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: transportes-gob-es-el-ministerio"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        # --- Page 0: standard HTML ---
        print(f"[{_SITE_ID}] fetching page 0 from {_LIST_URL}")
        raw0 = _curl(_LIST_URL, _HTML_HEADERS)
        if not raw0:
            print(f"[{_SITE_ID}] failed to fetch initial page; aborting")
            return 0

        soup0 = _make_soup(raw0)
        if not soup0:
            print(f"[{_SITE_ID}] failed to parse initial page; aborting")
            return 0

        dom_id = _extract_dom_id(raw0)
        print(f"[{_SITE_ID}] DOM ID: {dom_id}")

        def _process(items: list[dict]) -> bool:
            """Process items; return True to continue, False when limit hit."""
            nonlocal saved
            for item in items:
                if limit is not None and saved >= limit:
                    return False

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)

                    abstract, pdf_url, original_filename = _fetch_detail(url)

                    # Fall back to listing-page teaser if detail fetch failed or short
                    if not abstract or len(abstract) < _MIN_ABSTRACT:
                        abstract = item.get("teaser", "")

                    if not abstract or len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{_SITE_ID}] skipping {url}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    slug = item["slug"]
                    node_id = item.get("node_id")

                    self._save_paper({
                        "site_id": _SITE_ID,
                        "external_id": slug,
                        "post_number": node_id or slug,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": item.get("date"),
                        "posted_date": item.get("date"),
                        "url": url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": _PUBLISHER,
                        "category": item.get("category"),
                        "metadata": json.dumps(
                            {
                                "posted_date": item.get("date"),
                                "node_id": node_id,
                                "slug": slug,
                                "originalFilename": original_filename,
                            },
                            ensure_ascii=False,
                        ),
                    })
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {item['title'][:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}; continuing")
                    continue

            return limit is None or saved < limit

        # Process page 0
        items0 = _parse_listing(soup0)
        if not _process(items0):
            print(f"[{_SITE_ID}] limit reached. total saved: {saved}")
            return saved

        # --- Pages 1+ via AJAX ---
        if not dom_id:
            print(f"[{_SITE_ID}] no DOM ID; cannot paginate via AJAX. returning {saved}")
            return saved

        for page_num in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > _MAX_RUNTIME_S:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached ({elapsed:.0f}s); stopping")
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str}")

            if page_num == _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")

            soup = _fetch_ajax_soup(page_num, dom_id)
            if soup is None:
                print(f"[{_SITE_ID}] AJAX page {page_num} returned no data; stopping")
                break

            items = _parse_listing(soup)
            if not items:
                print(f"[{_SITE_ID}] page {page_num}: 0 items — end of pagination")
                break

            # Stop if all items already seen (silent loop-back guard)
            new_items = [i for i in items if i["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] page {page_num}: all items already seen; stopping")
                break

            if not _process(items):
                break

        print(f"[{_SITE_ID}] crawl complete. total saved: {saved}")
        return saved
