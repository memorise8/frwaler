# -*- coding: utf-8 -*-
"""Crawler for Department for Infrastructure (Northern Ireland) news."""

import json
import re
import subprocess
import time
from datetime import datetime, timezone

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler

_BS4_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str):
    from importlib import import_module
    bs4 = import_module("bs4")
    for parser in _BS4_PARSERS:
        try:
            return bs4.BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No working HTML parser found")


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS workaround and retry."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-A",
                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                 "-L", "--max-time", "30", url],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[infrastructure-ni-gov-uk-news] curl error attempt {attempt+1}: {exc}")
        if attempt < retries - 1:
            wait = 2 ** attempt  # 1s, 2s, 4s
            time.sleep(wait)
    return None


def _parse_date(raw: str) -> str | None:
    """Parse a date string to YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    # ISO datetime like 2026-05-08T12:00:00Z
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    # "8 May 2026"
    for fmt in ("%d %B %Y", "%d %b %Y", "%-d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _extract_detail(html: str, url: str):
    """Extract title, abstract, date from a news detail page."""
    soup = _make_soup(html)

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Date — first <time> with datetime attr
    published_date = None
    for t in soup.find_all("time"):
        dt_attr = t.get("datetime", "")
        if dt_attr:
            published_date = _parse_date(dt_attr)
            break
    if not published_date:
        # Try text like "8 May 2026"
        for t in soup.find_all("time"):
            published_date = _parse_date(t.get_text(strip=True))
            if published_date:
                break

    # Abstract — collect <p> tags from <main>, skip nav/header/footer noise
    abstract = ""
    main = soup.find("main")
    container = main if main else soup
    paras = []
    for p in container.find_all("p"):
        # Skip paragraphs inside nav/header/footer/aside
        parent_tags = {a.name for a in p.parents}
        if parent_tags & {"nav", "header", "footer", "aside"}:
            continue
        txt = p.get_text(" ", strip=True)
        if len(txt) > 20:
            paras.append(txt)
    abstract = " ".join(paras)

    # Topics / category from the page
    category = None
    for el in soup.find_all(class_=lambda c: c and "topics" in c.lower()):
        cat_txt = el.get_text(" ", strip=True)
        if cat_txt:
            category = cat_txt[:200]
            break

    # Node ID from article class (Drupal often puts node--id-XXXX)
    node_id = None
    art = soup.find("article")
    if art:
        for cls in art.get("class", []):
            m = re.search(r"node--id-(\d+)", cls)
            if m:
                node_id = m.group(1)
                break
    # Also try data-history-node-id
    if not node_id:
        for el in soup.find_all(attrs={"data-history-node-id": True}):
            node_id = el.get("data-history-node-id")
            break

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "category": category,
        "node_id": node_id,
    }


class InfrastructureNiGovUkNewsCrawler(BaseCrawler):
    site_id = "infrastructure-ni-gov-uk-news"
    site_name = "Custom: infrastructure-ni-gov-uk-news"
    base_url = "https://www.infrastructure-ni.gov.uk"

    LIST_URL = "https://www.infrastructure-ni.gov.uk/news"
    MAX_PAGES = 200
    PUBLISHER = "Department for Infrastructure"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        page = 0
        while page < self.MAX_PAGES:
            # Wall-clock budget check
            if time.time() - start_time > max_seconds:
                print(f"[infrastructure-ni-gov-uk-news] Wall-clock budget reached at page {page}, stopping.")
                break

            if saved >= limit_or_inf:
                break

            if page == self.MAX_PAGES - 1:
                print(f"[infrastructure-ni-gov-uk-news] Safety cap of {self.MAX_PAGES} pages reached.")

            list_url = f"{self.LIST_URL}?page={page}"
            html = _curl_get(list_url)
            if not html:
                print(f"[infrastructure-ni-gov-uk-news] Failed to fetch list page {page}, stopping.")
                break

            try:
                soup = _make_soup(html)
            except Exception as exc:
                print(f"[infrastructure-ni-gov-uk-news] Failed to parse list page {page}: {exc}")
                page += 1
                continue

            # Extract absolute news URLs
            page_urls = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith(f"{self.base_url}/news/") and href not in seen_urls:
                    page_urls.append(href)
                    seen_urls.add(href)

            if not page_urls:
                print(f"[infrastructure-ni-gov-uk-news] No new items on page {page}, pagination done.")
                break

            if page % 10 == 0:
                print(f"[infrastructure-ni-gov-uk-news] page {page}: saved {saved}/{limit_or_inf}")

            for item_url in page_urls:
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > max_seconds:
                    break

                try:
                    detail_html = _curl_get(item_url)
                    if not detail_html:
                        print(f"[infrastructure-ni-gov-uk-news] Failed to fetch {item_url}, skipping.")
                        continue

                    detail = _extract_detail(detail_html, item_url)
                    title = detail["title"]
                    abstract = detail["abstract"]
                    published_date = detail["published_date"]
                    node_id = detail["node_id"]

                    if not title:
                        print(f"[infrastructure-ni-gov-uk-news] No title at {item_url}, skipping.")
                        continue

                    if len(abstract) < 50:
                        print(f"[infrastructure-ni-gov-uk-news] Abstract too short ({len(abstract)} chars) at {item_url}, skipping.")
                        continue

                    # Slug from URL path (last segment)
                    slug = item_url.rstrip("/").split("/")[-1]

                    metadata = {
                        "node_id": node_id,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": None,
                        "publisher": self.PUBLISHER,
                        "department": None,
                        "journal": None,
                        "url": item_url,
                        "pdf_url": None,
                        "keywords": None,
                        "category": detail.get("category"),
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[infrastructure-ni-gov-uk-news] item {item_url} failed: {exc}")
                    continue

            page += 1

        print(f"[infrastructure-ni-gov-uk-news] Done. Saved {saved} items.")
        return saved
