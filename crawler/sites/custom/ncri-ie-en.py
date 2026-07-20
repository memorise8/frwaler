# -*- coding: utf-8 -*-
"""National Cancer Registry Ireland (NCRI) – Reports & Publications crawler.

List endpoint (section=3 = "Reports & Publications", 135 items, 12/page):
  https://www.ncri.ie/en/search?keywords=&section=3&sort_bef_combine=relevance_DESC&page=N

Detail pages:
  https://www.ncri.ie/en/reports-publications/reports/<slug>

Drupal 10 site – no JSON API; scraped via BeautifulSoup.
Node IDs are extracted from drupal-settings-json.  PDF filenames come from
Content-Disposition header on HEAD of /en/media/<id>/download.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

_ADDRESS_SIGNAL = re.compile(r"Cork Airport|Kinsale Road|Phone:\s*\+353", re.I)


def _make_soup(raw: str):
    """Build BeautifulSoup with html5lib → lxml → html.parser fallback."""
    if BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(tag_or_str) -> str:
    if tag_or_str is None:
        return ""
    if hasattr(tag_or_str, "get_text"):
        text = tag_or_str.get_text(separator=" ")
    else:
        text = str(tag_or_str)
    return re.sub(r"\s+", " ", text).strip()


def _meta_one(soup, *names: str) -> str | None:
    """Return the content of the first matching <meta> tag."""
    if not soup:
        return None
    for name in names:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
        tag = soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    return None


def _node_id_from_drupal(soup) -> str | None:
    """Extract Drupal node ID from drupal-settings-json script tag."""
    if not soup:
        return None
    script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
    if not script:
        return None
    raw = script.string or script.get_text()
    try:
        data = json.loads(raw)
        current_path = (data.get("path") or {}).get("currentPath", "")
        m = re.search(r"node/(\d+)", current_path)
        return m.group(1) if m else None
    except Exception:
        return None


class NcriIeEnCrawler(BaseCrawler):
    """Crawler for National Cancer Registry Ireland – Reports & Publications."""

    site_id = "ncri-ie-en"
    site_name = "Custom: ncri-ie-en"
    base_url = "https://www.ncri.ie"

    _SEARCH_URL = "https://www.ncri.ie/en/search"
    _SECTION = "3"          # "Reports & Publications" — 135 items across ~12 pages
    _MAX_PAGES = 200
    _WALL_CLOCK_BUDGET_S = 25 * 60
    _MIN_ABSTRACT_CHARS = 100

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, timeout: int = 30) -> str | None:
        """GET via curl with TLS compat; retries with 1 s / 3 s / 9 s backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.9",
            url,
        ]
        for attempt, wait in enumerate((1, 3, 9), start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                err = result.stderr.decode("utf-8", errors="replace").strip()
                if err:
                    print(f"[{self.site_id}] empty body attempt {attempt}/3 for {url}: {err[:120]}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")
            if attempt < 3:
                time.sleep(wait)
        return None

    def _curl_head(self, url: str) -> dict:
        """HEAD request; return lowercased header dict."""
        cmd = ["curl", "--tls-max", "1.3", "-skIL", "--max-time", "15", url]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=20)
            hdrs: dict[str, str] = {}
            for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    hdrs[k.strip().lower()] = v.strip()
            return hdrs
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str) -> tuple[list[str], bool]:
        """Return (list_of_detail_urls, has_next_page)."""
        soup = _make_soup(raw)
        if not soup:
            return [], False

        urls: list[str] = []
        for art in soup.find_all("article", class_=re.compile(r"search-result")):
            link = art.find("a", class_="blocklink__link")
            if link is None:
                link = art.find("a", href=re.compile(r"^/en/"))
            if not link:
                continue
            href = link.get("href", "")
            if href.startswith("/en/"):
                urls.append(urljoin(self.base_url, href))

        # Drupal slimline theme uses title="Go to next page" on the next-page link.
        has_next = bool(
            soup.find("a", title="Go to next page")
            or soup.find("li", class_="pager__item--next")
        )
        return urls, has_next

    # ------------------------------------------------------------------
    # Abstract extraction
    # ------------------------------------------------------------------

    def _build_abstract(self, soup, og_desc: str = "") -> str:
        """field-block-text content → og:description → <p> tags."""
        # Structured field blocks (first non-address block with >= MIN_ABSTRACT_CHARS)
        for div in soup.find_all("div", class_=re.compile(r"field--name-field-block-text")):
            text = _clean_text(div)
            if not _ADDRESS_SIGNAL.search(text) and len(text) >= self._MIN_ABSTRACT_CHARS:
                return text

        # og:description is usually a compact but useful summary
        if len(og_desc) >= self._MIN_ABSTRACT_CHARS:
            return og_desc

        # Fallback: collect substantial <p> tags from main content
        main = soup.find("main") or soup
        paras: list[str] = []
        for p in main.find_all("p"):
            text = _clean_text(p)
            if len(text) >= 40 and not _ADDRESS_SIGNAL.search(text):
                paras.append(text)
        if paras:
            combined = " ".join(paras[:8])
            if len(combined) >= self._MIN_ABSTRACT_CHARS:
                return combined

        return og_desc  # even if short — caller decides whether to skip

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw: str, url: str) -> dict | None:
        """Parse a detail page; return paper dict or None on hard failure."""
        soup = _make_soup(raw)
        if not soup:
            return None

        # Title
        title = _clean_text(_meta_one(soup, "og:title") or "")
        if not title:
            h1 = soup.find("h1")
            title = _clean_text(h1) if h1 else ""
        if not title:
            return None

        # Node ID from Drupal settings JSON (reliable numeric external_id)
        node_id = _node_id_from_drupal(soup)
        slug = urlparse(url).path.rstrip("/").split("/")[-1] or url
        external_id = node_id or slug
        post_number = node_id or slug

        # Published date — prefer field-date area, fall back to first <time>
        published_date: str | None = None
        listed_date: str | None = None
        field_date_div = soup.find("div", class_=re.compile(r"field--name-field-date"))
        if field_date_div:
            t = field_date_div.find("time")
            if t:
                dt = t.get("datetime", "")
                published_date = dt[:10] if dt else None
                listed_date = published_date
        if not published_date:
            all_times = soup.find_all("time")
            if all_times:
                dt = all_times[0].get("datetime", "")
                published_date = dt[:10] if dt else None
                listed_date = published_date
            if len(all_times) >= 2:
                dt2 = all_times[1].get("datetime", "")
                if dt2:
                    listed_date = dt2[:10]

        # Abstract
        og_desc = _clean_text(_meta_one(soup, "og:description", "description") or "")
        abstract = self._build_abstract(soup, og_desc)

        # PDF link and original filename
        pdf_url: str | None = None
        original_filename: str | None = None
        media_id: str | None = None
        media_link = soup.find("a", href=re.compile(r"/en/media/\d+/download"))
        if media_link:
            media_href = media_link.get("href", "")
            pdf_url = urljoin(self.base_url, media_href)
            m = re.search(r"/media/(\d+)/", media_href)
            if m:
                media_id = m.group(1)
            # Content-Disposition header carries the real filename
            hdrs = self._curl_head(pdf_url)
            cd = hdrs.get("content-disposition", "")
            fn_m = re.search(r"filename=([^\s;\r\n]+)", cd, re.I)
            if fn_m:
                original_filename = fn_m.group(1).strip("\"'")

        # Category
        sect_div = soup.find("div", class_=re.compile(r"field--name-field-section"))
        category = _clean_text(sect_div) if sect_div else ""
        if not category or len(category) > 80:
            category = "Reports & Publications"

        metadata = {
            "posted_date": published_date,
            "originalFilename": original_filename,
            "node_id": node_id,
            "media_id": media_id,
            "slug": slug,
            "section": self._SECTION,
            "og_description": og_desc,
        }

        return {
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id else None,
            "post_number": str(post_number) if post_number else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": category,
            "publisher": "National Cancer Registry Ireland",
            "authors": None,
            "journal": None,
            "keywords": None,
            "doi": None,
            "department": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        page = 0

        while page < self._MAX_PAGES:
            if time.monotonic() - started_at >= self._WALL_CLOCK_BUDGET_S - 30:
                print(f"[{self.site_id}] approaching 25-min budget at page {page}; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = (
                f"{self._SEARCH_URL}?keywords=&section={self._SECTION}"
                f"&sort_bef_combine=relevance_DESC&page={page}"
            )
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] page {page}: list fetch failed; stopping")
                break

            urls, has_next = self._parse_list_page(raw)
            if not urls:
                print(f"[{self.site_id}] page {page}: no articles found; end of pagination")
                break

            new_urls = [u for u in urls if u not in seen_urls]
            for u in new_urls:
                seen_urls.add(u)

            if not new_urls:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                break

            for url in new_urls:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - started_at >= self._WALL_CLOCK_BUDGET_S - 30:
                    print(f"[{self.site_id}] approaching 25-min budget; stopping cleanly")
                    return saved

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {url} failed: empty response")
                        continue

                    paper = self._parse_detail(detail_raw, url)
                    if not paper:
                        print(f"[{self.site_id}] item {url} failed: parse returned None")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skip short abstract "
                            f"({len(abstract)} chars): {paper.get('title', '')[:60]}"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            if not has_next:
                print(f"[{self.site_id}] no next page after page {page}; stopping")
                break

            page += 1

        if page >= self._MAX_PAGES:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; saved {saved}")

        print(f"[{self.site_id}] crawl complete: saved {saved}")
        return saved
