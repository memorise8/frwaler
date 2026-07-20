# -*- coding: utf-8 -*-
"""Custom crawler for Geological Survey Ireland (GSI) publications.

Target: https://www.gsi.ie/en-ie/research/Pages/related-publications.aspx
Discovery:
  - Research page XML table  → ~10 items
  - Publications home (/en-ie/publications) nav links → ~123 items
  - Paginate up to 200 pages (safety cap)
Detail: each /en-ie/publications/Pages/*.aspx page.
"""

import json
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

_SITE_ID = "gsi-ie-en-ie"
_BASE_URL = "https://www.gsi.ie"
_LIST_URL = "https://www.gsi.ie/en-ie/research/Pages/related-publications.aspx"
_PUB_HOME_URL = "https://www.gsi.ie/en-ie/publications"
_MAX_PAGES = 200
_RATE = 1.0          # seconds between detail fetches
_BUDGET_SECS = 25 * 60  # 25 minutes wall-clock max


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curl(url, retries=3):
    """Fetch URL via curl. Returns bytes or None."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            r = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "--max-time", "30",
                    "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    url,
                ],
                capture_output=True,
                timeout=60,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception as exc:
            print(f"[{_SITE_ID}] curl attempt {attempt+1}/{retries} failed for {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


def _bs(raw):
    """Parse HTML bytes with fallback chain: html5lib → lxml → html.parser."""
    if not raw:
        return None
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _parse_date(text):
    """Extract YYYY-MM-DD from DD/MM/YYYY or ISO date strings."""
    if not text:
        return None
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', text)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        return m.group(1)
    return None


def _clean(text):
    """Strip unicode junk and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r'[​ ﻿‌‍⁠]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _filename_from_url(url):
    """Return URL-decoded filename from the last path segment."""
    if not url:
        return None
    try:
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(path.rstrip("/").split("/")[-1].split("?")[0])
        return name if name else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class GSIPublicationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: gsi-ie-en-ie"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _collect_urls_from_research_page(self):
        """Parse the XML table on the research/related-publications page."""
        urls = []
        raw = _curl(_LIST_URL)
        if not raw:
            print(f"[{_SITE_ID}] research page fetch failed")
            return urls
        soup = _bs(raw)
        if not soup:
            return urls
        box = soup.find("div", class_="box-list")
        if not box:
            return urls
        for row in box.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            link = cells[0].find("a", href=True)
            if link:
                href = link["href"]
                if not href.startswith("http"):
                    href = _BASE_URL + href
                urls.append(href)
        return urls

    def _collect_urls_from_pub_home(self):
        """Collect all /publications/Pages/*.aspx links from the publications home."""
        urls = []
        raw = _curl(_PUB_HOME_URL)
        if not raw:
            print(f"[{_SITE_ID}] publications home fetch failed")
            return urls
        soup = _bs(raw)
        if not soup:
            return urls
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/publications/Pages/" in href and ".aspx" in href:
                if not href.startswith("http"):
                    href = _BASE_URL + href
                urls.append(href)
        return urls

    def _collect_all_urls(self):
        """Merge and deduplicate publication URLs from all discovery sources."""
        seen = set()
        result = []

        def add(u):
            if u and u not in seen:
                seen.add(u)
                result.append(u)

        for u in self._collect_urls_from_research_page():
            add(u)
        time.sleep(_RATE)
        for u in self._collect_urls_from_pub_home():
            add(u)
        time.sleep(_RATE)
        return result

    # ------------------------------------------------------------------
    # Detail fetch
    # ------------------------------------------------------------------

    def _fetch_detail(self, url):
        """Fetch and parse one publication detail page. Returns dict or None."""
        raw = _curl(url)
        if not raw:
            return None

        try:
            soup = _bs(raw)
        except Exception as e:
            print(f"[{_SITE_ID}] soup error {url}: {e}")
            return None
        if not soup:
            return None

        # Title
        h1 = soup.find("h1", id="pageTitle") or soup.find("h1")
        title = _clean(h1.get_text()) if h1 else None
        if not title:
            cdiv = soup.find("div", class_="content")
            if cdiv:
                title = _clean(cdiv.get_text(" ")).split("|")[0].strip()

        # Published date — in div.content (format "29/04/2025")
        published_date = None
        cdiv = soup.find("div", class_="content")
        if cdiv:
            published_date = _parse_date(cdiv.get_text(" "))

        # Abstract — ms-rtestate-field contains the full body text
        abstract = ""
        rtediv = soup.find("div", class_="ms-rtestate-field")
        if rtediv:
            abstract = _clean(rtediv.get_text(" "))
        if not abstract:
            # Fallback: longest <p> on the page
            paras = sorted(soup.find_all("p"), key=lambda p: len(p.get_text()), reverse=True)
            if paras:
                abstract = _clean(paras[0].get_text())

        # First PDF link
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".pdf"):
                if not href.startswith("http"):
                    href = _BASE_URL + href
                pdf_url = href
                original_filename = _filename_from_url(href)
                break

        # Keywords from <meta name="keywords">
        meta_kw = soup.find("meta", attrs={"name": re.compile(r"^keywords$", re.I)})
        keywords = None
        if meta_kw:
            kw = (meta_kw.get("content") or "").strip()
            if kw:
                keywords = kw

        # external_id and post_number from URL slug
        slug = url.rstrip("/").split("/")[-1].replace(".aspx", "")
        nums = re.findall(r'\d+', slug)
        post_number = nums[0] if nums else slug

        return {
            "title": title or "(untitled)",
            "abstract": abstract,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "external_id": slug,
            "post_number": post_number,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        wall_start = time.time()
        saved = 0
        limit_str = str(limit) if limit is not None else "inf"

        print(f"[{_SITE_ID}] Collecting publication URLs …")
        pub_urls = self._collect_all_urls()
        print(f"[{_SITE_ID}] Found {len(pub_urls)} unique publication URLs")

        seen_urls = set()
        page_count = 0

        for i, url in enumerate(pub_urls):
            if limit is not None and saved >= limit:
                break

            if time.time() - wall_start > _BUDGET_SECS:
                print(f"[{_SITE_ID}] Wall-clock budget reached, stopping cleanly")
                break

            if page_count >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached, stopping")
                break

            if url in seen_urls:
                continue
            seen_urls.add(url)
            page_count += 1

            if i > 0 and i % 10 == 0:
                print(f"[{_SITE_ID}] page {page_count}: saved {saved}/{limit_str}")

            try:
                detail = self._fetch_detail(url)
                if not detail:
                    print(f"[{_SITE_ID}] item {i} ({url}): no data returned, skipping")
                    continue

                abstract = detail.get("abstract", "")
                if len(abstract) < 50:
                    print(
                        f"[{_SITE_ID}] item {i} ({url}): "
                        f"abstract too short ({len(abstract)} chars), skipping"
                    )
                    continue

                paper = {
                    "id": detail["external_id"],
                    "site_id": self.site_id,
                    "external_id": detail["external_id"],
                    "post_number": detail["post_number"],
                    "title": detail["title"],
                    "abstract": abstract,
                    "published_date": detail.get("published_date"),
                    "listed_date": detail.get("published_date"),
                    "url": url,
                    "pdf_url": detail.get("pdf_url"),
                    "original_filename": detail.get("original_filename"),
                    "keywords": detail.get("keywords"),
                    "publisher": "Geological Survey Ireland",
                    "category": "Publication",
                    "authors": None,
                    "department": None,
                    "journal": None,
                    "doi": None,
                    "metadata": json.dumps({
                        "posted_date": detail.get("published_date"),
                        "originalFilename": detail.get("original_filename"),
                        "slug": detail["external_id"],
                    }),
                }

                self._save_paper(paper)
                saved += 1
                time.sleep(_RATE)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {i} ({url}) failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done: saved {saved}/{limit_str}")
        return saved
