# -*- coding: utf-8 -*-
"""Crawler for Canada.ca news — Crown-Indigenous Relations and Northern Affairs Canada."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

LIST_URL = (
    "https://www.canada.ca/en/news/advanced-news-search/news-results.html"
    "?_=1618957516190&dprtmnt=crownindigenousrelationsandnorthernaffairscanada"
    "&start=&end="
)
PAGE_SIZE = 10
MAX_PAGES = 200
CRAWL_TIMEOUT_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
DETAIL_SLEEP = 1.0


def _fetch(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with exponential-backoff retries."""
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", url],
                capture_output=True,
                timeout=35,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[canada-ca-en] curl error (attempt {attempt+1}/{retries}) for {url}: {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            time.sleep(wait)
    return None


def _make_soup(html: str) -> BeautifulSoup | None:
    """Try html5lib → lxml → html.parser; return None on total failure."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_list_page(html: str) -> list[dict]:
    """Return list of raw item dicts from one search-results page."""
    soup = _make_soup(html)
    if not soup:
        return []

    items = []
    for article in soup.find_all("article", class_="item"):
        try:
            a_tag = article.find("a", href=True)
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            url = a_tag["href"]
            if not url.startswith("http"):
                url = "https://www.canada.ca" + url

            # Date
            time_tag = article.find("time")
            pub_date = None
            if time_tag and time_tag.get("datetime"):
                pub_date = time_tag["datetime"].strip()[:10]

            # Publisher + category live in the first <p> as pipe-separated text
            first_p = article.find("p")
            publisher = ""
            category = ""
            if first_p:
                raw = first_p.get_text(separator="|", strip=True)
                parts = [p.strip() for p in raw.split("|")]
                # parts[0] = date-string, [1] = publisher, [2] = category
                if len(parts) >= 2:
                    publisher = parts[1]
                if len(parts) >= 3:
                    category = parts[2]

            # Abstract = second meaningful <p>
            all_ps = article.find_all("p")
            abstract = ""
            for p in all_ps[1:]:
                text = p.get_text(strip=True)
                if len(text) > 20:
                    abstract = text
                    break

            slug = re.sub(r"\.html$", "", url.rstrip("/").split("/")[-1])

            items.append({
                "title": title,
                "url": url,
                "external_id": slug,
                "post_number": slug,
                "published_date": pub_date,
                "listed_date": pub_date,
                "publisher": publisher,
                "category": category,
                "abstract": abstract,
            })
        except Exception as exc:
            print(f"[canada-ca-en] list item parse error: {exc}")
            continue
    return items


def _fetch_detail_abstract(url: str) -> str:
    """Fetch detail page and extract a rich abstract."""
    html = _fetch(url)
    if not html:
        return ""
    soup = _make_soup(html)
    if not soup:
        return ""

    # Preferred: <p class="teaser hidden"> or <p class="teaser">
    teaser = soup.find("p", class_="teaser")
    if teaser:
        text = teaser.get_text(strip=True)
        if len(text) >= 50:
            return text

    # Fall back: first two substantive paragraphs inside .cmp-text
    chunks: list[str] = []
    for div in soup.find_all("div", class_="cmp-text"):
        for p in div.find_all("p"):
            text = p.get_text(strip=True)
            if len(text) > 40:
                chunks.append(text)
            if len(chunks) >= 2:
                break
        if len(chunks) >= 2:
            break

    if chunks:
        return " ".join(chunks)[:2000]

    # Last resort: main content text
    main = soup.find("main")
    if main:
        return main.get_text(separator=" ", strip=True)[:2000]

    return ""


class CanadaCaEnCrawler(BaseCrawler):
    site_id = "canada-ca-en"
    site_name = "Custom: canada-ca-en"
    base_url = "https://www.canada.ca"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_disp = str(limit) if limit is not None else "∞"

        for page_idx in range(MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > CRAWL_TIMEOUT_SECS:
                print(f"[canada-ca-en] 25-minute budget reached at page {page_idx}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_idx % 10 == 0:
                print(f"[canada-ca-en] page {page_idx}: saved {saved}/{limit_disp}")

            idx = page_idx * PAGE_SIZE
            url = f"{LIST_URL}&idx={idx}"

            html = _fetch(url)
            if not html:
                print(f"[canada-ca-en] failed to fetch list page {page_idx}, stopping.")
                break

            items = _parse_list_page(html)
            if not items:
                print(f"[canada-ca-en] no items on page {page_idx}, end of results.")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[canada-ca-en] all items on page {page_idx} already seen, stopping.")
                break

            for it in new_items:
                seen_urls.add(it["url"])

            for it in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    abstract = it["abstract"]

                    # Boost to >=100 chars if needed by fetching detail page
                    if len(abstract) < 100:
                        detail_abstract = _fetch_detail_abstract(it["url"])
                        if len(detail_abstract) > len(abstract):
                            abstract = detail_abstract
                        time.sleep(DETAIL_SLEEP)

                    if len(abstract) < 50:
                        print(f"[canada-ca-en] skipping (abstract <50 chars): {it['url']}")
                        continue

                    metadata = json.dumps(
                        {"posted_date": it.get("listed_date"), "category": it.get("category")},
                        ensure_ascii=False,
                    )

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": it["external_id"],
                        "post_number": it["post_number"],
                        "title": it["title"],
                        "abstract": abstract,
                        "published_date": it["published_date"],
                        "listed_date": it["listed_date"],
                        "publisher": it["publisher"],
                        "category": it["category"],
                        "url": it["url"],
                        "pdf_url": None,
                        "keywords": None,
                        "doi": None,
                        "original_filename": None,
                        "metadata": metadata,
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[canada-ca-en] item {it.get('url', '?')} failed: {exc}")
                    continue

            if page_idx == MAX_PAGES - 1:
                print(f"[canada-ca-en] reached safety cap of {MAX_PAGES} pages.")

            time.sleep(0.5)

        print(f"[canada-ca-en] done. saved={saved}")
        return saved
