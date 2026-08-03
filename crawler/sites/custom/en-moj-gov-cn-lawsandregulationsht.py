# -*- coding: utf-8 -*-
"""Ministry of Justice PRC (English) - Laws and Regulations crawler.

Starting URL: http://en.moj.gov.cn/lawsandregulations.html
Pagination: lawsandregulations.html (p1), lawsandregulations_2.html, …
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "en-moj-gov-cn-lawsandregulationsht"
_BASE_URL = "http://en.moj.gov.cn"
_LIST_PAGE_1 = "http://en.moj.gov.cn/lawsandregulations.html"
_LIST_PAGE_N = "http://en.moj.gov.cn/lawsandregulations_{n}.html"
_ABSTRACT_MIN_CHARS = 100
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_CLOCK_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


class EnMojGovCnLawsRegsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: en-moj-gov-cn-lawsandregulationsht"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with up to 3 retries (1s, 3s, 9s backoff)."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url.strip(),
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/3): {exc}")
            wait = [1, 3, 9][attempt]
            if attempt < 2:
                print(f"[{_SITE_ID}] retrying in {wait}s…")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&#\d+;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _get_total_pages(self, html: str) -> int:
        m = re.search(r'totalcount="(\d+)"', html, re.IGNORECASE)
        return int(m.group(1)) if m else 1

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return [{title, url, date, list_abstract}, …] from one list page."""
        # Strip scripts/styles before parsing to avoid embedded JS noise
        clean = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
        clean = re.sub(r"<style[^>]*>.*?</style>", " ", clean, flags=re.DOTALL)

        try:
            soup = self._make_soup(clean)
        except Exception:
            soup = None

        if not soup:
            return []

        cont = soup.find("div", class_=re.compile(r"mr950_cont"))
        if not cont:
            return []

        items: list[dict] = []
        for box in cont.find_all("div", class_="lis_box"):
            try:
                h3 = box.find("h3")
                if not h3:
                    continue
                a_tag = h3.find("a")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                href = (a_tag.get("href") or "").strip()
                if not href or not title:
                    continue
                # Skip the self-referential section header
                if "lawsandregulations.html" in href:
                    continue

                # Resolve full URL
                if href.startswith("http"):
                    full_url = href
                elif href.startswith("//"):
                    full_url = "http:" + href
                else:
                    full_url = _BASE_URL + "/" + href.lstrip("/")

                date_tag = box.find("b")
                date_str = date_tag.get_text(strip=True) if date_tag else ""

                p_tag = box.find("p", class_="pc_box")
                list_abstract = p_tag.get_text(strip=True) if p_tag else ""

                items.append({
                    "title": title,
                    "url": full_url,
                    "date": date_str,
                    "list_abstract": list_abstract,
                })
            except Exception:
                continue

        return items

    # ------------------------------------------------------------------
    # Detail-page fetch
    # ------------------------------------------------------------------

    def _fetch_detail_text(self, url: str) -> str:
        """Fetch an HTML detail page and return stripped article body text."""
        html = self._curl_get(url)
        if not html:
            return ""

        html2 = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
        html2 = re.sub(r"<style[^>]*>.*?</style>", " ", html2, flags=re.DOTALL)

        # Try known content div classes in preference order
        for cls in ("art_info", "artinfo_box", "art_box", "l_865"):
            for quote in ('"', "'"):
                idx = html2.find(f"class={quote}{cls}{quote}")
                if idx != -1:
                    chunk = html2[idx: idx + 15000]
                    text = self._strip_html(chunk)
                    if len(text) >= _ABSTRACT_MIN_CHARS:
                        return text

        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        # Fetch page 1 first to learn total page count
        raw_p1 = self._curl_get(_LIST_PAGE_1)
        if not raw_p1:
            print(f"[{_SITE_ID}] Failed to fetch first page. Exiting.")
            return 0

        total_pages = min(self._get_total_pages(raw_p1), _MAX_PAGES)
        print(f"[{_SITE_ID}] Total list pages: {total_pages}")

        for page_num in range(1, total_pages + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _WALL_CLOCK_SECS:
                print(f"[{_SITE_ID}] Wall-clock budget reached at page {page_num}. Stopping.")
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_label}")

            if page_num == 1:
                raw = raw_p1
            else:
                time.sleep(self._delay)
                raw = self._curl_get(_LIST_PAGE_N.format(n=page_num))

            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch page {page_num}. Skipping.")
                continue

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page_num}. Done.")
                break

            # Infinite-loop guard: stop if every URL on this page was already seen
            if all(it["url"] in seen_urls for it in items):
                print(f"[{_SITE_ID}] All URLs on page {page_num} already seen. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    title = item["title"]
                    date_str = item["date"]
                    is_pdf = url.lower().split("?")[0].endswith(".pdf")

                    if is_pdf:
                        # PDF-only items rarely carry an inline abstract;
                        # if the list abstract is long enough, use it
                        abstract = item["list_abstract"]
                        pdf_url = url
                        if len(abstract) < _ABSTRACT_MIN_CHARS:
                            print(f"[{_SITE_ID}] skip PDF-only (no abstract): {title[:70]}")
                            continue
                    else:
                        time.sleep(self._delay)
                        abstract = self._fetch_detail_text(url)
                        pdf_url = None
                        if len(abstract) < _ABSTRACT_MIN_CHARS:
                            print(
                                f"[{_SITE_ID}] abstract too short "
                                f"({len(abstract)} chars): {title[:70]}"
                            )
                            continue

                    # Build external_id / post_number
                    m_id = re.search(r"c_(\d+)\.htm", url)
                    if m_id:
                        external_id = m_id.group(1)
                        post_number = m_id.group(1)
                    else:
                        tail = url.rstrip("/").split("/")[-1].split("?")[0]
                        external_id = re.sub(r"[^A-Za-z0-9._-]", "_", tail)[:200]
                        post_number = None

                    original_filename = None
                    if is_pdf:
                        original_filename = url.rstrip("/").split("/")[-1]

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "url": url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "published_date": date_str or None,
                        "posted_date": date_str or None,
                        "authors": None,
                        "publisher": (
                            "Ministry of Justice of the People's Republic of China"
                        ),
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "Laws and Regulations",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_str,
                                "list_abstract": item["list_abstract"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_label}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({url}): {exc}")
                    continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
