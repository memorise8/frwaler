# -*- coding: utf-8 -*-
"""EHESP press releases crawler.

List source:
    https://www.ehesp.fr/wp-json/wp/v2/pages/26058

The public page is a WordPress page whose rendered body contains month
headings and direct PDF links.  Each PDF is also a WordPress media attachment;
the media REST endpoint is used as the per-item metadata/detail endpoint when
available, and the PDF text is extracted for the abstract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

# absolute import -- spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler


_SITE_ID = "ehesp-fr-espace-presse"
_BASE_URL = "https://www.ehesp.fr"
_START_URL = f"{_BASE_URL}/espace-presse/communiques-de-presse/"
_LIST_API_URL = (
    f"{_BASE_URL}/wp-json/wp/v2/pages/26058"
    "?_fields=id,slug,link,date,modified,title,content"
)
_MEDIA_FIELDS = (
    "id,date,modified,slug,source_url,title,caption,description,"
    "media_details,mime_type,link"
)
_MAX_PAGES = 200
_BUDGET_SECONDS = 25 * 60
_RETRY_DELAYS = (1, 3, 9)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_MONTHS = {
    "janvier": "01",
    "fevrier": "02",
    "mars": "03",
    "avril": "04",
    "mai": "05",
    "juin": "06",
    "juillet": "07",
    "aout": "08",
    "septembre": "09",
    "octobre": "10",
    "novembre": "11",
    "decembre": "12",
}


def _fold_text(value):
    """Lowercase and strip accents for date matching."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return ascii_text.lower()


def _clean_text(value):
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value.replace("\x00", " "))
    return value.strip()


def _json_dumps(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _soup(raw_html):
    """Parse HTML with html5lib -> lxml -> html.parser fallback chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw_html or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
            continue
    return None


def _parse_month_label(raw):
    """Parse 'Avril 2026' -> ('2026-04-01', 'Avril 2026')."""
    label = _clean_text(raw)
    folded = _fold_text(label)
    match = re.search(r"\b([a-z]+)\s+(\d{4})\b", folded)
    if not match:
        return None, label
    month = _MONTHS.get(match.group(1))
    if not month:
        return None, label
    return f"{match.group(2)}-{month}-01", label


def _parse_french_date(raw):
    """Parse French dates with a day, e.g. 'mercredi 29 avril 2026'."""
    folded = _fold_text(raw)
    match = re.search(
        r"\b(\d{1,2})(?:er)?\s+"
        r"(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|"
        r"octobre|novembre|decembre)\s+(\d{4})\b",
        folded,
    )
    if not match:
        return None
    month = _MONTHS.get(match.group(2))
    if not month:
        return None
    return f"{match.group(3)}-{month}-{int(match.group(1)):02d}"


def _date_from_wp_datetime(raw):
    if not raw:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
    if match:
        return match.group(0)
    return None


def _filename_from_url(url):
    path = urllib.parse.urlparse(url or "").path
    name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return name or None


def _slug_from_filename(filename):
    if not filename:
        return None
    stem = os.path.splitext(filename)[0]
    folded = _fold_text(stem)
    folded = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return folded or stem


def _normalize_url(url):
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/._-")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


class EhespFrEspacePresseCrawler(BaseCrawler):
    site_id = "ehesp-fr-espace-presse"
    site_name = "Custom: ehesp-fr-espace-presse"
    base_url = "https://www.ehesp.fr"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self._media_cache = {}

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_bytes(self, url, *, accept=None, max_time=60, retries=3):
        headers = [
            "-H",
            f"User-Agent: {_USER_AGENT}",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        ]
        if accept:
            headers.extend(["-H", f"Accept: {accept}"])

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skLf",
            "--max-time",
            str(max_time),
            *headers,
            url,
        ]

        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl attempt {attempt + 1}/{retries} "
                    f"failed for {url}: rc={result.returncode} {stderr[:200]}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl attempt {attempt + 1}/{retries} "
                    f"failed for {url}: {exc}"
                )

            if attempt < retries - 1:
                time.sleep(_RETRY_DELAYS[attempt])

        print(f"[{self.site_id}] curl failed after {retries} attempts: {url}")
        return None

    def _curl_text(self, url, *, accept=None, max_time=60, retries=3):
        raw = self._curl_bytes(
            url, accept=accept, max_time=max_time, retries=retries
        )
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")

    def _fetch_json(self, url):
        raw = self._curl_text(url, accept="application/json", max_time=45)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] JSON parse failed for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        """Return (items, has_next) for the WordPress page list."""
        if page != 1:
            # The public /page/2/ URL repeats the same static page content.
            # Treat the REST page as the complete one-page list.
            return [], False

        data = self._fetch_json(_LIST_API_URL)
        if data:
            content = ((data.get("content") or {}).get("rendered")) or ""
            page_meta = {
                "wp_page_id": data.get("id"),
                "wp_page_slug": data.get("slug"),
                "wp_page_link": data.get("link"),
                "wp_page_date": data.get("date"),
                "wp_page_modified": data.get("modified"),
                "list_api_url": _LIST_API_URL,
            }
            return self._parse_list_html(content, page_meta), False

        # Fallback to public HTML if the REST endpoint is temporarily down.
        raw = self._curl_text(_START_URL, accept="text/html", max_time=45)
        if not raw:
            return [], False
        page_meta = {
            "wp_page_id": 26058,
            "wp_page_link": _START_URL,
            "list_api_url": _LIST_API_URL,
            "fallback_source": "public_html",
        }
        return self._parse_list_html(raw, page_meta), False

    def _parse_list_html(self, raw_html, page_meta):
        soup = _soup(raw_html)
        if soup is None:
            return []

        body = soup.find("body") or soup
        items = []
        current_listed_date = None
        current_listed_raw = None

        for element in body.find_all(["h4", "li", "p"]):
            if element.name == "h4":
                parsed_date, raw_label = _parse_month_label(element.get_text(" ", strip=True))
                if parsed_date:
                    current_listed_date = parsed_date
                    current_listed_raw = raw_label
                continue

            for anchor in element.find_all("a", href=True):
                href = anchor.get("href") or ""
                if ".pdf" not in href.lower():
                    continue

                pdf_url = urllib.parse.urljoin(self.base_url, href)
                title = _clean_text(anchor.get_text(" ", strip=True))
                original_filename = _filename_from_url(pdf_url)
                if not title:
                    title = os.path.splitext(original_filename or "document")[0]

                category = "Dossier de presse" if "dossier de presse" in _fold_text(title) else "Communique de presse"
                items.append(
                    {
                        "title": title,
                        "pdf_url": pdf_url,
                        "listed_date": current_listed_date,
                        "listed_date_raw": current_listed_raw,
                        "original_filename": original_filename,
                        "slug": _slug_from_filename(original_filename),
                        "category": category,
                        "page_meta": dict(page_meta),
                    }
                )

        return items

    # ------------------------------------------------------------------
    # Detail/media/PDF parsing
    # ------------------------------------------------------------------

    def _fetch_media_meta(self, item):
        pdf_url = item.get("pdf_url")
        if not pdf_url:
            return None
        if pdf_url in self._media_cache:
            return self._media_cache[pdf_url]

        filename = item.get("original_filename") or _filename_from_url(pdf_url)
        stem = os.path.splitext(filename or "")[0]
        queries = [stem, item.get("slug") or ""]
        seen_queries = set()

        for query in queries:
            query = (query or "").strip()
            if not query or query in seen_queries:
                continue
            seen_queries.add(query)
            url = (
                f"{self.base_url}/wp-json/wp/v2/media"
                f"?search={urllib.parse.quote(query)}"
                f"&per_page=20&_fields={urllib.parse.quote(_MEDIA_FIELDS)}"
            )
            data = self._fetch_json(url)
            if not isinstance(data, list):
                continue

            normalized_pdf = _normalize_url(pdf_url)
            best = None
            for media in data:
                if _normalize_url(media.get("source_url")) == normalized_pdf:
                    best = media
                    break
            if best is None and data:
                best = data[0]
            if best:
                best = dict(best)
                best["media_search_api_url"] = url
                self._media_cache[pdf_url] = best
                return best

        self._media_cache[pdf_url] = None
        return None

    def _extract_pdf_text(self, pdf_bytes, pdf_url):
        try:
            result = subprocess.run(
                ["pdftotext", "-", "-"],
                input=pdf_bytes,
                capture_output=True,
                timeout=60,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext failed for {pdf_url}: {exc}")
            return ""

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            print(f"[{self.site_id}] pdftotext rc={result.returncode} for {pdf_url}: {stderr[:200]}")
            return ""
        return result.stdout.decode("utf-8", errors="replace")

    def _pdf_text_for_item(self, item):
        pdf_url = item.get("pdf_url")
        if not pdf_url:
            return ""
        pdf_bytes = self._curl_bytes(
            pdf_url,
            accept="application/pdf,*/*;q=0.8",
            max_time=75,
            retries=3,
        )
        if not pdf_bytes:
            return ""
        return self._extract_pdf_text(pdf_bytes, pdf_url)

    def _process_item(self, item):
        time.sleep(self._delay)

        media = self._fetch_media_meta(item) or {}
        media_pdf_url = media.get("source_url")
        if media_pdf_url and _normalize_url(media_pdf_url) == _normalize_url(item["pdf_url"]):
            item = dict(item)
            item["pdf_url"] = media_pdf_url

        pdf_text = self._pdf_text_for_item(item)
        abstract = _clean_text(pdf_text)
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] abstract too short ({len(abstract)} chars), "
                f"skipping: {item.get('title', '')[:80]}"
            )
            return 0

        original_filename = item.get("original_filename") or _filename_from_url(item["pdf_url"])
        media_id = media.get("id")
        external_id = str(media_id) if media_id is not None else (item.get("slug") or original_filename)
        post_number = str(media_id) if media_id is not None else item.get("slug")
        detail_url = media.get("link") or item["pdf_url"]
        published_date = (
            _parse_french_date(pdf_text[:2000])
            or _date_from_wp_datetime(media.get("date"))
            or item.get("listed_date")
        )

        media_title = media.get("title") or {}
        if isinstance(media_title, dict):
            media_title = media_title.get("rendered")
        metadata = {
            "posted_date": item.get("listed_date_raw"),
            "originalFilename": original_filename,
            "attachment_id": media_id,
            "media_slug": media.get("slug"),
            "media_title": _clean_text(media_title),
            "media_date": media.get("date"),
            "media_modified": media.get("modified"),
            "media_mime_type": media.get("mime_type"),
            "media_details": media.get("media_details"),
            "media_search_api_url": media.get("media_search_api_url"),
            "wp_page_id": (item.get("page_meta") or {}).get("wp_page_id"),
            "wp_page_slug": (item.get("page_meta") or {}).get("wp_page_slug"),
            "wp_page_link": (item.get("page_meta") or {}).get("wp_page_link"),
            "wp_page_date": (item.get("page_meta") or {}).get("wp_page_date"),
            "wp_page_modified": (item.get("page_meta") or {}).get("wp_page_modified"),
            "list_api_url": (item.get("page_meta") or {}).get("list_api_url"),
            "list_page_url": _START_URL,
            "listed_date": item.get("listed_date"),
            "published_date_raw": pdf_text[:200].replace("\n", " "),
            "post_number": post_number,
            "source_url": item.get("pdf_url"),
            "category": item.get("category"),
            "department": "Direction de la communication",
            "pdf_text_chars": len(abstract),
        }

        paper = {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": item.get("title"),
            "abstract": abstract[:12000],
            "published_date": published_date,
            "listed_date": item.get("listed_date"),
            "posted_date": item.get("listed_date"),
            "authors": None,
            "publisher": "EHESP",
            "department": "Direction de la communication",
            "journal": None,
            "url": detail_url,
            "pdf_url": item.get("pdf_url"),
            "keywords": "EHESP, communique de presse",
            "category": item.get("category"),
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

        self._save_paper(paper)
        print(f"[{self.site_id}] saved: {paper['title'][:80]}")
        return 1

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] done: saved=0")
            return 0

        start_time = time.time()
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time >= _BUDGET_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}, stopping")
                break

            try:
                items, has_next = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} failed: {exc}")
                break

            if not items:
                print(f"[{self.site_id}] page {page}: no items, stopping")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            new_items = [item for item in items if item.get("pdf_url") not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page}: all items already seen, stopping")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _BUDGET_SECONDS:
                    print(f"[{self.site_id}] 25-minute budget reached during page {page}, stopping")
                    break

                url = item.get("pdf_url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    saved += self._process_item(item)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not has_next:
                print(f"[{self.site_id}] page {page}: last page, stopping")
                break

        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

        print(f"[{self.site_id}] done: saved={saved}")
        return saved
