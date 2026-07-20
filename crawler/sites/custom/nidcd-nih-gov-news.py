# -*- coding: utf-8 -*-
"""Custom crawler for NIDCD News (nidcd-nih-gov-news).

List page: https://www.nidcd.nih.gov/news/nidcd-news?page=N  (Drupal 10, pages 0-14)
Detail:    Fetch per-item URL; extract abstract from <meta name="description">.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(raw: str):
    """BeautifulSoup with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class NidcdNihGovNewsCrawler(BaseCrawler):
    site_id = "nidcd-nih-gov-news"
    site_name = "Custom: nidcd-nih-gov-news"
    base_url = "https://www.nidcd.nih.gov"

    LIST_URL = "https://www.nidcd.nih.gov/news/nidcd-news"
    MAX_PAGES = 200
    WALL_CLOCK_MINUTES = 25
    MIN_ABSTRACT_CHARS = 50
    _BACKOFF = (1, 3, 9)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch URL via curl; retry with exponential backoff."""
        url = url.strip()
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "-L",
                        "-A", self.USER_AGENT,
                        "--max-time", "30",
                        url,
                    ],
                    capture_output=True,
                    timeout=45,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[nidcd-nih-gov-news] curl error attempt {attempt+1}/{retries} for {url}: {exc}")
            if attempt < retries - 1:
                time.sleep(self._BACKOFF[attempt])
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[tuple[str, str, str]]:
        """Return list of (title, url, date_str) from a list-page HTML blob."""
        items: list[tuple[str, str, str]] = []
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[nidcd-nih-gov-news] list-page parse error: {exc}")
            return items

        for li in soup.find_all("li"):
            title_span = li.find("span", class_="views-field-title")
            date_span = li.find("span", class_="views-field-field-publish-date")
            if not title_span:
                continue
            a_tag = title_span.find("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = (a_tag.get("href") or "").strip()
            if not title or not href:
                continue
            url = href if href.startswith("http") else self.base_url + href
            date_str = ""
            if date_span:
                dt_text = date_span.get_text(strip=True)
                m = re.search(r"\((\d{2}/\d{2}/\d{4})\)", dt_text)
                if m:
                    try:
                        date_str = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
                    except Exception:
                        date_str = m.group(1)
            items.append((title, url, date_str))
        return items

    def _has_next_page(self, html: str) -> bool:
        """Return True if the page has a 'next page' link."""
        try:
            soup = _make_soup(html)
            return bool(
                soup.find("a", rel="next")
                or soup.find("a", title="Go to next page")
                or soup.find("li", class_="pager__item--next")
            )
        except Exception:
            return False

    def _fetch_abstract(self, url: str) -> str | None:
        """Fetch detail page and extract the best available abstract text."""
        html = self._curl_get(url)
        if not html:
            return None
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[nidcd-nih-gov-news] detail-page parse error for {url}: {exc}")
            return None

        # 1. <meta name="description"> / og:description / twitter:description
        for attr, val in [
            ("name", "description"),
            ("property", "og:description"),
            ("name", "twitter:description"),
        ]:
            tag = soup.find("meta", attrs={attr: val})
            if tag and tag.get("content"):
                text = tag["content"].strip()
                if len(text) >= self.MIN_ABSTRACT_CHARS:
                    return text

        # 2. Article body field
        for cls in ("field--name-body", "field--type-text-with-summary", "field-item"):
            container = soup.find(class_=cls)
            if container:
                text = re.sub(r"\s+", " ", container.get_text(separator=" ", strip=True)).strip()
                if len(text) >= self.MIN_ABSTRACT_CHARS:
                    return text[:2000]

        # 3. <article> element
        article = soup.find("article")
        if article:
            text = re.sub(r"\s+", " ", article.get_text(separator=" ", strip=True)).strip()
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                return text[:2000]

        # 4. <main> element fallback
        main = soup.find("main")
        if main:
            text = re.sub(r"\s+", " ", main.get_text(separator=" ", strip=True)).strip()
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                return text[:2000]

        return None

    # ------------------------------------------------------------------
    # Crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl NIDCD news listing pages and save articles."""
        saved = 0
        seen_urls: set[str] = set()
        limit_display = limit if limit is not None else "inf"
        deadline = time.time() + self.WALL_CLOCK_MINUTES * 60

        for page in range(self.MAX_PAGES):
            # Wall-clock budget
            if time.time() > deadline:
                print("[nidcd-nih-gov-news] wall-clock budget exceeded, exiting cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[nidcd-nih-gov-news] page {page}: saved {saved}/{limit_display}")

            list_url = f"{self.LIST_URL}?page={page}"
            html = self._curl_get(list_url)
            if not html:
                print(f"[nidcd-nih-gov-news] page {page}: fetch failed, stopping")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[nidcd-nih-gov-news] page {page}: no items found, stopping")
                break

            new_on_page = 0
            for title, url, date_str in items:
                if limit is not None and saved >= limit:
                    break
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(1.0)
                    abstract = self._fetch_abstract(url)
                    if not abstract or len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(f"[nidcd-nih-gov-news] skipping {url}: abstract too short or missing")
                        continue

                    external_id = re.sub(r"^https?://", "", url).rstrip("/")
                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "authors": json.dumps([]),
                        "abstract": abstract,
                        "category": "news",
                        "keywords": json.dumps([]),
                        "published_date": date_str,
                        "url": url,
                        "pdf_url": None,
                        "doi": None,
                        "department": "NIDCD",
                        "metadata": json.dumps({"source": "nidcd-news", "page": page}),
                    })
                    saved += 1
                    print(f"[nidcd-nih-gov-news] saved {saved}: {title[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nidcd-nih-gov-news] item failed ({url}): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[nidcd-nih-gov-news] page {page}: all items already seen, stopping")
                break

            if not self._has_next_page(html):
                print(f"[nidcd-nih-gov-news] page {page}: no next-page link, stopping")
                break
        else:
            print(f"[nidcd-nih-gov-news] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[nidcd-nih-gov-news] crawl complete: {saved} items saved")
        return saved
