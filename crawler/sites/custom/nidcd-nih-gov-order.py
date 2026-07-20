# -*- coding: utf-8 -*-
"""Crawler for NIDCD Free Publications (https://www.nidcd.nih.gov/order)."""

import json
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_SITE_ID = "nidcd-nih-gov-order"
_BASE_URL = "https://www.nidcd.nih.gov"
_LIST_URL = _BASE_URL + "/order"
_PAGE_CAP = 200
_WALL_SECONDS = 25 * 60  # 25 minute budget


def _make_soup(raw):
    """Parse HTML with fallback: html5lib -> lxml -> html.parser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl_get(url, retries=3, max_time=30):
    """Fetch URL with curl; return decoded text or None. Retries with exponential backoff."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk",
                    "--max-time", str(max_time),
                    "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    url,
                ],
                capture_output=True,
                timeout=max_time + 5,
            )
            raw = result.stdout
            if raw:
                return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/{retries}: {exc}")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed for {url}: {exc}")
    return None


def _parse_list_page(html):
    """Parse list page HTML; return list of (title, url, short_desc, pdf_url)."""
    items = []
    soup = _make_soup(html)
    if not soup:
        return items
    for row in soup.find_all("div", class_="views-row"):
        try:
            h2 = row.find("h2")
            if not h2:
                continue
            a_tag = h2.find("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            if not title or not href:
                continue
            url = href if href.startswith("http") else _BASE_URL + href

            desc = ""
            desc_div = row.find("div", class_="order-desc")
            if desc_div:
                desc = re.sub(r"\s+", " ", desc_div.get_text(separator=" ", strip=True)).strip()

            pdf_url = ""
            pdf_span = row.find("span", class_="downloadpdf")
            if pdf_span:
                pdf_a = pdf_span.find("a")
                if pdf_a:
                    ph = pdf_a.get("href", "")
                    if ph:
                        pdf_url = ph if ph.startswith("http") else _BASE_URL + ph

            items.append((title, url, desc, pdf_url))
        except Exception:
            continue
    return items


def _parse_detail_page(html):
    """Parse detail page; return (abstract_text, published_date)."""
    soup = _make_soup(html)
    if not soup:
        return "", ""

    for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    article = soup.find("article")
    if article:
        for tag in article.find_all(["style", "script"]):
            tag.decompose()
        text = article.get_text(separator=" ", strip=True)
    else:
        main = soup.find("main")
        if main:
            for tag in main.find_all(["style", "script"]):
                tag.decompose()
            text = main.get_text(separator=" ", strip=True)
        else:
            text = soup.get_text(separator=" ", strip=True)

    text = re.sub(r"\s+", " ", text).strip()

    published_date = ""
    date_m = re.search(
        r"(?:Last\s+Reviewed|Last\s+Updated|Date\s+Reviewed|Updated)"
        r"\s*:?\s*([A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})",
        html,
        re.I,
    )
    if date_m:
        raw_date = date_m.group(1).strip()
        for fmt in ("%B %d, %Y", "%B %d %Y", "%Y-%m-%d"):
            try:
                published_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    return text, published_date


class NidcdNihGovOrderCrawler(BaseCrawler):
    """Crawler for NIDCD Free Publications."""

    site_id = _SITE_ID
    site_name = "Custom: nidcd-nih-gov-order"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Crawl NIDCD free publications from /order with full pagination."""
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        page = 0
        while True:
            # Wall-clock budget
            if time.time() - start_time > _WALL_SECONDS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Exiting cleanly.")
                break

            # Safety page cap
            if page >= _PAGE_CAP:
                print(f"[{_SITE_ID}] Safety cap of {_PAGE_CAP} pages reached. Stopping.")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            # Progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            list_url = f"{_LIST_URL}?page={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch page {page}. Stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page}. End of pagination.")
                break

            # URL-dedup: if every item on this page is already seen, the paginator looped
            new_items = [(t, u, d, p) for t, u, d, p in items if u not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] All items on page {page} already seen. Stopping.")
                break

            for title, pub_url, short_desc, pdf_url in new_items:
                if limit is not None and saved >= limit:
                    break
                if pub_url in seen_urls:
                    continue
                seen_urls.add(pub_url)

                try:
                    if time.time() - start_time > _WALL_SECONDS:
                        print(f"[{_SITE_ID}] Budget exhausted during item loop. Exiting.")
                        return saved

                    time.sleep(self._delay)

                    abstract = ""
                    published_date = ""
                    detail_raw = _curl_get(pub_url)
                    if detail_raw:
                        abstract, published_date = _parse_detail_page(detail_raw)

                    # Fall back to list description if detail is too short
                    if len(abstract) < 50:
                        abstract = short_desc

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] Skipping '{title[:40]}' — "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    external_id = pub_url.replace(_BASE_URL, "").lstrip("/")

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "Health Information",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url": pub_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": (
                            "National Institute on Deafness and Other Communication Disorders"
                        ),
                        "metadata": json.dumps(
                            {"short_description": short_desc}, ensure_ascii=False
                        ),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Item '{title[:40]}' failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
