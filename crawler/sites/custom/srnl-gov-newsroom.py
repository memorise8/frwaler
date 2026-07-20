# -*- coding: utf-8 -*-
"""Crawler for SRNL (Savannah River National Laboratory) Newsroom News Releases.

Target: https://www.srnl.gov/newsroom/news-releases/
API:    https://www.srnl.gov/wp-json/wp/v2/news-releases  (WordPress REST API)
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class SRNLGovNewsroomCrawler(BaseCrawler):
    """Crawler for SRNL Newsroom News Releases."""

    site_id = "srnl-gov-newsroom"
    site_name = "Custom: srnl-gov-newsroom"
    base_url = "https://www.srnl.gov"

    _API_BASE = "https://www.srnl.gov/wp-json/wp/v2/news-releases"
    _PER_PAGE = 10

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, timeout=30):
        """GET via curl with TLS 1.3 max workaround. Returns text or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = (attempt + 1) ** 2 * 3
                if attempt < 2:
                    print(f"[srnl-gov-newsroom] Empty response (attempt {attempt+1}/3), retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                wait = (attempt + 1) ** 2 * 3
                if attempt < 2:
                    print(f"[srnl-gov-newsroom] curl error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[srnl-gov-newsroom] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html):
        """Remove style/script blocks, strip tags, decode entities, normalise whitespace."""
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL)
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        # Decode common HTML entities
        replacements = [
            ('&amp;', '&'), ('&#038;', '&'), ('&nbsp;', ' '),
            ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'),
            ('&#8217;', "'"), ('&#8216;', "'"),
            ('&#8220;', '"'), ('&#8221;', '"'),
            ('&#8230;', '...'), ('&#8211;', '-'), ('&#8212;', '-'),
        ]
        for ent, rep in replacements:
            text = text.replace(ent, rep)
        return re.sub(r'\s+', ' ', text).strip()

    def _extract_abstract(self, html):
        """Extract readable text from the entry-content section of a detail page.

        Falls back to collecting all <p> tags if the section marker is absent.
        """
        parsers = ["html5lib", "lxml", "html.parser"]
        soup = None
        for parser in parsers:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, parser)
                break
            except Exception:
                continue

        if soup is not None:
            # Remove nav/header/footer noise
            for tag in soup.find_all(["nav", "header", "footer", "aside"]):
                tag.decompose()
            # Try entry-content div first
            content_div = soup.find(class_=re.compile(r'entry-content'))
            if content_div:
                # Remove style/script children
                for tag in content_div.find_all(["style", "script"]):
                    tag.decompose()
                text = content_div.get_text(separator=" ")
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) >= 50:
                    return text
            # Fallback: collect paragraph texts
            paras = []
            for p in soup.find_all("p"):
                t = p.get_text(separator=" ").strip()
                if len(t) >= 50:
                    paras.append(t)
            if paras:
                return " ".join(paras)

        # Last resort: regex-based extraction from entry-content section
        m = re.search(r'class="entry-content[^"]*"', html)
        if m:
            chunk = html[m.start():m.start() + 25000]
            return self._strip_html(chunk)

        # Absolute fallback: all paragraph tags
        paras = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
        texts = []
        for p in paras:
            t = self._strip_html(p)
            if len(t) >= 50:
                texts.append(t)
        return " ".join(texts)

    @staticmethod
    def _extract_pdf_url(html):
        """Return the first PDF URL found in the page, or None."""
        pdfs = re.findall(r'https?://[^\s"\'<>]+\.pdf(?:[?#][^\s"\'<>]*)?', html)
        return pdfs[0] if pdfs else None

    @staticmethod
    def _parse_date(date_str):
        """Convert '2026-02-26T13:54:31' -> '2026-02-26'."""
        if date_str and len(date_str) >= 10:
            return date_str[:10]
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl SRNL news releases via WordPress REST API + HTML detail page.

        Pagination uses ?page=N with X-WP-TotalPages from headers.
        Per-item detail is fetched from the article HTML for a clean abstract.
        """
        saved = 0
        page = 1
        seen_urls = set()
        max_pages = 200
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > 25 * 60:
                print("[srnl-gov-newsroom] 25-minute budget reached. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > max_pages:
                print(f"[srnl-gov-newsroom] Safety cap of {max_pages} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[srnl-gov-newsroom] page {page}: saved {saved}/{limit_str}")

            # Fetch listing page via WP REST API
            api_url = (
                f"{self._API_BASE}"
                f"?per_page={self._PER_PAGE}&page={page}"
                f"&_fields=id,slug,title,date,link"
            )
            time.sleep(self._delay)
            raw = self._curl_get(api_url)

            if not raw:
                print(f"[srnl-gov-newsroom] Failed to fetch page {page}. Stopping.")
                break

            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[srnl-gov-newsroom] JSON decode error at page {page}: {exc}. Stopping.")
                break

            if not isinstance(items, list):
                # WP REST API returns {"code": "rest_post_invalid_page_number"} on overflow
                print(f"[srnl-gov-newsroom] Unexpected response at page {page} (not a list). Done.")
                break

            if len(items) == 0:
                print(f"[srnl-gov-newsroom] No items on page {page}. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    post_id = str(item.get("id", ""))
                    slug = item.get("slug", "")
                    title_raw = item.get("title", {})
                    title = self._strip_html(
                        title_raw.get("rendered", "") if isinstance(title_raw, dict) else str(title_raw)
                    ).strip()
                    date_raw = item.get("date", "")
                    detail_url = item.get("link", "")

                    if not title or not detail_url:
                        print(f"[srnl-gov-newsroom] Skipping item id={post_id}: missing title or URL")
                        continue

                    # URL deduplication — prevents infinite loop if paginator wraps
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    published_date = self._parse_date(date_raw)

                    # Fetch and parse detail HTML
                    time.sleep(self._delay)
                    detail_html = self._curl_get(detail_url)

                    abstract = ""
                    pdf_url = None
                    original_filename = None

                    if detail_html:
                        abstract = self._extract_abstract(detail_html)
                        pdf_url = self._extract_pdf_url(detail_html)
                        if pdf_url:
                            # Extract filename from URL path (before any query string)
                            fname = pdf_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                            if fname and "." in fname:
                                original_filename = fname

                    # Skip items whose abstract is too short to be useful
                    if len(abstract) < 50:
                        print(
                            f"[srnl-gov-newsroom] Skipping '{title[:50]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "post_number": post_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "authors": "",
                        "publisher": "Savannah River National Laboratory",
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": "News Release",
                        "doi": "",
                        "metadata": json.dumps({
                            "posted_date": date_raw,
                            "slug": slug,
                            "post_id": post_id,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[srnl-gov-newsroom] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[srnl-gov-newsroom] item {item.get('id', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[srnl-gov-newsroom] Done. Total saved: {saved}")
        return saved
