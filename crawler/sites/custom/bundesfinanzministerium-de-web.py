# -*- coding: utf-8 -*-
"""Crawler for bundesfinanzministerium.de English brochures/publications."""

import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from crawler.base_crawler import BaseCrawler


class BundesfinanzministeriumWebCrawler(BaseCrawler):

    site_id = "bundesfinanzministerium-de-web"
    site_name = "Custom: bundesfinanzministerium-de-web"
    base_url = "https://www.bundesfinanzministerium.de"

    _LIST_URL = (
        "https://www.bundesfinanzministerium.de"
        "/Web/EN/Resources/Publications/Monthly_report/monthly_report.html"
    )
    _PAGE_PARAM = "251020_list"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT = 50

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch *url* via curl with TLS compatibility; retries with backoff."""
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
                    f"(attempt {attempt+1}/{retries}): {url}"
                )
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout (attempt {attempt+1}/{retries}): {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/{retries}): {exc}")
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
    def _iso_date(dt_str):
        """Extract YYYY-MM-DD from a datetime attribute such as '2026-05-13T11:00'."""
        if not dt_str:
            return None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(dt_str))
        return m.group(1) if m else None

    def _parse_list_page(self, html):
        """Return a list of raw item dicts extracted from a brochures list page."""
        soup = self._make_soup(html)
        if not soup:
            return []
        items = []
        for li in soup.select("li.bmf-list-entry"):
            try:
                title_a = li.select_one("a.bmf-resultlist-teaser-link")
                if not title_a:
                    continue
                title_span = title_a.select_one("span")
                title = (title_span or title_a).get_text(strip=True)
                if not title:
                    continue

                rel_url = title_a.get("href", "").strip()
                if not rel_url:
                    continue
                detail_url = (
                    rel_url if rel_url.startswith("http")
                    else f"{self.base_url}/{rel_url.lstrip('/')}"
                )

                cat_span = li.select_one("span.bmf-labelbox-text--large")
                category = cat_span.get_text(strip=True) if cat_span else None

                abstract_div = li.select_one("div.bmf-plaintext")
                abstract_list = (
                    abstract_div.get_text(separator=" ", strip=True)
                    if abstract_div else ""
                )
                # Remove trailing ellipsis left by the list-page excerpt
                abstract_list = re.sub(r"\s*[…\.]{1,3}\s*$", "", abstract_list).strip()

                time_tag = li.select_one("time")
                listed_date = self._iso_date(
                    time_tag.get("datetime") if time_tag else None
                )

                badge = li.select_one("span.bmf-badge-text")
                doc_type = badge.get_text(strip=True) if badge else None

                # Direct PDF link available on list page for "Publication" type items
                pdf_a = li.select_one("a.bmf-link--download")
                list_pdf_url = None
                if pdf_a:
                    href = pdf_a.get("href", "")
                    if "publicationFile" in href:
                        list_pdf_url = (
                            href if href.startswith("http")
                            else f"{self.base_url}{href}"
                        )

                # Derive a stable external_id from the URL slug
                path = urlparse(detail_url).path
                slug = re.sub(r"\.html?$", "", path.rstrip("/").split("/")[-1])
                external_id = slug or detail_url

                items.append({
                    "title": title,
                    "detail_url": detail_url,
                    "category": category,
                    "abstract_list": abstract_list,
                    "listed_date": listed_date,
                    "doc_type": doc_type,
                    "list_pdf_url": list_pdf_url,
                    "external_id": external_id,
                })
            except Exception as exc:
                print(f"[{self.site_id}] list-item parse error: {exc}")
        return items

    def _fetch_detail(self, url):
        """Fetch a detail page; return (abstract, pdf_url). Both may be None."""
        html = self._curl_get(url)
        if not html:
            return None, None

        # og:description is the most reliable abstract: pre-written, ~400 chars,
        # always present even when the page body is mostly tables/figures.
        abstract = None
        og_m = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{30,})["\']',
            html,
        )
        if not og_m:
            og_m = re.search(
                r'<meta[^>]+content=["\']([^"\']{30,})["\'][^>]+property=["\']og:description["\']',
                html,
            )
        if og_m:
            import html as _html_mod
            abstract = _html_mod.unescape(og_m.group(1)).strip()

        # Fallback: collect body paragraphs from the main article section
        if not abstract or len(abstract) < 80:
            soup = self._make_soup(html)
            if soup:
                main = soup.find(id="Inhalt") or soup.find("main")
                if main:
                    _SKIP_PREFIXES = (
                        "You are here", "Consent for", "Cookie", "Datenschutz",
                        "Skip to", "Jump to", "Zurück", "More on this",
                    )
                    paras = []
                    total_chars = 0
                    for p in main.find_all("p"):
                        text = p.get_text(separator=" ", strip=True)
                        if len(text) < 25:
                            continue
                        if any(text.startswith(kw) for kw in _SKIP_PREFIXES):
                            continue
                        paras.append(text)
                        total_chars += len(text)
                        if total_chars >= 2000:
                            break
                    if paras:
                        fallback = " ".join(paras).strip()
                        if len(fallback) > len(abstract or ""):
                            abstract = fallback

        # PDF: prefer EN link; fall back to any publicationFile link
        pdf_url = None
        for href_m in re.finditer(r'href=["\']([^"\']*publicationFile[^"\']*)["\']', html):
            href = href_m.group(1)
            full = href if href.startswith("http") else f"{self.base_url}{href}"
            if pdf_url is None:
                pdf_url = full
            if "/EN/" in href or "/en/" in href:
                pdf_url = full
                break

        return abstract, pdf_url

    # ------------------------------------------------------------------
    # Main crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_val = limit if limit is not None else float("inf")

        page = 1
        while True:
            # Wall-clock budget guard
            if time.time() - start_time >= self._CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached; stopping at page {page}")
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                break

            list_url = (
                self._LIST_URL
                if page == 1
                else f"{self._LIST_URL}?gtp={self._PAGE_PARAM}%253D{page}"
            )

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_val}")

            html = self._curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 items parsed; stopping")
                break

            new_on_page = 0
            for item in items:
                if saved >= limit_val:
                    break

                detail_url = item["detail_url"]
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    det_abstract, det_pdf_url = self._fetch_detail(detail_url)

                    # Best abstract: detail page first, then list page excerpt
                    abstract = (det_abstract or item["abstract_list"] or "").strip()
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] skip (abstract {len(abstract)} chars): "
                            f"{item['title'][:60]}"
                        )
                        continue

                    pdf_url = item["list_pdf_url"] or det_pdf_url

                    original_filename = None
                    if pdf_url:
                        seg = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in seg:
                            original_filename = seg

                    paper = {
                        "site_id": self.site_id,
                        "external_id": item["external_id"],
                        "post_number": item["external_id"],
                        "url": detail_url,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": item["listed_date"],
                        "posted_date": item["listed_date"],
                        "publisher": "Federal Ministry of Finance",
                        "category": item["category"],
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": item["category"],
                        "metadata": json.dumps(
                            {
                                "doc_type": item["doc_type"],
                                "posted_date": item["listed_date"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item '{item.get('title','?')[:50]}' "
                        f"failed: {exc}"
                    )

            # URL deduplication caught all items → we've looped back to start
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                break

            if saved >= limit_val:
                break

            # Check whether a next-page link exists before fetching it
            soup = self._make_soup(html)
            next_page = page + 1
            has_next = False
            if soup:
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    if f"%253D{next_page}" in href or f"={next_page}" in href:
                        has_next = True
                        break
            if not has_next:
                print(f"[{self.site_id}] no page-{next_page} link after page {page}; done")
                break

            page += 1

        print(f"[{self.site_id}] crawl done: {saved} documents saved")
        return saved
