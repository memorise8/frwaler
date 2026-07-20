# -*- coding: utf-8 -*-
"""Crawler for economy-ni.gov.uk/news — Department for the Economy (Northern Ireland).

Drupal 10 HTML listing + detail pages. List pages: ?page=N (20 items each).
Node IDs extracted from Drupal settings JSON as post_number.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.economy-ni.gov.uk"
_LIST_URL = _BASE + "/news"
_MAX_PAGES = 200
_RATE_SLEEP = 1.0


def _curl_get(url: str, max_retries: int = 3) -> str | None:
    delays = [1, 3, 9]
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "--max-time", "30",
                    "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    url,
                ],
                capture_output=True, timeout=35,
            )
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
        except Exception as exc:
            print(f"[economy-ni-gov-uk-news] curl error (attempt {attempt + 1}): {exc}")
        if attempt < max_retries - 1:
            time.sleep(delays[attempt])
    return None


def _parse_html(html: str):
    """BeautifulSoup with fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _extract_listing_urls(html: str) -> list[str]:
    """Extract article URLs from a listing page, excluding feed and sub-paths."""
    raw = re.findall(
        r'href="(https://www\.economy-ni\.gov\.uk/news/[^"#?]+)"', html
    )
    result = []
    seen = set()
    for url in raw:
        slug = url.split("/news/", 1)[-1].rstrip("/")
        # Skip the RSS feed and any path with extra segments
        if not slug or "feed" in slug or "/" in slug:
            continue
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _extract_node_id(html: str) -> str | None:
    """Extract Drupal node ID from settings JSON embedded in page."""
    m = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', html)
    return m.group(1) if m else None


def _extract_detail(url: str, html: str) -> dict:
    """Parse a detail page into structured fields."""
    soup = _parse_html(html)

    # Title
    title = ""
    if soup:
        try:
            h1 = soup.find("h1", class_="page-title") or soup.find("h1")
            title = h1.get_text(strip=True) if h1 else ""
        except Exception:
            pass
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # Published date from first <time datetime="...">
    published_date = ""
    m = re.search(r'<time\s+datetime="([^"]+)"', html)
    if m:
        raw_dt = m.group(1)
        published_date = raw_dt[:10]  # YYYY-MM-DD

    # Node ID
    node_id = _extract_node_id(html)

    # Abstract from <main> element text
    abstract = ""
    if soup:
        try:
            main = (
                soup.find("main")
                or soup.find("div", {"id": "main-content"})
                or soup.find("div", {"role": "main"})
            )
            if main:
                abstract = re.sub(r"\s+", " ", main.get_text(" ", strip=True)).strip()
        except Exception:
            pass
    if not abstract:
        m = re.search(r"<main[^>]*>(.*?)</main>", html, re.DOTALL)
        if m:
            abstract = re.sub(r"<[^>]+>", " ", m.group(1))
            abstract = re.sub(r"\s+", " ", abstract).strip()

    # PDF URL and filename
    pdf_url = None
    pdf_filename = None
    if soup:
        try:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    pdf_url = href if href.startswith("http") else _BASE + href
                    pdf_filename = href.rstrip("/").split("/")[-1].split("?")[0]
                    break
        except Exception:
            pass

    slug = url.split("/news/", 1)[-1].rstrip("/")

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "node_id": node_id,
        "slug": slug,
        "pdf_url": pdf_url,
        "pdf_filename": pdf_filename,
    }


class EconomyNIGovUKNewsCrawler(BaseCrawler):
    site_id = "economy-ni-gov-uk-news"
    site_name = "Custom: economy-ni-gov-uk-news"
    base_url = "https://www.economy-ni.gov.uk"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(_MAX_PAGES):
            # 25-minute wall-clock budget
            if time.time() - start_time > 25 * 60:
                print(
                    f"[economy-ni-gov-uk-news] 25-minute budget reached at page {page}. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(
                    f"[economy-ni-gov-uk-news] page {page}: saved {saved}/{limit_str}"
                )

            list_html = _curl_get(f"{_LIST_URL}?page={page}")
            if not list_html:
                print(
                    f"[economy-ni-gov-uk-news] Failed to fetch listing page {page}. Stopping."
                )
                break

            article_urls = _extract_listing_urls(list_html)
            new_urls = [u for u in article_urls if u not in seen_urls]

            if not new_urls:
                print(
                    f"[economy-ni-gov-uk-news] No new links on page {page}. End of pagination."
                )
                break

            for u in new_urls:
                seen_urls.add(u)

            # Fetch and save each article
            for article_url in new_urls:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(_RATE_SLEEP)
                    detail_html = _curl_get(article_url)
                    if not detail_html:
                        print(
                            f"[economy-ni-gov-uk-news] item {article_url} failed: empty response"
                        )
                        continue

                    d = _extract_detail(article_url, detail_html)
                    title = d["title"]
                    abstract = d["abstract"]
                    published_date = d["published_date"]
                    node_id = d["node_id"]
                    slug = d["slug"]
                    pdf_url = d["pdf_url"]
                    pdf_filename = d["pdf_filename"]

                    if not title:
                        print(
                            f"[economy-ni-gov-uk-news] item {article_url} failed: no title"
                        )
                        continue

                    if len(abstract) < 50:
                        print(
                            f"[economy-ni-gov-uk-news] item {article_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper({
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": node_id or slug,
                        "post_number": node_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": article_url,
                        "pdf_url": pdf_url,
                        "original_filename": pdf_filename,
                        "publisher": "Department for the Economy",
                        "authors": None,
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "News",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "node_id": node_id,
                                "slug": slug,
                                "posted_date": published_date,
                                "originalFilename": pdf_filename,
                            },
                            ensure_ascii=False,
                        ),
                    })
                    saved += 1
                    print(
                        f"[economy-ni-gov-uk-news] Saved {saved}/{limit_str}: {title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[economy-ni-gov-uk-news] item {article_url} failed: {exc}"
                    )
                    continue

        print(f"[economy-ni-gov-uk-news] Done. Total saved: {saved}")
        return saved
