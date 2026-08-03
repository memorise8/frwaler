# -*- coding: utf-8 -*-
"""Custom crawler for IUCN International Press Releases.

Starting URL: https://iucn.org/press-releases?ptype=international

Strategy:
  1. Walk paginated list pages (?ptype=international&page=N, 10 items each).
     Each page has <time datetime="..."> elements and /press-release/... hrefs
     appearing in the same order — zip them to associate date + URL.
  2. For each detail page:
     - Title  : <meta property="og:title">
     - Abstract: field--name-field-lead div (lead paragraph) + body paragraphs
     - Date   : <time datetime> from the list page
     - Node ID: shortlink href (https://iucn.org/node/NNNNN)
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID   = "iucn-org-press-releases"
_BASE      = "https://iucn.org"
_LIST_URL  = "https://iucn.org/press-releases"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
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
# HTML parsing
# ---------------------------------------------------------------------------

def _make_soup(html: str, label: str = ""):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] parser '{parser}' failed for {label}: {exc}")
    return None


def _strip_tags(html_frag: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_frag)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# List-page parsing
# ---------------------------------------------------------------------------

def _parse_list_page(html: str) -> list[tuple[str, str]]:
    """Return list of (relative_url, iso_date) from one list page.

    IUCN renders <time datetime="..."> elements and /press-release/... hrefs
    in the same order inside the card grid.  We zip the two sequences.
    """
    soup = _make_soup(html, "list page")
    if soup is None:
        return []

    time_tags = soup.find_all("time")
    iso_dates = []
    for t in time_tags:
        dt = t.get("datetime", "")
        if dt:
            iso_dates.append(dt)

    link_tags = soup.find_all("a", href=re.compile(r"^/press-release/"))
    hrefs = []
    seen_hrefs: set[str] = set()
    for a in link_tags:
        href = a["href"]
        if href not in seen_hrefs:
            seen_hrefs.add(href)
            hrefs.append(href)

    pairs = list(zip(hrefs, iso_dates))
    return pairs


# ---------------------------------------------------------------------------
# Detail-page parsing
# ---------------------------------------------------------------------------

def _parse_detail(html: str, url: str) -> dict | None:
    """Extract title, abstract, node_id from a detail page."""
    soup = _make_soup(html, url)
    if soup is None:
        return None

    # Title
    og_title = soup.find("meta", property="og:title")
    title = og_title["content"].strip() if og_title and og_title.get("content") else ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

    # Node ID from shortlink
    node_id = ""
    shortlink = soup.find("link", rel="shortlink")
    if shortlink:
        m = re.search(r"/node/(\d+)", shortlink.get("href", ""))
        if m:
            node_id = m.group(1)

    # Abstract: field--name-field-lead (lead paragraph)
    abstract_parts: list[str] = []
    lead_div = soup.find(class_=re.compile(r"field--name-field-lead"))
    if lead_div:
        lead_text = lead_div.get_text(separator=" ", strip=True)
        if lead_text:
            abstract_parts.append(lead_text)

    # Body paragraphs in node__content (after the lead) — skip navigation noise
    node_content = soup.find(class_=re.compile(r"node__content"))
    if node_content:
        # Exclude known navigation/widget sections
        for noise in node_content.find_all(class_=re.compile(
                r"field--name-body|block-|header|footer|sharing|breadcrumb|pagination|sidebar")):
            noise.decompose()

        paras = node_content.find_all("p")
        seen_texts: set[str] = set()
        for p in paras:
            txt = p.get_text(separator=" ", strip=True)
            if len(txt) >= 50 and txt not in seen_texts:
                # Skip if it's duplicated in lead
                if not any(txt in part for part in abstract_parts):
                    abstract_parts.append(txt)
                    seen_texts.add(txt)

    abstract = "\n\n".join(abstract_parts)

    # Fallback: og:description
    if len(abstract) < 50:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            abstract = og_desc["content"].strip()

    return {
        "title": title,
        "abstract": abstract,
        "node_id": node_id,
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class IUCNPressReleasesCrawler(BaseCrawler):
    site_id   = "iucn-org-press-releases"
    site_name = "Custom: iucn-org-press-releases"
    base_url  = "https://iucn.org"

    def crawl(self, limit=None):  # noqa: C901
        saved      = 0
        page_num   = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str  = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget guard
            if time.time() - start_time > _WALL_SECS:
                print(f"[{_SITE_ID}] wall-clock budget reached, stopping cleanly.")
                break

            # Limit guard
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page_num >= _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached, stopping.")
                break

            list_url = f"{_LIST_URL}?ptype=international&page={page_num}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] failed to fetch list page {page_num}, stopping.")
                break

            pairs = _parse_list_page(raw)

            if not pairs:
                print(f"[{_SITE_ID}] page {page_num}: no items found, end of pagination.")
                break

            # Progress log every 10 pages
            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str}")

            new_on_page = 0
            for rel_url, iso_date in pairs:
                if limit is not None and saved >= limit:
                    break

                full_url = _BASE + rel_url
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                new_on_page += 1

                # Published date: ISO datetime → YYYY-MM-DD
                published_date = ""
                if iso_date:
                    m = re.match(r"(\d{4}-\d{2}-\d{2})", iso_date)
                    published_date = m.group(1) if m else iso_date[:10]

                # Slug from URL for external_id: YYYYMM/slug
                slug_m = re.search(r"/press-release/(.+)$", rel_url)
                external_id = slug_m.group(1) if slug_m else rel_url

                # Fetch detail page
                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(full_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] detail fetch failed: {full_url}")
                        continue

                    detail = _parse_detail(detail_html, full_url)
                    if detail is None:
                        print(f"[{_SITE_ID}] detail parse failed: {full_url}")
                        continue

                    title    = detail["title"] or external_id
                    abstract = detail["abstract"]
                    node_id  = detail["node_id"]

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] abstract too short (<50 chars), skipping: {full_url}")
                        continue

                    paper = {
                        "id":             None,
                        "site_id":        self.site_id,
                        "external_id":    node_id or external_id,
                        "title":          title,
                        "authors":        json.dumps([], ensure_ascii=False),
                        "abstract":       abstract,
                        "category":       "Press Release",
                        "keywords":       json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url":            full_url,
                        "pdf_url":        "",
                        "doi":            "",
                        "department":     "IUCN",
                        "metadata":       json.dumps({
                            "node_id":  node_id,
                            "slug":     external_id,
                            "ptype":    "international",
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {full_url} failed: {exc}")
                    continue

            # If zero new URLs appeared on this page (all already seen), stop
            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page_num}: all URLs already seen, stopping.")
                break

            page_num += 1

        print(f"[{_SITE_ID}] done. total saved: {saved}")
        return saved
