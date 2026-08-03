# -*- coding: utf-8 -*-
"""NIBIB NIH News Events - Press Releases crawler.

Target: https://www.nibib.nih.gov/news-events/press-releases
Drupal 10 site; list pages use ?page=N (0-indexed); detail pages at
/news-events/newsroom/<slug>.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler


class NIBIBNewsEventsCrawler(BaseCrawler):
    site_id = "nibib-nih-gov-news-events"
    site_name = "Custom: nibib-nih-gov-news-events"
    base_url = "https://www.nibib.nih.gov"

    _LIST_URL = "https://www.nibib.nih.gov/news-events/press-releases"
    _SAFETY_CAP = 200

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET with curl, TLS-max 1.3, up to 3 retries + exponential backoff."""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] Empty response for {url}, retry in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl error ({url}): {exc}, retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list:
        soup = self._make_soup(html)
        if not soup:
            return []

        items = []
        for row in soup.find_all("div", class_="views-row"):
            try:
                h3 = row.find(
                    "h3",
                    class_=lambda c: c and "views-field-field-link" in c,
                )
                if not h3:
                    continue
                a = h3.find("a")
                if not a:
                    continue
                title = a.get_text(strip=True)
                href = a.get("href", "").strip()
                if not href:
                    continue
                url = href if href.startswith("http") else f"{self.base_url}{href}"

                # Date: "Press Releases · October 31, 2024"
                date_str = ""
                date_span = row.find(
                    "span",
                    class_=lambda c: c and "views-field-field-publication-date" in c,
                )
                if date_span:
                    date_text = date_span.get_text(" ", strip=True)
                    m = re.search(r"(\w+ \d+,\s*\d{4})", date_text)
                    if m:
                        try:
                            dt = datetime.strptime(m.group(1).strip(), "%B %d, %Y")
                            date_str = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass

                # Teaser (short abstract from list page)
                teaser = ""
                body_div = row.find(
                    "div",
                    class_=lambda c: c and "views-field-body" in c,
                )
                if body_div:
                    teaser = body_div.get_text(" ", strip=True)

                items.append({
                    "title": title,
                    "url": url,
                    "published_date": date_str,
                    "teaser": teaser,
                })
            except Exception as exc:
                print(f"[{self.site_id}] Error parsing list row: {exc}")
                continue

        return items

    def _get_last_page(self, html: str) -> int:
        """Return the 0-indexed last page number from pagination links."""
        nums = re.findall(r'\?page=(\d+)', html)
        return max((int(n) for n in nums), default=0)

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str) -> dict:
        soup = self._make_soup(html)
        if not soup:
            return {}

        node_id = ""
        article = soup.find("article")
        if article:
            node_id = article.get("data-history-node-id", "") or ""

        # Full body text
        abstract = ""
        body_field = soup.find(
            "div",
            class_=lambda c: c and "field--name-body" in c,
        )
        if body_field:
            abstract = body_field.get_text(" ", strip=True)

        # Fallback: wider article text if body field is sparse
        if len(abstract) < 100 and article:
            abstract = re.sub(r"\s+", " ", article.get_text(" ", strip=True)).strip()

        return {"node_id": node_id, "abstract": abstract}

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        page = 0
        last_page: int | None = None

        while True:
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached, exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {self._SAFETY_CAP} pages reached.")
                break

            list_url = f"{self._LIST_URL}?page={page}"
            list_html = self._curl_get(list_url)
            if not list_html:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping.")
                break

            if last_page is None:
                last_page = self._get_last_page(list_html)
                print(f"[{self.site_id}] Pages detected: 0–{last_page} ({last_page + 1} total)")

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            items = self._parse_list_page(list_html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}, stopping.")
                break

            new_this_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(item_url)
                    if not detail_html:
                        print(f"[{self.site_id}] item {item_url} failed: no response, skipping")
                        continue

                    detail = self._parse_detail(detail_html)
                    abstract = detail.get("abstract", "") or item.get("teaser", "")
                    abstract = re.sub(r"\s+", " ", abstract).strip()

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract <50 chars for {item_url}, skipping.")
                        continue

                    node_id = detail.get("node_id", "")
                    external_id = node_id or item_url.rstrip("/").split("/")[-1]
                    slug = item_url.rstrip("/").split("/")[-1]

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": item["title"],
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "Press Releases",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": item["published_date"],
                        "url": item_url,
                        "pdf_url": "",
                        "doi": "",
                        "department": "National Institute of Biomedical Imaging and Bioengineering",
                        "metadata": json.dumps(
                            {"slug": slug, "source": "nibib.nih.gov"},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_this_page += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            # Stop if we hit the last page or got no new records
            if last_page is not None and page >= last_page:
                print(f"[{self.site_id}] Reached last page ({last_page}), done.")
                break

            if new_this_page == 0:
                print(f"[{self.site_id}] No new items on page {page}, stopping.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
