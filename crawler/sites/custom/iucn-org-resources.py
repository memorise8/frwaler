# -*- coding: utf-8 -*-
"""IUCN Resources – Dataset crawler (rstype=1116).

List:   GET https://iucn.org/search-website?rstype=1116&thm=All&tpc=All&rgn=All&cntry=All&page=N
Detail: GET https://iucn.org/resources/dataset/{slug}
"""

from __future__ import annotations

import json
import re
import time
from html import unescape

from crawler.base_crawler import BaseCrawler


def _make_soup(html: str):
    """Return BeautifulSoup using first available parser; None on total failure."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class IUCNResourcesCrawler(BaseCrawler):
    site_id = "iucn-org-resources"
    site_name = "Custom: iucn-org-resources"
    base_url = "https://iucn.org"

    _LIST_URL = "https://iucn.org/search-website"
    _LIST_BASE_PARAMS = {
        "rstype": "1116",
        "thm": "All",
        "tpc": "All",
        "rgn": "All",
        "cntry": "All",
    }
    _MAX_PAGES = 200
    _WALL_MINS = 25
    _MIN_ABS = 50
    _BACKOFF = (1, 3, 9)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _get(self, url: str, params=None) -> str | None:
        """GET with exponential-backoff retries; returns UTF-8 body or None."""
        # Force gzip/deflate only — the base session sets 'br' but brotli may
        # not be installed, causing the server to return undecompressable bytes.
        headers = {"Accept-Encoding": "gzip, deflate"}
        for i, wait in enumerate((*self._BACKOFF, None)):
            try:
                resp = self._session.get(url, params=params, timeout=30,
                                         headers=headers)
                if resp.status_code == 200:
                    try:
                        return resp.content.decode("utf-8")
                    except UnicodeDecodeError:
                        return resp.content.decode("utf-8", errors="replace")
                print(f"[{self.site_id}] HTTP {resp.status_code} {url}")
            except Exception as exc:
                print(f"[{self.site_id}] network error ({url}): {exc}")
            if wait is None:
                return None
            time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Listing parser
    # ------------------------------------------------------------------

    def _parse_listing(self, html: str) -> list[dict]:
        """Return list of {url_path, title, date, label, desc} from listing HTML."""
        items = []

        soup = None
        try:
            soup = _make_soup(html)
        except Exception:
            pass

        if soup:
            vc = soup.select_one(".view-content")
            scope = vc if vc else soup
            for li in scope.select("li"):
                wt = li.select_one(".wrapper-title a")
                if not wt:
                    continue
                url_path = (wt.get("href") or "").strip()
                title = wt.get_text(strip=True)
                if not url_path or not title:
                    continue
                # Only keep actual resource paths (filter out nav/menu items)
                if not re.match(r"^/(?:index%2[Ee]php/)?resources/", url_path):
                    continue
                date_el = li.select_one(".search-date")
                label_el = li.select_one(".label")
                desc_el = li.select_one(".description")
                items.append({
                    "url_path": url_path,
                    "title": unescape(title),
                    "date": (date_el.get_text(strip=True) if date_el else ""),
                    "label": (label_el.get_text(strip=True) if label_el else ""),
                    "desc": (desc_el.get_text(strip=True) if desc_el else ""),
                })
        else:
            # Regex fallback (no BS4)
            vc_idx = html.find("view-content")
            chunk = html[vc_idx:vc_idx + 60000] if vc_idx >= 0 else html
            for m in re.finditer(
                r'class="wrapper-title">\s*<a href="(/(?:index%2[Ee]php/)?resources/[^"]+)"[^>]*>([^<]+)',
                chunk,
            ):
                url_path = m.group(1).strip()
                title = unescape(m.group(2).strip())
                items.append({
                    "url_path": url_path,
                    "title": title,
                    "date": "",
                    "label": "",
                    "desc": "",
                })

        return items

    # ------------------------------------------------------------------
    # Detail parser
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str) -> dict:
        """Extract {title, abstract, date, pdf_url} from detail page HTML."""
        result = {"title": "", "abstract": "", "date": "", "pdf_url": ""}

        soup = None
        try:
            soup = _make_soup(html)
        except Exception:
            pass

        if soup:
            h1 = soup.select_one("h1.title")
            if h1:
                result["title"] = h1.get_text(strip=True)

            ci = soup.select_one(".content-created-info")
            if ci:
                m = re.search(r"\d{4}", ci.get_text())
                if m:
                    result["date"] = m.group()

            main = soup.select_one(".main-section")
            if main:
                for tag in main.select("img, nav, script, style, .wrapper-img, .content-image"):
                    tag.decompose()
                text = main.get_text(separator=" ", strip=True)
                # Strip leading "Dataset / Publication / File ..." type label
                text = re.sub(
                    r"^(?:Dataset|Publication|Grey\s+literature|File|Database|Conservation\s+Tool|Brief)\s+",
                    "",
                    text,
                    flags=re.IGNORECASE,
                ).strip()
                result["abstract"] = text

            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if href.lower().endswith(".pdf"):
                    if href.startswith("/"):
                        href = "https://iucn.org" + href
                    result["pdf_url"] = href
                    break
        else:
            # Regex fallback
            h1_m = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h1>', html, re.DOTALL)
            if h1_m:
                result["title"] = _strip_tags(h1_m.group(1))

            yr_m = re.search(r"Year:\s*(\d{4})", html)
            if yr_m:
                result["date"] = yr_m.group(1)

            ms_idx = html.find('class="main-section"')
            if ms_idx >= 0:
                ms = html[ms_idx: ms_idx + 30000]
                ms = re.sub(r"<script[^>]*>.*?</script>", " ", ms, flags=re.DOTALL)
                ms = re.sub(r"<style[^>]*>.*?</style>", " ", ms, flags=re.DOTALL)
                text = _strip_tags(ms)
                text = re.sub(
                    r"^(?:Dataset|Publication|Grey\s+literature|File|Database|Conservation\s+Tool|Brief)\s+",
                    "",
                    text,
                    flags=re.IGNORECASE,
                ).strip()
                result["abstract"] = text

            pdf_m = re.search(r'href="([^"]+\.pdf)"', html, re.IGNORECASE)
            if pdf_m:
                url = pdf_m.group(1)
                if url.startswith("/"):
                    url = "https://iucn.org" + url
                result["pdf_url"] = url

        return result

    # ------------------------------------------------------------------
    # crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        deadline = time.time() + self._WALL_MINS * 60
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"

        for page_num in range(self._MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if time.time() > deadline:
                print(f"[{self.site_id}] wall-clock budget exhausted at page {page_num}; stopping")
                break

            if page_num % 10 == 0 and page_num > 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            params = {**self._LIST_BASE_PARAMS, "page": str(page_num)}
            html = self._get(self._LIST_URL, params=params)
            if not html:
                print(f"[{self.site_id}] failed to fetch listing page {page_num}; stopping")
                break

            listing_items = self._parse_listing(html)
            if not listing_items:
                print(f"[{self.site_id}] page {page_num}: no items found; stopping")
                break

            # Pagination loop guard: if first URL on this page was already seen, stop
            first_url = listing_items[0]["url_path"]
            if page_num > 0 and first_url in seen_urls:
                print(f"[{self.site_id}] pagination loop detected at page {page_num}; stopping")
                break

            new_on_page = 0
            for item in listing_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() > deadline:
                    print(f"[{self.site_id}] wall-clock budget reached inside page {page_num}")
                    break

                url_path = item["url_path"]
                if url_path in seen_urls:
                    continue
                seen_urls.add(url_path)
                new_on_page += 1

                # external_id = URL slug (last segment)
                external_id = re.sub(r"%2[Ee]", ".", url_path).rstrip("/").split("/")[-1]
                detail_url = (
                    "https://iucn.org" + url_path
                    if url_path.startswith("/")
                    else url_path
                )

                try:
                    time.sleep(self._delay)
                    detail_html = self._get(detail_url)
                    if not detail_html:
                        print(f"[{self.site_id}] failed detail fetch: {detail_url}; skipping")
                        continue

                    detail = self._parse_detail(detail_html)

                    title = detail["title"] or item["title"]
                    abstract = detail["abstract"] or item["desc"]
                    date_raw = detail["date"] or item["date"]
                    # Convert bare year to ISO date
                    if re.match(r"^\d{4}$", date_raw):
                        published_date = f"{date_raw}-01-01"
                    else:
                        published_date = date_raw

                    if not abstract or len(abstract) < self._MIN_ABS:
                        print(
                            f"[{self.site_id}] skip {external_id}: "
                            f"abstract {len(abstract)} chars < {self._MIN_ABS}"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": item.get("label", ""),
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url", ""),
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps(
                            {
                                "resourceType": item.get("label", ""),
                                "listingDescription": item.get("desc", ""),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {external_id} failed: {exc}; continuing")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_num}: all items already seen; stopping")
                break

            if page_num == self._MAX_PAGES - 1:
                print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages; stopping")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
