# -*- coding: utf-8 -*-
"""NIAAA NIH News & Events crawler.

Listing: https://www.niaaa.nih.gov/news-events/news?page=N  (pages 0-7, ~10 items each)
Detail:  https://www.niaaa.nih.gov/news-events/{type}/{slug}

The listing page is Drupal-rendered HTML. Articles come in three types:
  - announcement  (/news-events/announcement/{slug})
  - spectrum      (/news-events/spectrum/{volume}/{slug})
  - news-releases (/news-events/news-releases/{slug})

Each detail page has its full body inside <main>, preceded by the title,
category label, and date. We strip those header tokens to build the abstract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.niaaa.nih.gov"
_LIST_URL = f"{_BASE}/news-events/news"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MIN_ABSTRACT = 100  # skip & log items whose abstract is shorter than this
_WALL_CLOCK_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget

_MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
_DATE_RE = re.compile(
    rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
    rf"({_MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})"
    rf"|({_MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})"
)


def _curl_get(url: str, user_agent: str, timeout: int = 30) -> str | None:
    """GET via curl; retry up to 3× with exponential backoff (1 s, 3 s, 9 s)."""
    delays = [1, 3, 9]
    for attempt in range(3):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk",
                    "--max-time", str(timeout),
                    "-H", "Accept: text/html,*/*;q=0.8",
                    "-H", f"User-Agent: {user_agent}",
                    url,
                ],
                capture_output=True,
                timeout=timeout + 5,
            )
            body = result.stdout.decode("utf-8", errors="replace")
            if body.strip():
                return body
            if attempt < 2:
                print(
                    f"[niaaa-nih-gov-news-events] empty response for {url}, "
                    f"retry {attempt + 1}/3 in {delays[attempt]}s"
                )
                time.sleep(delays[attempt])
        except Exception as exc:
            if attempt < 2:
                print(
                    f"[niaaa-nih-gov-news-events] curl error ({url}): {exc}, "
                    f"retry {attempt + 1}/3 in {delays[attempt]}s"
                )
                time.sleep(delays[attempt])
            else:
                print(f"[niaaa-nih-gov-news-events] curl failed after 3 attempts for {url}: {exc}")
    return None


def _make_soup(html: str):
    """BeautifulSoup with html5lib → lxml → html.parser fallback."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(text: str) -> str:
    """Return the first date found in *text* as YYYY-MM-DD, or ''."""
    m = _DATE_RE.search(text)
    if not m:
        return ""
    if m.group(1):  # weekday-prefixed form
        month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
    else:
        month_str, day_str, year_str = m.group(4), m.group(5), m.group(6)
    try:
        return datetime.strptime(
            f"{month_str} {day_str} {year_str}", "%B %d %Y"
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _strip_header_tokens(text: str, title: str) -> str:
    """Remove the boilerplate header (title / category / date / image label) from the start."""
    t = text.strip()

    # Remove title if it appears at the very start
    if title and t.startswith(title):
        t = t[len(title):].strip()

    # Remove category label
    t = re.sub(
        r"^(?:Announcement|Spectrum|News Release|Press Release|Event)\s*",
        "", t, flags=re.I,
    ).strip()

    # Remove weekday-prefixed date
    t = re.sub(
        rf"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
        rf"(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}\s*",
        "", t,
    ).strip()

    # Remove bare month date that may remain
    t = re.sub(
        rf"^(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}\s*",
        "", t,
    ).strip()

    # Remove "Spanish / En español" bilingual label
    t = re.sub(r"^Spanish\s*/\s*En espa[ñn]ol\s*", "", t, flags=re.I).strip()

    # Remove leading "Image" caption placeholder
    t = re.sub(r"^Image\s*", "", t, flags=re.I).strip()

    return t


class NIAAANewsEventsCrawler(BaseCrawler):
    """Crawler for NIAAA NIH News & Events."""

    site_id = "niaaa-nih-gov-news-events"
    site_name = "Custom: niaaa-nih-gov-news-events"
    base_url = "https://www.niaaa.nih.gov"

    # ------------------------------------------------------------------ #
    # Public interface                                                      #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        for page in range(_MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > _WALL_CLOCK_S:
                print(
                    f"[niaaa-nih-gov-news-events] 25-minute budget reached at page {page}. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(
                    f"[niaaa-nih-gov-news-events] page {page}: saved {saved}/{limit_str}"
                )

            page_url = f"{_LIST_URL}?page={page}"
            raw = _curl_get(page_url, self.USER_AGENT)
            if not raw:
                print(
                    f"[niaaa-nih-gov-news-events] failed to fetch listing page {page}. Stopping."
                )
                break

            article_links = self._parse_listing(raw)
            if not article_links:
                print(f"[niaaa-nih-gov-news-events] no items at page {page}. Done.")
                break

            # Detect end of real pagination: all links already seen → loop-back
            new_links = [(href, title) for href, title in article_links if href not in seen_urls]
            if not new_links:
                print(
                    f"[niaaa-nih-gov-news-events] all items on page {page} already seen. Done."
                )
                break

            for href, title in new_links:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(href)
                time.sleep(self._delay)

                try:
                    ok = self._fetch_and_save(href, title)
                    if ok:
                        saved += 1
                        print(
                            f"[niaaa-nih-gov-news-events] saved {saved}/{limit_str}: {title[:60]}"
                        )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[niaaa-nih-gov-news-events] item {href!r} failed: {exc}")
                    continue

        if page == _MAX_PAGES - 1:
            print(
                f"[niaaa-nih-gov-news-events] safety cap of {_MAX_PAGES} pages reached. Stopping."
            )

        print(f"[niaaa-nih-gov-news-events] done. total saved: {saved}")
        return saved

    # ------------------------------------------------------------------ #
    # Listing parser                                                        #
    # ------------------------------------------------------------------ #

    def _parse_listing(self, html: str) -> list[tuple[str, str]]:
        """Return (href, title) pairs for article links on a listing page.

        Only searches within <main> to avoid picking up navigation links.
        Category-index links (class="tags" or class="link") and shallow paths
        are filtered out.
        """
        soup = _make_soup(html)
        if soup is None:
            return self._parse_listing_regex(html)

        # Restrict to <main> content area so nav links are excluded
        root = soup.find("main") or soup

        seen: set[str] = set()
        result: list[tuple[str, str]] = []

        for a in root.find_all("a", href=True):
            href: str = a["href"]
            if not href.startswith("/news-events/"):
                continue
            # Skip category tag links and nav links
            cls = a.get("class") or []
            if "tags" in cls or "link" in cls:
                continue
            # Require at least 3 path segments: news-events / type / slug
            parts = href.strip("/").split("/")
            if len(parts) < 3:
                continue
            title = a.get_text(separator=" ", strip=True)
            if not title or href in seen:
                continue
            seen.add(href)
            result.append((href, title))

        return result

    def _parse_listing_regex(self, html: str) -> list[tuple[str, str]]:
        """Regex fallback for listing parsing when BeautifulSoup is unavailable."""
        links = re.findall(
            r'href="(/news-events/[^"?#]+)"[^>]*>([^<]+)</a>',
            html,
        )
        seen: set[str] = set()
        result = []
        for href, title in links:
            parts = href.strip("/").split("/")
            if len(parts) < 3:
                continue
            title = title.strip()
            if not title or href in seen:
                continue
            seen.add(href)
            result.append((href, title))
        return result

    # ------------------------------------------------------------------ #
    # Detail fetcher / parser                                              #
    # ------------------------------------------------------------------ #

    def _fetch_and_save(self, href: str, list_title: str) -> bool:
        """Fetch one detail page, parse it, and save. Returns True on success."""
        detail_url = f"{self.base_url}{href}"

        raw = _curl_get(detail_url, self.USER_AGENT)
        if not raw:
            print(
                f"[niaaa-nih-gov-news-events] failed to fetch {href} after 3 attempts, skipping."
            )
            return False

        soup = _make_soup(raw)
        if soup is None:
            print(f"[niaaa-nih-gov-news-events] parse failed for {href}, skipping.")
            return False

        # Title
        h1 = soup.find("h1")
        title = h1.get_text(separator=" ", strip=True) if h1 else list_title
        if not title:
            title = list_title

        # Main content block
        main = soup.find("main")
        if not main:
            print(f"[niaaa-nih-gov-news-events] no <main> on {href}, skipping.")
            return False

        main_text = main.get_text(separator=" ", strip=True)

        # Date
        published_date = _parse_date(main_text)

        # Abstract: strip the title/category/date/image header tokens
        abstract = _strip_header_tokens(main_text, title)

        if len(abstract) < _MIN_ABSTRACT:
            print(
                f"[niaaa-nih-gov-news-events] abstract too short "
                f"({len(abstract)} chars) for {href}, skipping."
            )
            return False

        # Derive metadata from URL path
        parts = href.strip("/").split("/")
        category = parts[1] if len(parts) > 1 else ""
        external_id = href.strip("/").replace("/", "--")

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": "",
            "doi": "",
            "department": "National Institute on Alcohol Abuse and Alcoholism",
            "metadata": json.dumps(
                {"source_path": href, "category": category},
                ensure_ascii=False,
            ),
        }

        self._save_paper(paper)
        return True
