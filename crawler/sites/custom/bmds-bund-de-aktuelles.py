# -*- coding: utf-8 -*-
"""Crawler for BMDS Pressemitteilungen (bmds.bund.de/aktuelles/pressemitteilungen).

TYPO3 tx_news-based site. Pagination uses per-page cHash tokens that must be
followed from each page's own HTML — they cannot be constructed without the
server-supplied hash.
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class BmdsBundDeAktuellesCrawler(BaseCrawler):
    """BMDS Pressemitteilungen crawler."""

    site_id = "bmds-bund-de-aktuelles"
    site_name = "Custom: bmds-bund-de-aktuelles"
    base_url = "https://bmds.bund.de"

    _LIST_URL = "https://bmds.bund.de/aktuelles/pressemitteilungen"
    _PUBLISHER = "Bundesministerium für Digitales und Staatsmodernisierung"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl with 3-attempt exponential-backoff retry.

        Returns decoded text or None on persistent failure.
        """
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.9",
            "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] Empty response (attempt {attempt + 1}), "
                          f"retrying in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl error (attempt {attempt + 1}): {exc}, "
                          f"retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """Return BeautifulSoup with html5lib → lxml → html.parser fallback."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_date(raw):
        """'07.05.2026, Pressemitteilung' → '2026-05-07'; returns '' on failure."""
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
        if m:
            d, mo, y = m.groups()
            return f"{y}-{mo}-{d}"
        return ""

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html, current_page):
        """Return (items, next_page_url).

        items — list of dicts: title, url, listed_date, category, teaser
        next_page_url — absolute URL for page current_page+1, or None
        """
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup failed on list page: {exc}")
            return [], None

        if soup is None:
            return [], None

        items = []
        for li in soup.select("li.search-result.teaser"):
            try:
                a_tag = li.select_one("a.teaser-link")
                if not a_tag:
                    continue
                title = a_tag.get("title") or a_tag.get_text(strip=True)
                detail_path = a_tag.get("href", "")
                if not detail_path:
                    continue
                url = (detail_path if detail_path.startswith("http")
                       else f"https://bmds.bund.de{detail_path}")

                date_span = li.select_one(".date-text")
                raw_date = date_span.get_text(" ", strip=True) if date_span else ""
                listed_date = self._parse_date(raw_date)

                category = "Pressemitteilung"
                if "," in raw_date:
                    category = raw_date.split(",", 1)[1].strip() or category

                teaser_p = li.select_one(".results-list-teaser p")
                teaser = teaser_p.get_text(strip=True) if teaser_p else ""

                items.append({
                    "title": title,
                    "url": url,
                    "listed_date": listed_date,
                    "category": category,
                    "teaser": teaser,
                })
            except Exception as exc:
                print(f"[{self.site_id}] List item parse error: {exc}")
                continue

        # Find next-page cHash URL — TYPO3 hashes are page-specific
        next_page_url = None
        next_page_num = current_page + 1
        for a in soup.select("a[href*='currentPage']"):
            href = a.get("href", "")
            m = re.search(r"currentPage(?:%5D|])[=](\d+)", href)
            if m and int(m.group(1)) == next_page_num:
                href_clean = href.replace("&amp;", "&")
                next_page_url = (href_clean if href_clean.startswith("http")
                                 else f"https://bmds.bund.de{href_clean}")
                break

        return items, next_page_url

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html, list_item):
        """Parse a press-release detail page; return enriched dict."""
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup failed on detail: {exc}")
            return {}

        if soup is None:
            return {}

        # Title
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else list_item.get("title", "")

        # Published date
        date_span = soup.select_one(".date-text")
        raw_date = date_span.get_text(" ", strip=True) if date_span else ""
        published_date = self._parse_date(raw_date) or list_item.get("listed_date", "")

        # Body text — primary content in .ce-bodytext
        body_div = soup.select_one(".ce-bodytext")
        abstract = ""
        pm_number = None
        if body_div:
            abstract = body_div.get_text(separator=" ", strip=True)
            abstract = re.sub(r"\s+", " ", abstract).strip()
            pm_m = re.search(r"Pressemitteilung\s+(\d+)/\d{4}", abstract)
            if pm_m:
                pm_number = pm_m.group(1)

        # Fallback: accumulate all ce-bodytext blocks
        if len(abstract) < 50:
            parts = []
            for div in soup.select(".ce-bodytext"):
                t = div.get_text(separator=" ", strip=True)
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    parts.append(t)
            abstract = " ".join(parts)

        # Final fallback to list teaser
        if len(abstract) < 50:
            abstract = list_item.get("teaser", "")

        # PDF links
        pdf_url = None
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if href.lower().endswith(".pdf"):
                pdf_url = (href if href.startswith("http")
                           else f"https://bmds.bund.de{href}")
                break

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "pm_number": pm_number,
            "pdf_url": pdf_url,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls = set()
        page_num = 1
        current_url = self._LIST_URL
        start_time = time.time()
        MAX_PAGES = 200

        while True:
            # 25-minute wall-clock budget
            if time.time() - start_time > 25 * 60:
                print(f"[{self.site_id}] 25-minute budget reached, stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached.")
                break

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            # Fetch list page
            html = self._curl_get(current_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch page {page_num}. Stopping.")
                break

            items, next_page_url = self._parse_list_page(html, page_num)

            if not items:
                print(f"[{self.site_id}] No items on page {page_num}. Done.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                slug = url.rstrip("/").rsplit("/", 1)[-1]

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(url)
                    if not detail_html:
                        print(f"[{self.site_id}] Failed to fetch detail: {url}")
                        continue

                    detail = self._parse_detail(detail_html, item)

                    title = detail.get("title") or item["title"]
                    abstract = detail.get("abstract") or item.get("teaser", "")
                    published_date = detail.get("published_date") or item["listed_date"]
                    pm_number = detail.get("pm_number")
                    pdf_url = detail.get("pdf_url")

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract too short "
                              f"({len(abstract)} chars), skipping: {title[:50]}")
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "url": url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": item["listed_date"],
                        "posted_date": item["listed_date"],
                        "category": item.get("category") or "Pressemitteilung",
                        "publisher": self._PUBLISHER,
                        "authors": "",
                        "keywords": "",
                        "doi": None,
                        "pdf_url": pdf_url,
                        "post_number": pm_number,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": item["listed_date"],
                            "pm_number": pm_number,
                            "pm_year": published_date[:4] if published_date else None,
                            "teaser": item.get("teaser", ""),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            # URL-dedup loop guard: if every item on this page was already seen, stop
            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page_num} already seen. Stopping.")
                break

            if next_page_url is None:
                print(f"[{self.site_id}] No next-page link after page {page_num}. Done.")
                break

            current_url = next_page_url
            page_num += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
