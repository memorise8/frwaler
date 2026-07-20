# -*- coding: utf-8 -*-
"""Crawler for HHS Guidance Portal.

Starting URL: https://www.hhs.gov/guidance/
Listing:      https://www.hhs.gov/guidance/?page=N  (N=0..~546, 10 items/page)
Detail:       https://www.hhs.gov/guidance/document/{slug}
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from urllib.parse import urljoin

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "hhs-gov-guidance"
_BASE_URL = "https://www.hhs.gov"
_LIST_URL = "https://www.hhs.gov/guidance/"
_ABSTRACT_MIN = 50
_MAX_PAGES = 200
_DELAY = 1.0

_CURL_HEADERS = [
    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: en-US,en;q=0.5",
    "-H", "Connection: keep-alive",
    "-H", "Upgrade-Insecure-Requests: 1",
    "-H", "Sec-Fetch-Dest: document",
    "-H", "Sec-Fetch-Mode: navigate",
    "-H", "Sec-Fetch-Site: none",
]


def _curl_get(url: str, *, timeout: int = 45, retries: int = 3) -> str | None:
    """Fetch URL via curl with browser headers and retry w/ exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--compressed",
        "--connect-timeout", "15", "--max-time", str(timeout),
    ] + _CURL_HEADERS + [url]

    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15, check=False)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip() and "<title>Access Denied</title>" not in raw[:1000]:
                return raw
            if "<title>Access Denied</title>" in raw[:1000]:
                print(f"[{_SITE_ID}] Access Denied attempt {attempt + 1}/{retries}: {url}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/{retries} for {url}: {exc}")

        if attempt < retries - 1:
            wait = waits[attempt]
            print(f"[{_SITE_ID}] retrying in {wait}s...")
            time.sleep(wait)

    print(f"[{_SITE_ID}] curl failed after {retries} attempts: {url}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _parse_date(text: str) -> str:
    """Convert date strings to YYYY-MM-DD.

    Handles ISO datetime ('2026-05-28T12:00:00Z') and US format ('5/28/2026').
    """
    if not text:
        return ""
    text = text.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return text


class HhsGovGuidanceCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: hhs-gov-guidance"
    base_url = _BASE_URL

    def _parse_listing(self, html: str) -> list[dict]:
        """Parse one listing page, return list of item dicts."""
        items = []
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] listing soup error: {exc}")
            return items

        for td in soup.select("td.views-field-field-form-types"):
            try:
                a = td.find("a", href=True)
                if not a:
                    continue
                href = a["href"]
                if "/document/" not in href:
                    continue

                title = a.get_text(strip=True)
                doc_url = href if href.startswith("http") else f"{_BASE_URL}{href}"
                slug = href.rstrip("/").split("/")[-1]

                publisher = ""
                category = ""
                listed_date = ""
                row = td.find_parent("tr")
                if row:
                    opdiv_td = row.select_one("td.views-field-field-opdiv-staffdiv")
                    if opdiv_td:
                        publisher = opdiv_td.get_text(strip=True)
                    status_td = row.select_one("td.views-field-field-guidance-status")
                    if status_td:
                        category = status_td.get_text(strip=True)
                    date_td = row.select_one("td.views-field-field-issue-date")
                    if date_td:
                        t = date_td.find("time")
                        if t:
                            listed_date = _parse_date(
                                t.get("datetime") or t.get_text(strip=True)
                            )

                items.append({
                    "title": title,
                    "url": doc_url,
                    "slug": slug,
                    "publisher": publisher,
                    "category": category,
                    "listed_date": listed_date,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] row parse error: {exc}")
                continue

        return items

    def _fetch_detail(self, doc_url: str, slug: str) -> dict | None:
        """Fetch and parse a detail page. Returns dict or None on failure."""
        html = _curl_get(doc_url)
        if not html:
            return None

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] detail soup error {doc_url}: {exc}")
            return None

        result: dict = {}

        # Abstract: paragraphs in .text-align-left, skipping download-button paragraphs
        main_div = soup.find("div", class_="text-align-left")
        if main_div:
            texts = []
            for p in main_div.find_all("p"):
                cls = p.get("class") or []
                cls_str = " ".join(cls)
                # Skip paragraphs whose only content is a download button
                btn = p.find("a", href=True)
                if btn:
                    btn_cls = " ".join(btn.get("class") or [])
                    if "usa-button" in btn_cls and not p.find("a", class_=lambda c: c and "usa-button" not in " ".join(c if isinstance(c, list) else [c])):
                        # paragraph is just a button link — skip
                        if len(p.get_text(strip=True)) < 80:
                            continue
                text = p.get_text(separator=" ", strip=True)
                if text and len(text) > 20:
                    texts.append(text)
            result["abstract"] = " ".join(texts).strip()

        # Sidebar: Unique ID, Date Published, Topic
        sidebar = soup.find("div", class_="guidance-sidebar")
        if sidebar:
            sidebar_text = sidebar.get_text(separator="\n", strip=True)

            uid_m = re.search(r"Unique ID[:\s]+\n?\s*(HHS-[A-Z0-9\-]+)", sidebar_text, re.I)
            if uid_m:
                result["external_id"] = uid_m.group(1).strip()

            pub_m = re.search(r"Date Published[:\s]+\n?\s*(\d{1,2}/\d{1,2}/\d{4})", sidebar_text)
            if pub_m:
                result["published_date"] = _parse_date(pub_m.group(1))

            # Topics list
            strong_el = sidebar.find("strong")
            if strong_el and "Topic" in strong_el.get_text(strip=True):
                ul = strong_el.find_next("ul")
                if ul:
                    topics = [
                        li.get_text(strip=True)
                        for li in ul.find_all("li")
                        if li.get_text(strip=True)
                    ]
                    if topics:
                        result["keywords"] = ", ".join(topics)

        # Fall back to slug if no Unique ID found
        if not result.get("external_id"):
            result["external_id"] = slug

        # PDF download link
        dl_link = soup.find("a", string=re.compile(r"Download.*Guidance", re.I))
        if not dl_link:
            dl_link = soup.select_one("a.usa-button")
        if dl_link:
            href = dl_link.get("href", "")
            if href:
                result["pdf_url"] = href if href.startswith("http") else urljoin(doc_url, href)

        return result

    def crawl(self, limit=None):
        """Crawl HHS Guidance Portal and save documents.

        Returns the count of saved documents.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(_MAX_PAGES):
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > 25 * 60:
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page_num}, stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page_num == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached, stopping")

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

            list_url = f"{_LIST_URL}?page={page_num}" if page_num > 0 else _LIST_URL
            list_html = _curl_get(list_url)
            if not list_html:
                print(f"[{_SITE_ID}] listing fetch failed at page {page_num}, stopping")
                break

            items = self._parse_listing(list_html)
            if not items:
                print(f"[{_SITE_ID}] no items on page {page_num}, stopping")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            for it in new_items:
                seen_urls.add(it["url"])

            if not new_items:
                print(f"[{_SITE_ID}] all items on page {page_num} already seen, stopping")
                break

            for it in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(_DELAY)
                    detail = self._fetch_detail(it["url"], it["slug"])
                    if not detail:
                        print(f"[{_SITE_ID}] detail fetch failed: {it['url']}")
                        continue

                    abstract = detail.get("abstract", "")
                    if not abstract or len(abstract) < _ABSTRACT_MIN:
                        print(
                            f"[{_SITE_ID}] skipping short abstract "
                            f"({len(abstract)} chars): {it['url']}"
                        )
                        continue

                    pdf_url = detail.get("pdf_url")
                    original_filename = None
                    if pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in tail and len(tail) <= 200:
                            original_filename = tail

                    paper_dict = {
                        "site_id": self.site_id,
                        "external_id": detail.get("external_id") or it["slug"],
                        "url": it["url"],
                        "title": it["title"],
                        "abstract": abstract,
                        "published_date": detail.get("published_date", ""),
                        "posted_date": it.get("listed_date", ""),
                        "publisher": it.get("publisher", ""),
                        "category": it.get("category", ""),
                        "keywords": detail.get("keywords", ""),
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "unique_id": detail.get("external_id"),
                                "guidance_status": it.get("category"),
                                "opdiv_staffdiv": it.get("publisher"),
                                "topic": detail.get("keywords"),
                                "slug": it["slug"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper_dict)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed {it['url']}: {exc}")
                    continue

        print(f"[{_SITE_ID}] done: saved {saved} documents")
        return saved
