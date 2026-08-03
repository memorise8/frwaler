# -*- coding: utf-8 -*-
"""Crawler for NESC (nesc.ie) Publications — National Economic & Social Council."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler


class NescIePublicationsCrawler(BaseCrawler):
    site_id = "nesc-ie-publications"
    site_name = "Custom: nesc-ie-publications"
    base_url = "https://www.nesc.ie"

    _LIST_API = "https://www.nesc.ie/wp-json/nesc/v1/publications-search"
    _ITEMS_PER_PAGE = 10  # fixed by the API

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, params=None):
        """GET via curl with retries. Returns response text or None."""
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json,text/html,*/*",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """Parse HTML with fallback parser chain. Returns BeautifulSoup or None."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _parse_list_html(self, html):
        """Extract publication stubs from the list API HTML response."""
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] Failed to build soup for list: {exc}")
            return []
        if soup is None:
            return []

        items = []
        for li in soup.find_all("li", class_="nesc-pub-search__item"):
            try:
                a = li.find("a", class_="nesc-pub-search__item-link")
                if not a:
                    continue
                url = (a.get("href") or "").strip()
                if not url:
                    continue

                time_el = li.find("time", class_="nesc-pub-search__date")
                date = (time_el.get("datetime") or "").strip() if time_el else ""

                title_el = li.find("h3", class_="nesc-pub-search__title")
                title = title_el.get_text(strip=True) if title_el else ""

                meta = {}
                for detail_div in li.find_all("div", class_="nesc-pub-search__detail"):
                    dt = detail_div.find("dt")
                    dd = detail_div.find("dd")
                    if dt and dd:
                        meta[dt.get_text(strip=True)] = dd.get_text(strip=True)

                items.append({
                    "url": url,
                    "date": date,
                    "title": title,
                    "pub_type": meta.get("Type", ""),
                    "report_no": meta.get("Report No.", ""),
                    "research_area": meta.get("Research Area", ""),
                })
            except Exception as exc:
                print(f"[{self.site_id}] List item parse error: {exc}")
                continue
        return items

    def _fetch_detail(self, url):
        """Fetch detail page and extract abstract, PDF URL, WP post ID."""
        raw = self._curl_get(url)
        if not raw:
            return None

        try:
            soup = self._make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] Soup error for {url}: {exc}")
            return {}
        if soup is None:
            return {}

        # Abstract: main entry content
        abstract = ""
        content_el = soup.select_one(".entry-content")
        if content_el:
            abstract = content_el.get_text(separator=" ", strip=True)

        # PDF URL and original filename
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower():
                pdf_url = href
                seg = href.rstrip("/").split("/")[-1]
                original_filename = seg if seg.lower().endswith(".pdf") else None
                break

        # WP post ID from body class attribute (postid-NNNN)
        wp_post_id = None
        body = soup.find("body")
        if body:
            for cls in body.get("class", []):
                m = re.match(r"postid-(\d+)$", cls)
                if m:
                    wp_post_id = m.group(1)
                    break

        return {
            "abstract": abstract,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "wp_post_id": wp_post_id,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        page = 1
        max_pages = 200
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        while True:
            try:
                # ---- limit / safety checks ----
                if limit is not None and saved >= limit:
                    break
                if page > max_pages:
                    print(f"[{self.site_id}] Safety cap: reached {max_pages} pages. Stopping.")
                    break
                if time.time() - start_time > max_seconds:
                    print(f"[{self.site_id}] Time budget (25 min) exceeded. Stopping cleanly.")
                    break

                # ---- fetch list page ----
                raw = None
                for attempt in range(3):
                    raw = self._curl_get(self._LIST_API, {"paged": page})
                    if raw:
                        break
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] List page {page} fetch failed "
                          f"(attempt {attempt + 1}/3), retry in {wait}s")
                    time.sleep(wait)

                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                    break

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(f"[{self.site_id}] Invalid JSON at page {page}: {exc}. Stopping.")
                    break

                if not data.get("success"):
                    print(f"[{self.site_id}] API returned success=false at page {page}. Stopping.")
                    break

                total_pages = int(data["data"].get("pages") or 0)
                html_chunk = data["data"].get("html", "")

                items = self._parse_list_html(html_chunk)
                if not items:
                    print(f"[{self.site_id}] No items at page {page}. Done.")
                    break

                # URL dedup guard against silent paginator loops
                new_items = [it for it in items if it["url"] not in seen_urls]
                if not new_items:
                    print(f"[{self.site_id}] All URLs on page {page} already seen. Stopping.")
                    break

                # ---- process each item ----
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    url = item["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    try:
                        time.sleep(self._delay)
                        detail = self._fetch_detail(url)
                        if detail is None:
                            print(f"[{self.site_id}] Failed to fetch detail: {url}")
                            continue

                        abstract = detail.get("abstract", "")
                        if len(abstract) < 50:
                            print(f"[{self.site_id}] Abstract too short (<50 chars), "
                                  f"skipping: {url}")
                            continue

                        slug = urlparse(url).path.strip("/").split("/")[-1] or url
                        wp_post_id = detail.get("wp_post_id")
                        # post_number: prefer WP integer ID (monotonic), then slug
                        post_number = wp_post_id or slug

                        pdf_url = detail.get("pdf_url")
                        original_filename = detail.get("original_filename")

                        paper = {
                            "site_id": self.site_id,
                            "external_id": slug,
                            "post_number": post_number,
                            "title": item["title"],
                            "abstract": abstract,
                            "published_date": item["date"],
                            "listed_date": item["date"],
                            "url": url,
                            "pdf_url": pdf_url,
                            "original_filename": original_filename,
                            "authors": "",
                            "publisher": "National Economic & Social Council",
                            "department": "",
                            "journal": "",
                            "category": item.get("pub_type", ""),
                            "keywords": item.get("research_area", ""),
                            "doi": None,
                            "metadata": json.dumps({
                                "posted_date": item["date"],
                                "pub_type": item.get("pub_type"),
                                "report_no": item.get("report_no"),
                                "research_area": item.get("research_area"),
                                "wp_post_id": wp_post_id,
                                "originalFilename": original_filename,
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        counter = f"{saved}/{limit}" if limit is not None else str(saved)
                        print(f"[{self.site_id}] Saved {counter}: {item['title'][:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] Item failed ({url}): {exc}")
                        continue

                # ---- progress logging ----
                if page % 10 == 0:
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                # ---- end of pages? ----
                if total_pages and page >= total_pages:
                    print(f"[{self.site_id}] Reached last page ({page}/{total_pages}). Done.")
                    break

                page += 1

            except KeyboardInterrupt:
                print(f"[{self.site_id}] Interrupted by user at page {page}, saved {saved}.")
                raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
