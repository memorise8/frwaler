# -*- coding: utf-8 -*-
"""Crawler for Ministry of Finance of the People's Republic of China (English Reports).

Starting URL: http://www.mof.gov.cn/en/reports/
List pagination: index.htm, index_2.htm, index_3.htm, ...
Items: <li class="media listpage"><h4><a href="./YYYYMM/tYYYYMMDD_NUM.htm">TITLE</a></h4></li>
Detail content: div#zoom (conpagecon) > div.TRS_Editor > <p> tags
Date / post_number extracted from URL path.
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import urljoin

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_LIST_BASE = "http://www.mof.gov.cn/en/reports/"
_PUBLISHER = "Ministry of Finance of the People's Republic of China"


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS 1.3 compat. Retries with exponential backoff."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        if attempt > 0:
            wait = 3 ** attempt  # 3s, 9s
            print(f"[mof-gov-cn-en] retry in {wait}s for {url[:80]}")
            time.sleep(wait)
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[mof-gov-cn-en] curl attempt {attempt + 1} error: {exc}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date_from_url(url: str) -> str | None:
    """Extract YYYY-MM-DD from URL pattern .../t20260520_3990222.htm"""
    m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _parse_post_number(url: str) -> str | None:
    """Extract post number from URL pattern .../tYYYYMMDD_3990222.htm → '3990222'."""
    m = re.search(r"_(\d+)\.htm$", url)
    if m:
        return m.group(1)
    return None


def _extract_abstract(soup) -> str:
    """Extract article abstract from the TRS_Editor / conpagecon content div."""
    content_div = (
        soup.find("div", id="zoom")
        or soup.find("div", class_="conpagecon")
        or soup.find("div", class_="TRS_Editor")
    )
    if not content_div:
        # Fallback: collect all <p> with meaningful text from body
        paras = []
        total = 0
        for p in soup.find_all("p"):
            text = p.get_text(separator=" ", strip=True)
            if len(text) < 30:
                continue
            paras.append(text)
            total += len(text)
            if total >= 600:
                break
        return " ".join(paras)[:2000]

    paras = []
    total = 0
    for tag in content_div.find_all(["p", "div"], recursive=True):
        # Skip nested divs that are just containers
        if tag.name == "div" and tag.find("p"):
            continue
        text = tag.get_text(separator=" ", strip=True)
        if len(text) < 30:
            continue
        paras.append(text)
        total += len(text)
        if total >= 800:
            break

    return " ".join(paras)[:2000]


class MofGovCnEnCrawler(BaseCrawler):

    site_id = "mof-gov-cn-en"
    site_name = "Custom: mof-gov-cn-en"
    base_url = "http://www.mof.gov.cn"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        max_seconds = 25 * 60  # 25-minute wall-clock cap

        page = 1
        max_pages = 200

        while page <= max_pages:
            # Wall-clock budget check
            if time.time() - start_time > max_seconds:
                print(f"[mof-gov-cn-en] 25-min wall-clock budget reached, stopping")
                break

            if saved >= limit_or_inf:
                break

            # Build list page URL
            if page == 1:
                page_url = _LIST_BASE + "index.htm"
            else:
                page_url = _LIST_BASE + f"index_{page}.htm"

            if page % 10 == 0 or page == 1:
                print(f"[mof-gov-cn-en] page {page}: saved {saved}/{limit_or_inf}")

            html = _curl_get(page_url)
            if not html:
                print(f"[mof-gov-cn-en] page {page}: fetch failed, stopping")
                break

            soup = _make_soup(html)
            if not soup:
                print(f"[mof-gov-cn-en] page {page}: parse failed, stopping")
                break

            # Collect items: <li class="media listpage"> or fallback to h4 a
            li_items = soup.find_all("li", class_="listpage")
            if li_items:
                anchors = [li.find("a", href=True) for li in li_items]
                anchors = [a for a in anchors if a]
            else:
                # Fallback: find all h4 > a on the page that point at .htm detail pages
                anchors = []
                for h4 in soup.find_all("h4"):
                    a = h4.find("a", href=True)
                    if a and re.search(r"/t\d{8}_\d+\.htm", a.get("href", "")):
                        anchors.append(a)

            if not anchors:
                print(f"[mof-gov-cn-en] page {page}: no items, end of list")
                break

            new_this_page = 0

            for anchor in anchors:
                if saved >= limit_or_inf:
                    break

                href = anchor.get("href", "")
                detail_url = urljoin(_LIST_BASE, href)

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_this_page += 1

                list_title = anchor.get_text(strip=True)

                try:
                    time.sleep(1.0)
                    detail_html = _curl_get(detail_url)
                    if not detail_html:
                        print(f"[mof-gov-cn-en] detail fetch failed: {detail_url}")
                        continue

                    detail_soup = _make_soup(detail_html)
                    if not detail_soup:
                        print(f"[mof-gov-cn-en] detail parse failed: {detail_url}")
                        continue

                    abstract = _extract_abstract(detail_soup)
                    if len(abstract) < 50:
                        print(
                            f"[mof-gov-cn-en] abstract too short "
                            f"({len(abstract)} chars), skipping: {detail_url}"
                        )
                        continue

                    pub_date = _parse_date_from_url(detail_url)
                    post_num = _parse_post_number(detail_url)

                    title = list_title or detail_url.rsplit("/", 1)[-1]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_num,
                        "post_number": post_num,
                        "url": detail_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": pub_date,
                        "publisher": _PUBLISHER,
                        "category": "Reports",
                        "pdf_url": None,
                        "keywords": None,
                        "metadata": json.dumps({
                            "posted_date": pub_date,
                            "post_number_raw": post_num,
                            "list_url": page_url,
                        }),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[mof-gov-cn-en] saved #{saved}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[mof-gov-cn-en] item failed {detail_url}: {exc}")
                    continue

            if new_this_page == 0:
                print(f"[mof-gov-cn-en] page {page}: no new URLs, stopping")
                break

            if page >= max_pages:
                print(f"[mof-gov-cn-en] safety cap of {max_pages} pages reached")

            page += 1
            time.sleep(0.5)

        return saved
