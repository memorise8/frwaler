# -*- coding: utf-8 -*-
"""Crawler for bmftr.bund.de Publikationssuche (German federal research ministry)."""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class BmftrBundDeSiteglobalsCrawler(BaseCrawler):

    site_id = "bmftr-bund-de-siteglobals"
    site_name = "Custom: bmftr-bund-de-siteglobals"
    base_url = "https://www.bmftr.bund.de"

    _LIST_URL = (
        "https://www.bmftr.bund.de"
        "/SiteGlobals/Forms/Suche/Publikationssuche/Publikationssuche_Formular.html"
    )
    _RESULTS_PER_PAGE = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT = 50

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch *url* via curl with TLS compatibility and exponential backoff."""
        backoff = [0, 1, 3]
        for attempt in range(retries):
            if backoff[attempt]:
                time.sleep(backoff[attempt])
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "-L",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                        "--max-time", "30",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                print(
                    f"[{self.site_id}] curl exit {result.returncode} "
                    f"(attempt {attempt + 1}/{retries}): {url}"
                )
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout (attempt {attempt + 1}/{retries}): {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        return None

    def _make_soup(self, html):
        """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _de_date_to_iso(s):
        """Convert DD.MM.YYYY → YYYY-MM-DD; returns None on failure."""
        if not s:
            return None
        m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s.strip())
        if m:
            return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
        return None

    @staticmethod
    def _post_number_from_url(url):
        """Extract numeric prefix from URL filename.

        e.g. '1130008' from '.../1130008_Praxisleitfaden...html'
        Falls back to stripped filename slug if no leading digits found.
        """
        filename = url.rstrip("/").split("/")[-1]
        m = re.match(r"^(\d+)_", filename)
        if m:
            return m.group(1)
        return re.sub(r"\.html$", "", filename, flags=re.IGNORECASE)

    def _parse_list_items(self, soup):
        """Return raw item dicts from a parsed list page; dedup by URL within page."""
        seen_on_page = set()
        items = []
        for div in soup.find_all("div", class_="c-teaser-search-result"):
            try:
                link = div.find("a", class_="c-teaser-search-result__link-main")
                if not link:
                    continue
                href = link.get("href", "").strip()
                if not href:
                    continue
                url = href if href.startswith("http") else self.base_url + href
                if url in seen_on_page:
                    continue
                seen_on_page.add(url)

                h3 = div.find("h3", class_="c-teaser-search-result__headline")
                title = h3.get_text(strip=True) if h3 else ""
                if not title:
                    continue

                date_span = div.find("span", class_="is-date")
                date_raw = date_span.get_text(strip=True) if date_span else ""

                type_span = div.find("span", class_="is-type")
                category = type_span.get_text(strip=True) if type_span else ""

                text_p = div.find("p", class_="c-teaser-search-result__text")
                excerpt = text_p.get_text(separator=" ", strip=True) if text_p else ""

                pdf_a = div.find("a", class_="is-download-link")
                pdf_url_raw = pdf_a.get("href", "").strip() if pdf_a else ""
                if pdf_url_raw and not pdf_url_raw.startswith("http"):
                    pdf_url_raw = self.base_url + pdf_url_raw

                items.append({
                    "url": url,
                    "title": title,
                    "date_raw": date_raw,
                    "category": category,
                    "excerpt": excerpt,
                    "pdf_url": pdf_url_raw or None,
                })
            except Exception:
                continue
        return items

    def _fetch_detail(self, url):
        """Fetch a detail page and extract abstract, keywords, published_date.

        Returns (abstract, keywords, published_date) — any may be None.
        """
        html = self._curl_get(url)
        if not html:
            return None, None, None

        # Full abstract from <meta name="description">
        abstract = None
        for pat in (
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
        ):
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                abstract = m.group(1).strip()
                break

        # Keywords from <meta name="keywords">
        keywords = None
        for pat in (
            r'<meta[^>]+name=["\']keywords["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']keywords["\']',
        ):
            mk = re.search(pat, html, re.IGNORECASE)
            if mk:
                keywords = mk.group(1).strip()
                break

        # Published date from LD+JSON (datePublished preferred over dateModified)
        published_date = None
        for ld_str in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
        ):
            try:
                ld = json.loads(ld_str)
                date = ld.get("datePublished") or ld.get("dateModified")
                if date:
                    published_date = str(date)[:10]
                    break
            except Exception:
                pass

        return abstract, keywords, published_date

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            # Wall-clock budget guard
            if time.time() - start_time > self._CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] wall-clock budget (25 min) reached, stopping cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                break

            # Build list-page URL
            if page == 1:
                page_url = f"{self._LIST_URL}?resultsPerPage={self._RESULTS_PER_PAGE}"
            else:
                page_url = (
                    f"{self._LIST_URL}"
                    f"?gtp=1106308_list%253D{page}"
                    f"&resultsPerPage={self._RESULTS_PER_PAGE}"
                )

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            if page > 1:
                time.sleep(self._delay)

            html = self._curl_get(page_url)
            if not html:
                print(f"[{self.site_id}] failed to fetch list page {page}, stopping")
                break

            soup = self._make_soup(html)
            if not soup:
                print(f"[{self.site_id}] failed to parse list page {page}")
                break

            raw_items = self._parse_list_items(soup)
            if not raw_items:
                print(f"[{self.site_id}] no items on page {page}, stopping")
                break

            new_on_page = 0
            for item in raw_items:
                try:
                    if limit is not None and saved >= limit:
                        break

                    detail_url = item["url"]
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    title = item["title"]
                    excerpt = item["excerpt"]
                    pdf_url = item["pdf_url"]
                    date_raw = item["date_raw"]
                    category = item["category"]

                    listed_date = self._de_date_to_iso(date_raw)
                    post_number = self._post_number_from_url(detail_url)

                    original_filename = None
                    if pdf_url:
                        fn = pdf_url.split("?")[0].split("/")[-1]
                        if fn.lower().endswith(".pdf"):
                            original_filename = fn

                    # Fetch detail page for reliable abstract + keywords + published_date
                    time.sleep(self._delay)
                    det_abstract, det_keywords, det_pub_date = self._fetch_detail(detail_url)

                    abstract = det_abstract if det_abstract else excerpt
                    keywords = det_keywords
                    published_date = det_pub_date if det_pub_date else listed_date

                    # Skip items whose abstract is too short
                    if not abstract or len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] skipping '{title[:50]}'"
                            f" — abstract too short ({len(abstract or '')} chars)"
                        )
                        continue

                    metadata = {
                        "posted_date": date_raw,
                        "category_raw": category,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": post_number,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": None,
                        "publisher": "BMFTR",
                        "keywords": keywords,
                        "category": category,
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{item.get('title', '?')[:40]}' failed: {exc}; continuing")
                    continue

            # Stop if no new URLs appeared on this page (infinite-loop guard)
            if new_on_page == 0:
                print(f"[{self.site_id}] no new items on page {page}, stopping")
                break

            # Advance to next page only if the paginator shows a "next" link
            next_a = soup.find("a", class_="c-pagination__button--next")
            if not next_a or not next_a.get("href"):
                break

            page += 1

        return saved
