# -*- coding: utf-8 -*-
"""Custom crawler for Insight Centre Press Releases.

Starting URL: https://www.insight-centre.org/press-releases/

Strategy:
  1. Fetch the single listing page (no pagination — all ~63 press releases
     are embedded as <a> links inside <p> tags within the
     content-content_editor div, grouped by <h5> year headings).
  2. For each detail page:
     - Title      : <meta property="og:title">
     - Date       : Yoast JSON-LD schema datePublished
     - Post ID    : body class page-id-NNN
     - Abstract   : text extracted from .inner_content_editor div paragraphs
     - External ID: slug from URL path
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID   = "insight-centre-org-press-releases"
_BASE      = "https://www.insight-centre.org"
_LIST_URL  = "https://www.insight-centre.org/press-releases/"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))   # safety cap (only 1 list page currently, kept for future)
_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3):
    # Normalise http:// → https://
    url = re.sub(r'^http://', 'https://', url)
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
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
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML / BeautifulSoup helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str, label: str = ""):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] parser '{parser}' failed for {label}: {exc}")
    return None


def _html_to_text(html_frag: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html_frag, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#8211;", "–", text)
    text = re.sub(r"&#8216;|&#8217;", "'", text)
    text = re.sub(r"&#8220;|&#8221;", '"', text)
    text = re.sub(r"&[a-zA-Z#\d]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# List-page parsing
# ---------------------------------------------------------------------------

def _parse_list_page(html: str):
    """Return list of (url, link_text) from the listing page.

    The press releases listing embeds all items directly in the page HTML
    as <a> links inside <p> tags within the .content-content_editor div.
    Year grouping is done via <h5> headings but dates come from detail pages.
    """
    soup = _make_soup(html, "list page")
    if soup is None:
        return []

    # Find the content editor section that holds the press release links
    section = None
    for div in soup.find_all("div", class_=re.compile(r"content-content_editor")):
        # Check it contains press-release links
        if div.find("a", href=re.compile(r"insight-centre\.org")):
            section = div
            break

    if section is None:
        # Fallback: search the whole page for links matching press-release pattern
        section = soup

    items = []
    seen_hrefs: set = set()

    for p_tag in section.find_all("p"):
        for a_tag in p_tag.find_all("a", href=True):
            href = a_tag["href"].strip()
            # Normalise http → https
            href = re.sub(r'^http://', 'https://', href)
            # Skip: non-insight links, fragment-only, preview/draft URLs
            if "insight-centre.org" not in href:
                continue
            if "?page_id" in href and "preview=true" in href:
                continue
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            link_text = a_tag.get_text(separator=" ", strip=True)
            items.append((href, link_text))

    return items


# ---------------------------------------------------------------------------
# Detail-page parsing
# ---------------------------------------------------------------------------

def _parse_detail(html: str, url: str):
    """Extract title, abstract, date, post_number from a detail page."""
    # --- Title via og:title ---
    title = ""
    og_title = re.search(r'property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html)
    if not og_title:
        og_title = re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:title["\']', html)
    if og_title:
        title = og_title.group(1).strip()
        # Strip site name suffix " - Insight"
        title = re.sub(r'\s*[-–|]\s*Insight\s*$', '', title).strip()

    # --- Post ID (WordPress page-id from body class) ---
    post_number = None
    body_class = re.search(r'<body[^>]*class=["\']([^"\']+)["\']', html)
    if body_class:
        m = re.search(r'page-id-(\d+)', body_class.group(1))
        if m:
            post_number = m.group(1)

    # --- Published date from Yoast JSON-LD schema ---
    published_date = ""
    schema_match = re.search(
        r'class=["\']yoast-schema-graph["\'][^>]*>(.*?)</script>', html, re.DOTALL
    )
    if schema_match:
        try:
            schema = json.loads(schema_match.group(1))
            for item in schema.get("@graph", []):
                if item.get("@type") == "WebPage":
                    dp = item.get("datePublished", "")
                    if dp:
                        m = re.match(r"(\d{4}-\d{2}-\d{2})", dp)
                        published_date = m.group(1) if m else dp[:10]
                    break
        except Exception:
            pass

    # --- Abstract from inner_content_editor div ---
    abstract = ""
    soup = _make_soup(html, url)
    if soup:
        # Find the content editor div (not the CSS inline style reference)
        editor_div = None
        for div in soup.find_all("div", class_=re.compile(r"inner_content_editor")):
            # Must contain actual paragraph text content
            if div.find("p"):
                editor_div = div
                break

        if editor_div:
            # Remove navigation noise: contact blocks, footers, mailto links, etc.
            for noise in editor_div.find_all(["script", "style", "nav"]):
                noise.decompose()

            paras = []
            seen_texts: set = set()
            for p in editor_div.find_all("p"):
                txt = p.get_text(separator=" ", strip=True)
                # Clean up HTML entities
                txt = re.sub(r"&[a-zA-Z#\d]+;", "", txt)
                txt = re.sub(r"\s+", " ", txt).strip()
                # Skip very short, nav-like, or email/phone paragraphs
                if len(txt) < 30:
                    continue
                if re.match(r'^[+\d\s\-()]{5,20}$', txt):
                    continue
                if "@" in txt and len(txt) < 60:
                    continue
                if txt not in seen_texts:
                    seen_texts.add(txt)
                    paras.append(txt)

            abstract = "\n\n".join(paras)

    # Fallback: og:description
    if len(abstract) < 50:
        og_desc = re.search(r'property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']', html)
        if not og_desc:
            og_desc = re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:description["\']', html)
        if og_desc:
            abstract = og_desc.group(1).strip()

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "post_number": post_number,
    }


# ---------------------------------------------------------------------------
# Slug extraction
# ---------------------------------------------------------------------------

def _slug_from_url(url: str) -> str:
    """Extract the last meaningful path segment as external_id."""
    path = url.rstrip("/")
    # Remove query params
    path = path.split("?")[0]
    slug = path.rsplit("/", 1)[-1]
    return slug or path


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class InsightCentreOrgPressReleasesCrawler(BaseCrawler):
    site_id   = "insight-centre-org-press-releases"
    site_name = "Custom: insight-centre-org-press-releases"
    base_url  = "https://www.insight-centre.org"

    def crawl(self, limit=None):  # noqa: C901
        saved      = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str  = str(limit) if limit is not None else "∞"

        # ------------------------------------------------------------------
        # Step 1: fetch the single listing page
        # ------------------------------------------------------------------
        print(f"[{_SITE_ID}] fetching listing page: {_LIST_URL}")
        raw = _curl_get(_LIST_URL)
        if not raw:
            print(f"[{_SITE_ID}] failed to fetch listing page, aborting.")
            return 0

        items = _parse_list_page(raw)
        if not items:
            print(f"[{_SITE_ID}] no items found on listing page, aborting.")
            return 0

        print(f"[{_SITE_ID}] found {len(items)} press release links on listing page")

        # ------------------------------------------------------------------
        # Step 2: fetch each detail page
        # ------------------------------------------------------------------
        for idx, (url, link_text) in enumerate(items):
            # Wall-clock budget guard
            if time.time() - start_time > _WALL_SECS:
                print(f"[{_SITE_ID}] wall-clock budget reached, stopping cleanly.")
                break

            # Limit guard
            if limit is not None and saved >= limit:
                break

            # Deduplicate
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Progress log every 10 items
            if idx % 10 == 0:
                print(f"[{_SITE_ID}] item {idx}: saved {saved}/{limit_str}")

            try:
                time.sleep(self._delay)
                detail_html = _curl_get(url)
                if not detail_html:
                    print(f"[{_SITE_ID}] detail fetch failed: {url}")
                    continue

                detail = _parse_detail(detail_html, url)
                if detail is None:
                    print(f"[{_SITE_ID}] detail parse failed: {url}")
                    continue

                title         = detail["title"]
                abstract      = detail["abstract"]
                published_date = detail["published_date"]
                post_number   = detail["post_number"]

                # Fallback title from link text (strip date prefix)
                if not title:
                    t = re.sub(r"^[\d\w\s,]+[–\-:]\s*", "", link_text).strip()
                    title = t or link_text

                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] abstract too short (<50 chars), skipping: {url}")
                    continue

                slug = _slug_from_url(url)
                external_id = post_number or slug

                paper = {
                    "id":              None,
                    "site_id":         self.site_id,
                    "external_id":     external_id,
                    "post_number":     post_number,
                    "title":           title,
                    "abstract":        abstract,
                    "published_date":  published_date,
                    "listed_date":     published_date,
                    "url":             url,
                    "pdf_url":         None,
                    "authors":         None,
                    "publisher":       "Insight Centre for Data Analytics",
                    "department":      None,
                    "journal":         None,
                    "keywords":        None,
                    "category":        "Press Release",
                    "doi":             None,
                    "original_filename": None,
                    "metadata":        json.dumps({
                        "slug":       slug,
                        "page_id":    post_number,
                        "link_text":  link_text,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {url} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] done. total saved: {saved}")
        return saved
