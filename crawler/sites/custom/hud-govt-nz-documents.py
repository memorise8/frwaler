# -*- coding: utf-8 -*-
"""Crawler for hud.govt.nz — Statistics and research documents (type 32).

Uses the site's internal AJAX search endpoint (/documents/search) which
returns paginated HTML fragments.  Detail pages are fetched for full body
text when an internal link is present.  Absolute import — loaded via
spec_from_file_location.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_LIST_URL = "https://www.hud.govt.nz/documents/search"
_DOC_TYPE = "32"   # Statistics and research (target URL: _documenttypes[]=32)
_PAGE_SIZE = 12    # server default; start increments by this
_MAX_PAGES = 200   # safety cap
_WALL_BUDGET = 25 * 60  # seconds
_ABSTRACT_MIN = 100     # skip items shorter than this (test asserts >=100)
_PUBLISHER = "Te Tūāpapa Kura Kāinga – Ministry of Housing and Urban Development"


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if not html:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str:
    """Parse various date strings → ISO YYYY-MM-DD, or empty string."""
    if not raw:
        return ""
    s = raw.strip()
    for fmt in ("%d %b %y", "%d %b %Y", "%d %B %Y", "%d %B %y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _original_filename(url: str) -> str:
    """Extract last path segment from URL as the original filename."""
    if not url:
        return ""
    tail = urlparse(url).path.rstrip("/").split("/")[-1].split("?")[0]
    return tail if "." in tail else ""


class HudGovtNzDocumentsCrawler(BaseCrawler):
    site_id = "hud-govt-nz-documents"
    site_name = "Custom: hud-govt-nz-documents"
    base_url = "https://www.hud.govt.nz"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, extra_headers: dict | None = None,
                  retries: int = 3) -> str | None:
        """GET via curl, return decoded text or None after retries."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json, text/html, */*",
            "-H", "Accept-Language: en-NZ,en;q=0.9",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/{retries}): {exc}")
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2  # 1s, 4s, 9s
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List-page helpers
    # ------------------------------------------------------------------

    def _fetch_list(self, start: int = 0) -> tuple[str | None, int | None]:
        """Fetch one list page → (html_content, total_count)."""
        url = (f"{_LIST_URL}?q=&_documenttypes%5B%5D={_DOC_TYPE}"
               f"&start={start}")
        raw = self._curl_get(url, {"X-Requested-With": "XMLHttpRequest"})
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, None

        content = data.get("content") or ""
        sentence = data.get("resultsSentence") or ""
        total = None
        m = re.search(r"of\s+(\d+)", sentence)
        if m:
            total = int(m.group(1))
        return content, total

    def _parse_list_html(self, html: str) -> list[dict]:
        """Parse list HTML fragment → list of raw item dicts."""
        soup = _make_soup(html)
        if not soup:
            return []
        items = []
        for div in soup.find_all("div", class_="search-result"):
            try:
                title_el = div.find(class_="search-result__title-text")
                date_el = div.find(class_="search-result__title-date")
                intro_el = div.find(class_="search-result__intro")

                title = title_el.get_text(strip=True) if title_el else ""
                date_str = date_el.get_text(strip=True) if date_el else ""
                intro = intro_el.get_text(strip=True) if intro_el else ""

                pdf_url = ""
                pdf_a = div.find("a", class_="linked-pages__download")
                if pdf_a and pdf_a.get("href"):
                    href = pdf_a["href"]
                    if href.lower().endswith(".pdf"):
                        pdf_url = urljoin(self.base_url, href)

                detail_url = ""
                detail_a = div.find("a", class_="linked-pages__internal")
                if detail_a and detail_a.get("href"):
                    detail_url = urljoin(self.base_url, detail_a["href"])

                if not title:
                    continue
                items.append({
                    "title": title,
                    "date_str": date_str,
                    "intro": intro,
                    "pdf_url": pdf_url,
                    "detail_url": detail_url,
                })
            except Exception as exc:
                print(f"[{self.site_id}] list-item parse error: {exc}")
        return items

    # ------------------------------------------------------------------
    # Detail-page helpers
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict:
        """Fetch detail page → dict with abstract, published_date, pdf_url, original_filename."""
        raw = self._curl_get(url)
        if not raw:
            return {}

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error for {url}: {exc}")
            return {}
        if not soup:
            return {}

        # Published date from the intro date span
        published_date = ""
        date_el = soup.find(class_="page-header__intro-date")
        if date_el:
            raw_date = re.sub(r"^Last updated:\s*", "", date_el.get_text(strip=True), flags=re.I)
            published_date = _parse_date(raw_date)

        # Intro text (exclude the date span)
        intro_text = ""
        intro_el = soup.find(class_="page-header__intro")
        if intro_el:
            for span in intro_el.find_all(class_="page-header__intro-date"):
                span.decompose()
            intro_text = intro_el.get_text(separator=" ", strip=True)

        # Body content from typography blocks (skip hidden elements like outdated-browser notice)
        body_parts = []
        for el in soup.find_all(class_="typography"):
            if el.has_attr("hidden"):
                continue
            txt = el.get_text(separator=" ", strip=True)
            txt = re.sub(r"\s+", " ", txt)
            if txt and txt not in body_parts:
                body_parts.append(txt)
        body_text = " ".join(body_parts)

        # Combine intro + body
        parts = []
        if intro_text:
            parts.append(intro_text)
        if body_text and body_text.strip() != intro_text.strip():
            parts.append(body_text)
        abstract = "\n\n".join(parts)

        # PDF from detail page — detail pages use document-download--file, not linked-pages__download
        pdf_url = ""
        orig_fn = ""
        pdf_a = soup.find("a", class_="document-download--file")
        if not pdf_a:
            # fallback: any anchor whose href ends with .pdf
            pdf_a = soup.find("a", href=re.compile(r"\.pdf(\?|$)", re.I))
        if pdf_a and pdf_a.get("href"):
            href = pdf_a["href"]
            pdf_url = urljoin(self.base_url, href)
            orig_fn = _original_filename(pdf_url)

        return {
            "abstract": abstract,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "original_filename": orig_fn,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Statistics and research documents from hud.govt.nz.

        Paginates the AJAX search endpoint, fetches detail pages for full
        abstracts, and persists via self._save_paper().
        """
        start_ts = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        saved = 0
        seen_urls: set[str] = set()
        total_known: int | None = None
        page_num = 0
        page_offset = 0

        while True:
            # Wall-clock budget
            if time.time() - start_ts > _WALL_BUDGET:
                print(f"[{self.site_id}] Wall-clock budget ({_WALL_BUDGET}s) reached. Stopping.")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page_num >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Fetch list page with retry
            content_html = None
            total = None
            for attempt in range(3):
                content_html, total = self._fetch_list(page_offset)
                if content_html:
                    break
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] list fetch failed at offset {page_offset}, "
                          f"retry {attempt+1}/3 in {wait}s...")
                    time.sleep(wait)

            if total_known is None and total is not None:
                total_known = total
                print(f"[{self.site_id}] Total documents: {total_known}")

            if not content_html:
                print(f"[{self.site_id}] No content at offset {page_offset}. Stopping.")
                break

            items = self._parse_list_html(content_html)
            if not items:
                print(f"[{self.site_id}] No items at offset {page_offset}. Done.")
                break

            # Deduplicate
            new_items = []
            for item in items:
                key = item.get("detail_url") or item.get("pdf_url") or item.get("title", "")
                if key and key not in seen_urls:
                    seen_urls.add(key)
                    new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] All items already seen at offset {page_offset}. Stopping.")
                break

            page_num += 1
            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_label}")

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                title = item["title"]
                date_str = item["date_str"]
                intro = item["intro"]
                list_pdf_url = item.get("pdf_url", "")
                detail_url = item.get("detail_url", "")

                # Slug = external_id / post_number
                if detail_url:
                    slug = urlparse(detail_url).path.rstrip("/").split("/")[-1]
                elif list_pdf_url:
                    fn = _original_filename(list_pdf_url)
                    slug = fn[:-4] if fn.lower().endswith(".pdf") else fn
                    if not slug:
                        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                else:
                    slug = re.sub(r"[^a-z0-9]+", "-",
                                  title.lower()).strip("-")

                listed_date = _parse_date(date_str)

                try:
                    abstract = ""
                    published_date = listed_date
                    pdf_url = list_pdf_url
                    original_filename = _original_filename(list_pdf_url)

                    if detail_url:
                        time.sleep(self._delay)
                        det = self._fetch_detail(detail_url)
                        if det.get("abstract"):
                            abstract = det["abstract"]
                        if det.get("published_date"):
                            published_date = det["published_date"]
                        if det.get("pdf_url"):
                            pdf_url = det["pdf_url"]
                        if det.get("original_filename"):
                            original_filename = det["original_filename"]

                    # Fall back to intro if detail didn't yield an abstract
                    if not abstract:
                        abstract = intro

                    if len(abstract) < _ABSTRACT_MIN:
                        print(f"[{self.site_id}] Skipping (abstract {len(abstract)} chars < "
                              f"{_ABSTRACT_MIN}): {title[:50]}")
                        continue

                    if not original_filename and pdf_url:
                        original_filename = _original_filename(pdf_url)

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": detail_url or f"{self.base_url}/documents",
                        "pdf_url": pdf_url or None,
                        "original_filename": original_filename or None,
                        "publisher": _PUBLISHER,
                        "authors": None,
                        "keywords": None,
                        "category": "Statistics and research",
                        "doi": None,
                        "metadata": json.dumps({
                            "posted_date": date_str,
                            "originalFilename": original_filename or None,
                            "document_type": "Statistics and research",
                        }, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{title[:40]}' failed: {exc}")
                    continue

            page_offset += _PAGE_SIZE

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
