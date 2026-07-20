# -*- coding: utf-8 -*-
"""Crawler for NIMHD NIH news releases.

Source: https://www.nimhd.nih.gov/news-events/all-news/news-releases

URL discovery strategy:
  1. sitemap.xml  — reliable, covers all /news-release/ paths
  2. Listing page 0 (curl, no antibot) — supplementary
  3. Playwright for listing pages 1+ (antibot blocks plain curl)
  4. Deduplicate all collected URLs; fetch each detail page with curl+retry.
"""

import json
import re
import subprocess
import sys
import time as time_mod

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "nimhd-nih-gov-news-events"
_BASE_URL = "https://www.nimhd.nih.gov"
_SITEMAP_URL = "https://www.nimhd.nih.gov/sitemap.xml"
_LIST_URL = "https://www.nimhd.nih.gov/news-events/all-news/news-releases"
_MAX_PAGES = 200
_MIN_ABSTRACT = 100
_MAX_WALL_SEC = 25 * 60


def _curl(url, retries=3):
    """Fetch URL via curl with TLS-max 1.3 and retry."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-L", url],
                capture_output=True,
                timeout=30,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", "replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt+1}: {exc}")
        if attempt < retries - 1:
            time_mod.sleep(delays[attempt])
    return None


def _make_soup(html):
    """Parse HTML with fallback parsers."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _extract_listing_urls(html, seen):
    """Return list of new /news-release/ URLs found in listing page HTML."""
    found = []
    try:
        soup = _make_soup(html)
        if not soup:
            return found
        for art in soup.find_all("article", class_="news"):
            if "grid-row" not in art.get("class", []):
                continue
            h3 = art.find("h3")
            if not h3:
                continue
            a_tag = h3.find("a", href=True)
            if not a_tag:
                continue
            href = a_tag["href"]
            if "/news-release/" not in href:
                continue
            full = href if href.startswith("http") else _BASE_URL + href
            if full not in seen:
                seen.add(full)
                found.append(full)
    except Exception as exc:
        print(f"[{_SITE_ID}] listing parse error: {exc}")
    return found


def _collect_all_urls():
    """Return ordered list of unique news-release URLs."""
    seen = set()
    urls = []

    # 1. Sitemap — primary source, no antibot
    print(f"[{_SITE_ID}] fetching sitemap…")
    xml = _curl(_SITEMAP_URL)
    if xml:
        raw = re.findall(
            r"<loc>(http://default/news-events/news-release/[^<]+)</loc>", xml
        )
        for u in raw:
            full = _BASE_URL + "/" + u.split("http://default/")[1]
            if full not in seen:
                seen.add(full)
                urls.append(full)
        print(f"[{_SITE_ID}] sitemap: {len(raw)} news-release URLs")

    # 2. Listing page 0 — curl works, no antibot on first page
    html0 = _curl(_LIST_URL)
    if html0:
        new = _extract_listing_urls(html0, seen)
        if new:
            urls.extend(new)
            print(f"[{_SITE_ID}] listing p0: +{len(new)} new URLs")

    # 3. Listing pages 1+ via playwright (antibot blocks curl)
    try:
        from crawler.playwright_fetcher import fetch_html

        for p in range(1, _MAX_PAGES):
            page_url = f"{_LIST_URL}?page={p}"
            print(f"[{_SITE_ID}] listing page {p} (playwright)…")
            html_p = fetch_html(
                page_url,
                timeout_seconds=30,
                wait_for_selector="article",
                extra_wait_seconds=2.0,
            )
            if not html_p:
                print(f"[{_SITE_ID}] page {p}: no HTML, stopping pagination")
                break
            new = _extract_listing_urls(html_p, seen)
            if not new:
                print(f"[{_SITE_ID}] page {p}: 0 new URLs, stopping pagination")
                break
            urls.extend(new)
            print(f"[{_SITE_ID}] page {p}: +{len(new)} URLs (total {len(urls)})")
            if p >= _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] reached {_MAX_PAGES}-page safety cap, stopping")
                break

    except Exception as exc:
        print(f"[{_SITE_ID}] playwright listing skipped: {exc}")

    return urls


def _parse_date(soup):
    """Extract published date from detail page."""
    # Strategy 1: look for "Published Date" NavigableString → nearby text
    for tag in soup.find_all(string=re.compile(r"Published\s*Date", re.I)):
        try:
            parent = tag.parent
            full_txt = parent.get_text(" ", strip=True)
            m = re.search(r"Published\s*Date\s*:?\s*(.+?)(?:\s*Share\s*:|$)", full_txt, re.I)
            if m:
                d = m.group(1).strip()
                if d:
                    return d
        except Exception:
            continue

    # Strategy 2: <time> tag on the page
    t = soup.find("time")
    if t:
        return t.get("datetime") or t.get_text(strip=True)

    return ""


def _parse_detail(url):
    """Fetch and parse a news-release detail page. Returns dict or None."""
    html = _curl(url)
    if not html:
        return None

    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] soup error for {url}: {exc}")
        return None
    if not soup:
        return None

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Abstract: field--name-body is the CMS body field
    body_div = soup.select_one(".field--name-body")
    abstract = body_div.get_text(" ", strip=True) if body_div else ""

    # Fallback to usa-prose
    if len(abstract) < _MIN_ABSTRACT:
        prose = soup.select_one(".usa-prose")
        if prose:
            abstract = prose.get_text(" ", strip=True)

    published_date = _parse_date(soup)

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
    }


class NimhdNihGovNewsEventsCrawler(BaseCrawler):
    site_id = "nimhd-nih-gov-news-events"
    site_name = "Custom: nimhd-nih-gov-news-events"
    base_url = "https://www.nimhd.nih.gov"

    def crawl(self, limit=None):
        start_t = time_mod.time()
        limit_val = limit if limit is not None else float("inf")
        saved = 0
        seen_urls = set()

        # Collect URLs (sitemap + listing pages)
        all_urls = _collect_all_urls()
        print(f"[{_SITE_ID}] total unique URLs collected: {len(all_urls)}")

        for idx, url in enumerate(all_urls):
            # Wall-clock budget
            if time_mod.time() - start_t > _MAX_WALL_SEC:
                print(f"[{_SITE_ID}] 25-minute wall budget reached, stopping.")
                break

            if saved >= limit_val:
                break

            # URL dedup (belt-and-suspenders)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Progress every 10
            if idx > 0 and idx % 10 == 0:
                lim_str = str(limit_val) if limit_val != float("inf") else "∞"
                print(f"[{_SITE_ID}] page {idx}: saved {saved}/{lim_str}")

            try:
                detail = _parse_detail(url)

                if not detail:
                    print(f"[{_SITE_ID}] item {idx}: no content from {url}")
                    continue

                title = detail["title"]
                abstract = detail["abstract"]

                if not title:
                    print(f"[{_SITE_ID}] item {idx}: no title, skipping {url}")
                    continue

                if len(abstract) < _MIN_ABSTRACT:
                    print(
                        f"[{_SITE_ID}] item {idx}: abstract too short "
                        f"({len(abstract)} chars), skipping {url}"
                    )
                    continue

                external_id = url.rstrip("/").split("/")[-1]

                paper = {
                    "id": external_id,
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "title": title,
                    "authors": json.dumps([]),
                    "abstract": abstract,
                    "category": "News Release",
                    "keywords": json.dumps([]),
                    "published_date": detail["published_date"],
                    "url": url,
                    "pdf_url": None,
                    "doi": None,
                    "department": (
                        "National Institute on Minority Health and Health Disparities"
                    ),
                    "metadata": json.dumps({"source": "nimhd.nih.gov"}),
                }

                self._save_paper(paper)
                saved += 1

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                continue

            time_mod.sleep(1.0)

        print(f"[{_SITE_ID}] done: saved {saved} items")
        return saved
