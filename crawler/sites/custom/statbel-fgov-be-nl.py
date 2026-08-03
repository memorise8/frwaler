# -*- coding: utf-8 -*-
"""Crawler for Statbel (Statistics Belgium) Dutch-language news.

The Dutch listing page /nl/nieuws is protected by F5 TSPD bot-challenge
which cannot be solved from automated curl/requests.  The English listing
/en/news is NOT challenged.  Each English article page carries a
hreflang="nl" link that resolves to the matching Dutch thema URL, which
is also freely accessible.

Strategy:
  1. Paginate /en/news?page=N  (reliable, no TSPD)
  2. For each article, fetch the English detail page
       → extract hreflang="nl" → Dutch URL
  3. Fetch the Dutch URL
       → extract Dutch title / abstract / dates / PDF

All three URL types avoid TSPD; the Dutch listing /nl/nieuws is only
attempted as an optional optimisation (skip if TSPD is returned).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: bytes | str, context: str = ""):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="replace")
    else:
        text = html
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, parser)
        except Exception as exc:
            print(f"[statbel-fgov-be-nl] soup parser={parser} failed ({context}): {exc}")
    return None


def _extract_text(element) -> str:
    """Get stripped text from a BS4 element."""
    if element is None:
        return ""
    return element.get_text(separator=" ", strip=True)


def _url_slug(url: str) -> str:
    """Extract last path segment as external ID."""
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else ""


def _parse_date(dt_attr: str) -> str | None:
    """Extract YYYY-MM-DD from a datetime attribute."""
    if not dt_attr:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_attr)
    return m.group(1) if m else None


def _extract_node_id(html_text: str) -> str | None:
    """Extract Drupal node ID from page HTML."""
    m = re.search(r'(?:href|action|content)=["\'][^"\']*?/node/(\d+)', html_text)
    if m:
        return m.group(1)
    m = re.search(r'"nid"\s*:\s*"?(\d+)"?', html_text)
    if m:
        return m.group(1)
    return None


def _find_pdf_url(soup, base_url: str) -> str | None:
    """Find first PDF link in the page."""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower():
            return urljoin(base_url, href)
    return None


def _is_tspd(raw: bytes | str) -> bool:
    """Return True if this looks like an F5 TSPD bot-challenge page."""
    if isinstance(raw, bytes):
        sample = raw[:2000].decode("utf-8", errors="replace")
    else:
        sample = raw[:2000]
    return "bobcmn" in sample or "TSPD" in sample


# ---------------------------------------------------------------------------
# Listing-page parser
# ---------------------------------------------------------------------------

def _parse_listing_page(soup, page_url: str) -> list[dict]:
    """Parse a news listing (Dutch or English) → list of item dicts."""
    items = []
    rows = soup.find_all("div", class_="views-row")
    for row in rows:
        title_field = row.find("div", class_=re.compile(r"views-field-title"))
        if not title_field:
            continue
        a_tag = title_field.find("a", href=True)
        if not a_tag:
            continue
        url = a_tag["href"]
        if not url.startswith("http"):
            url = urljoin(page_url, url)
        title = _extract_text(a_tag)

        time_tag = row.find("time")
        listed_date = _parse_date(time_tag.get("datetime", "") if time_tag else "")

        theme_field = row.find("div", class_=re.compile(r"views-field-field-theme"))
        category = _extract_text(theme_field) if theme_field else None

        if url:
            items.append({
                "url": url,
                "title": title,
                "listed_date": listed_date,
                "category": category,
            })
    return items


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _extract_hreflang_nl(soup) -> str | None:
    """Return the hreflang="nl" alternate URL from the page <head>."""
    link = soup.find("link", hreflang="nl")
    if link and link.get("href"):
        return link["href"]
    return None


def _parse_detail_page(soup, url: str, html_text: str) -> dict:
    """Extract Dutch title, abstract, dates, node_id, pdf from a detail page."""
    h1 = soup.find("h1")
    title = _extract_text(h1) if h1 else ""

    time_tag = soup.find("time", datetime=True)
    published_date = _parse_date(time_tag["datetime"] if time_tag else "")

    # Full body — prefer field--name-body
    abstract = ""
    body_div = soup.find(class_=re.compile(r"field--name-body"))
    if body_div:
        for unwanted in body_div.find_all(["script", "style", "aside"]):
            unwanted.decompose()
        abstract = body_div.get_text(separator="\n", strip=True)
        for marker in ["Blijf steeds op de hoogte", "Nieuwsbrief", "Newsletter",
                       "Whatsapp", "Bluesky", "Linked In", "Subscribe"]:
            idx = abstract.find(marker)
            if idx > 100:
                abstract = abstract[:idx].strip()

    # Fallback: main content area
    if len(abstract) < 50:
        main = soup.find("main") or soup.find("article") or soup.find(id="content")
        if main:
            for unwanted in main.find_all(["nav", "header", "footer", "script", "style"]):
                unwanted.decompose()
            abstract = main.get_text(separator="\n", strip=True)[:3000].strip()

    node_id = _extract_node_id(html_text)
    pdf_url = _find_pdf_url(soup, url)
    original_filename = None
    if pdf_url:
        from urllib.parse import unquote
        original_filename = unquote(urlparse(pdf_url).path.split("/")[-1])

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "node_id": node_id,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
    }


# ---------------------------------------------------------------------------
# HTTP helper  — curl subprocess to avoid Python urllib3 JA3 TLS fingerprint
# ---------------------------------------------------------------------------

_CURL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _curl_get(cookie_jar: str | None, url: str, retries: int = 3) -> bytes | None:
    """GET url via curl; persist cookies in cookie_jar. Returns bytes or None."""
    delays = (1, 3, 9)
    cmd = [
        "curl", "-sk",
        "-A", _CURL_UA,
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: nl-BE,nl;q=0.9",
        "--max-time", "45",
        "-L",
    ]
    if cookie_jar:
        cmd += ["-b", cookie_jar, "-c", cookie_jar]
    cmd.append(url)

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            print(f"[statbel-fgov-be-nl] curl rc={result.returncode} attempt {attempt+1} for {url}")
        except Exception as exc:
            print(f"[statbel-fgov-be-nl] curl error attempt {attempt+1}/{retries} for {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class StatbelFgovBeNlCrawler(BaseCrawler):
    site_id = "statbel-fgov-be-nl"
    site_name = "Custom: statbel-fgov-be-nl"
    base_url = "https://statbel.fgov.be"

    # Primary: Dutch listing (fast if IP is clean). Fallback: English listing.
    NL_LIST_URL = "https://statbel.fgov.be/nl/nieuws"
    EN_LIST_URL = "https://statbel.fgov.be/en/news"
    PUBLISHER = "Statbel"
    RATE_SLEEP = 1.0
    MAX_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    def crawl(self, limit=None) -> int:
        """Crawl Statbel NL news with English listing fallback."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = limit if limit is not None else "∞"

        # Temp cookie jar so TSPD session cookies persist across requests
        fd, cookie_jar = tempfile.mkstemp(suffix=".txt", prefix="statbel_")
        os.close(fd)
        try:
            saved = self._crawl_pages(
                limit, seen_urls, start_time, limit_display, cookie_jar
            )
        finally:
            try:
                os.unlink(cookie_jar)
            except OSError:
                pass

        print(f"[{self.site_id}] crawl complete: saved {saved} items.")
        return saved

    def _crawl_pages(self, limit, seen_urls, start_time, limit_display,
                     cookie_jar: str) -> int:
        saved = 0
        use_nl_list = True  # Try Dutch listing first; fall back to English on TSPD

        for page in range(self.MAX_PAGES):
            if time.time() - start_time > self.MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-min budget reached at page {page}; stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            if use_nl_list:
                list_url = f"{self.NL_LIST_URL}?page={page}"
                raw = _curl_get(cookie_jar, list_url)
                if raw and not _is_tspd(raw):
                    # Dutch listing succeeded — parse directly
                    soup = _make_soup(raw, context=f"nl list {page}")
                    records = _parse_listing_page(soup, list_url) if soup else []
                    if not records:
                        print(f"[{self.site_id}] Dutch listing empty at page {page}; stopping.")
                        break
                    saved += self._save_nl_records(
                        records, seen_urls, limit, cookie_jar
                    )
                    time.sleep(self.RATE_SLEEP)
                    continue
                else:
                    print(f"[{self.site_id}] Dutch listing TSPD/failed at page {page}; "
                          "switching to English listing.")
                    use_nl_list = False

            # --- English listing path ---
            list_url = f"{self.EN_LIST_URL}?page={page}"
            raw = _curl_get(cookie_jar, list_url)
            if not raw or _is_tspd(raw):
                print(f"[{self.site_id}] English listing failed at page {page}; stopping.")
                break

            soup = _make_soup(raw, context=f"en list {page}")
            records = _parse_listing_page(soup, list_url) if soup else []
            if not records:
                print(f"[{self.site_id}] English listing empty at page {page}; stopping.")
                break

            new_records = [r for r in records
                           if r["url"] not in seen_urls and r["url"]]
            if not new_records:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping.")
                break

            for record in new_records:
                if limit is not None and saved >= limit:
                    break
                if record["url"] in seen_urls:
                    continue
                seen_urls.add(record["url"])

                try:
                    saved += self._process_en_item(cookie_jar, record)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{record['url']}' failed: {exc}")
                    continue

                time.sleep(self.RATE_SLEEP)

        return saved

    # ------------------------------------------------------------------
    # Dutch listing path (when Dutch listing is accessible)
    # ------------------------------------------------------------------

    def _save_nl_records(self, records, seen_urls, limit, cookie_jar) -> int:
        saved = 0
        for record in records:
            if limit is not None and saved >= limit:
                break
            url = record["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                saved += self._process_nl_item(cookie_jar, record)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item '{url}' failed: {exc}")
                continue

            time.sleep(self.RATE_SLEEP)
        return saved

    def _process_nl_item(self, cookie_jar: str, record: dict) -> int:
        """Fetch Dutch detail page and save. Returns 1 on success."""
        url = record["url"]
        raw = _curl_get(cookie_jar, url)
        if not raw or _is_tspd(raw):
            print(f"[{self.site_id}] Dutch detail blocked/failed: {url}")
            return 0

        html_text = raw.decode("utf-8", errors="replace")
        soup = _make_soup(raw, context=f"nl detail {url}")
        if not soup:
            return 0

        detail = _parse_detail_page(soup, url, html_text)
        return self._save_item(
            nl_url=url,
            listed_date=record.get("listed_date"),
            category=record.get("category"),
            listing_title=record.get("title", ""),
            detail=detail,
        )

    # ------------------------------------------------------------------
    # English listing path (TSPD fallback)
    # ------------------------------------------------------------------

    def _process_en_item(self, cookie_jar: str, record: dict) -> int:
        """Fetch English detail → extract Dutch URL → fetch Dutch detail → save."""
        en_url = record["url"]
        listed_date = record.get("listed_date")
        category = record.get("category")

        # Step 1: fetch English detail to get hreflang=nl URL
        en_raw = _curl_get(cookie_jar, en_url)
        if not en_raw or _is_tspd(en_raw):
            print(f"[{self.site_id}] English detail blocked/failed: {en_url}")
            return 0

        en_soup = _make_soup(en_raw, context=f"en detail {en_url}")
        if not en_soup:
            return 0

        nl_url = _extract_hreflang_nl(en_soup)
        if not nl_url:
            # No Dutch equivalent — skip
            print(f"[{self.site_id}] no hreflang=nl for {en_url}")
            return 0

        time.sleep(self.RATE_SLEEP)

        # Step 2: fetch Dutch detail
        nl_raw = _curl_get(cookie_jar, nl_url)
        if not nl_raw or _is_tspd(nl_raw):
            print(f"[{self.site_id}] Dutch detail blocked/failed: {nl_url}")
            return 0

        html_text = nl_raw.decode("utf-8", errors="replace")
        nl_soup = _make_soup(nl_raw, context=f"nl detail {nl_url}")
        if not nl_soup:
            return 0

        detail = _parse_detail_page(nl_soup, nl_url, html_text)
        return self._save_item(
            nl_url=nl_url,
            listed_date=listed_date,
            category=category,
            listing_title=record.get("title", ""),
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Common save
    # ------------------------------------------------------------------

    def _save_item(self, nl_url: str, listed_date, category,
                   listing_title: str, detail: dict) -> int:
        """Build paper dict and save. Returns 1 on success, 0 on skip."""
        title = detail.get("title") or listing_title
        abstract = detail.get("abstract", "")

        if len(abstract) < 50:
            print(f"[{self.site_id}] skipping '{title[:60]}': "
                  f"abstract too short ({len(abstract)} chars)")
            return 0

        slug = _url_slug(nl_url)
        node_id = detail.get("node_id")
        post_number = node_id if node_id else slug
        published_date = detail.get("published_date") or listed_date

        metadata: dict = {}
        if node_id:
            metadata["node_id"] = node_id
        if category:
            metadata["category_raw"] = category

        self._save_paper({
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": nl_url,
            "pdf_url": detail.get("pdf_url"),
            "original_filename": detail.get("original_filename"),
            "publisher": self.PUBLISHER,
            "category": category,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        })
        return 1
