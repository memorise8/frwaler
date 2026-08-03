# -*- coding: utf-8 -*-
"""Crawler for libreria.educacion.gob.es Informes y estadisticas.

Discovered live surfaces:
  Start/showcase: /p/3740_estadisticas/
  Series list   : /lote/{lote_id}/
  Detail        : /libro/{slug}_{book_id}/
  PDF download  : /ebook/{edition_id}/free_download/

The site renders the useful list/detail data as HTML.  A small JSON API exists
for alternate editions/prices, but not for the statistics lists.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from email.header import decode_header
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_SITE_ID = "libreria-educacion-gob-es-p"
_BASE_URL = "https://www.libreria.educacion.gob.es"
_START_URL = f"{_BASE_URL}/p/3740_estadisticas/"


def _make_soup(raw):
    """BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
            continue
    return None


def _clean_text(value):
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text


def _node_text(node):
    if node is None:
        return ""
    try:
        return _clean_text(node.get_text(" ", strip=True))
    except Exception:
        return ""


def _meta_content(soup, *, name=None, prop=None, itemprop=None):
    if soup is None:
        return ""
    attrs = {}
    if name:
        attrs["name"] = name
    if prop:
        attrs["property"] = prop
    if itemprop:
        attrs["itemprop"] = itemprop
    node = soup.find("meta", attrs=attrs)
    if node is not None:
        return _clean_text(node.get("content"))
    return ""


def _iso_date(value):
    raw = _clean_text(value)
    if not raw:
        return None

    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    match = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", raw)
    if match:
        return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"

    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if match:
        return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"

    return None


def _absolute_url(href):
    if not href:
        return None
    return urljoin(_BASE_URL, href)


def _book_id_from_url(url):
    match = re.search(r"_(\d+)/?$", urlparse(url).path)
    return match.group(1) if match else None


def _edition_id_from_url(url):
    if not url:
        return None
    match = re.search(r"/ebook/(\d+)/free_download/?", urlparse(url).path)
    return match.group(1) if match else None


def _filename_from_url(url):
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if "." in tail and len(tail) <= 220:
        return tail
    return None


def _filename_from_content_disposition(headers_text):
    if not headers_text:
        return None

    match = re.search(r"filename\*=([^;\r\n]+)", headers_text, flags=re.I)
    if match:
        value = match.group(1).strip().strip('"')
        if "'" in value:
            value = value.split("'", 2)[-1]
        value = unquote(value)
        return _clean_text(value) or None

    match = re.search(r"filename=([^;\r\n]+)", headers_text, flags=re.I)
    if not match:
        return None

    raw = match.group(1).strip().strip('"')
    try:
        decoded_parts = []
        for part, encoding in decode_header(raw):
            if isinstance(part, bytes):
                decoded_parts.append(part.decode(encoding or "utf-8", errors="replace"))
            else:
                decoded_parts.append(part)
        return _clean_text("".join(decoded_parts)) or None
    except Exception:
        return _clean_text(unquote(raw)) or None


def _split_people(text):
    value = _clean_text(text)
    if not value:
        return None
    if ";" in value:
        parts = [p.strip() for p in value.split(";")]
    else:
        parts = [p.strip() for p in value.split(",")]
    parts = [p for p in parts if p]
    return "; ".join(parts) if parts else value


class LibreriaEducacionGobEsPCrawler(BaseCrawler):
    site_id = "libreria-educacion-gob-es-p"
    site_name = "Custom: libreria-educacion-gob-es-p"
    base_url = "https://www.libreria.educacion.gob.es"

    _START_URL = _START_URL
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: es-ES,es;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)
        return self._run_curl(cmd, url, timeout=timeout + 10)

    def _curl_head(self, url, *, referer=None, timeout=30):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skLI",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: application/pdf,application/octet-stream,*/*",
            "-H",
            "Accept-Language: es-ES,es;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)
        return self._run_curl(cmd, url, timeout=timeout + 10)

    def _run_curl(self, cmd, url, *, timeout):
        waits = [1, 3, 9]
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout)
                body = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                if body:
                    return body
                last_error = f"curl exit={result.returncode} stderr={stderr[:200]}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                print(
                    f"[{self.site_id}] curl error attempt {attempt + 1}/3 for "
                    f"{url}: {last_error}; retry in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html, page_url):
        soup = _make_soup(html)
        if soup is None:
            return [], [], None

        main = (
            soup.select_one(".showcase-content")
            or soup.select_one("article.showcase")
            or soup.select_one("section.subjects")
            or soup.select_one("section.page-content")
            or soup
        )
        list_title = _node_text(main.find("h1")) or _meta_content(soup, prop="og:title")

        list_links = []
        book_items = []

        for anchor in main.select("a[href]"):
            href = anchor.get("href") or ""
            url = _absolute_url(href)
            if not url:
                continue
            path = urlparse(url).path

            if re.search(r"/libro/[^/]+_\d+/?$", path):
                li = anchor.find_parent("li", class_="book")
                title = _node_text(anchor)
                synopsis = ""
                author = ""
                if li is not None:
                    title = (
                        _node_text(li.select_one(".book-title a"))
                        or _clean_text(anchor.get("title"))
                        or _clean_text(anchor.get("alt"))
                        or title
                    )
                    synopsis = _node_text(li.select_one(".book-synopsis"))
                    author = _node_text(li.select_one(".book-author"))
                book_items.append(
                    {
                        "url": url,
                        "title": title,
                        "list_synopsis": synopsis,
                        "list_author": author,
                        "list_url": page_url,
                        "series": list_title,
                    }
                )
                continue

            if re.search(r"/lote/\d+/?$", path):
                list_links.append(url)
                continue

            # Some showcase entries point to another collection page rather
            # than a lote; keep those under the same domain/content tree.
            if re.search(r"/p/\d+_[^/]+/?$", path) and url != page_url:
                list_links.append(url)

        next_url = self._find_next_url(main, page_url)
        if next_url:
            list_links.append(next_url)

        return self._dedupe_dicts(book_items, "url"), self._dedupe_values(list_links), list_title

    def _find_next_url(self, main, page_url):
        candidates = []
        for selector in (
            'a[rel="next"]',
            ".pagination a",
            ".paginator a",
            "ul.pager a",
            "a.next",
            "a.siguiente",
        ):
            candidates.extend(main.select(selector))

        for anchor in candidates:
            text = _node_text(anchor).lower()
            classes = " ".join(anchor.get("class") or []).lower()
            rel = " ".join(anchor.get("rel") or []).lower()
            if (
                "next" in rel
                or "next" in classes
                or "siguiente" in classes
                or "siguiente" in text
                or text in {">", ">", "»", "›"}
            ):
                next_url = _absolute_url(anchor.get("href"))
                if next_url and next_url != page_url:
                    return next_url
        return None

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html, detail_url, list_item):
        soup = _make_soup(html)
        if soup is None:
            raise ValueError("detail HTML could not be parsed")

        title = (
            _node_text(soup.select_one("h1.book-title"))
            or _meta_content(soup, prop="og:title")
            or list_item.get("title")
        )
        abstract = (
            _node_text(soup.select_one(".book-synopsis[itemprop='description']"))
            or _node_text(soup.select_one(".book-synopsis"))
            or _meta_content(soup, prop="og:description")
            or _meta_content(soup, name="description")
            or list_item.get("list_synopsis")
            or ""
        )

        definitions = self._parse_definitions(soup)
        subjects = self._extract_subjects(soup, definitions)
        publisher = definitions.get("Editorial") or definitions.get("editorial")
        raw_date = (
            definitions.get("Fecha publicación")
            or _meta_content(soup, prop="books:release_date")
            or _meta_content(soup, itemprop="datePublished")
        )
        published_date = _iso_date(raw_date)
        listed_date = published_date

        authors = (
            _node_text(soup.select_one(".book-author"))
            or list_item.get("list_author")
            or None
        )

        pdf_url = None
        download = soup.select_one('a[href*="/ebook/"][href*="/free_download"]')
        if download is not None:
            pdf_url = _absolute_url(download.get("href"))

        book_id = _book_id_from_url(detail_url)
        edition_id = _edition_id_from_url(pdf_url)
        original_filename = _filename_from_url(pdf_url)
        if pdf_url:
            headers = self._curl_head(pdf_url, referer=detail_url)
            original_filename = (
                _filename_from_content_disposition(headers)
                or original_filename
            )

        isbn = _meta_content(soup, prop="books:isbn") or definitions.get("EAN")
        pages = _meta_content(soup, prop="books:page_count") or definitions.get("Páginas")
        language = _meta_content(soup, prop="books:language") or definitions.get("Idioma")
        legal_deposit = definitions.get("Depósito legal")
        category = ", ".join(subjects) if subjects else None

        metadata = {
            "posted_date": raw_date or listed_date,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "book_id": book_id,
            "edition_id": edition_id,
            "isbn": isbn,
            "ean": definitions.get("EAN"),
            "legal_deposit": legal_deposit,
            "pages": pages,
            "language": language,
            "subjects": subjects,
            "imprint": publisher,
            "series": list_item.get("series"),
            "volume": None,
            "issue": None,
            "journal_raw": None,
            "list_url": list_item.get("list_url"),
            "list_title": list_item.get("series"),
            "list_synopsis": list_item.get("list_synopsis"),
            "detail_url": detail_url,
            "pdf_download_endpoint": pdf_url,
            "definitions": definitions,
            "og_title": _meta_content(soup, prop="og:title"),
            "og_description": _meta_content(soup, prop="og:description"),
            "release_date": _meta_content(soup, prop="books:release_date"),
        }

        external_id = book_id or edition_id or urlparse(detail_url).path.strip("/")

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": book_id or edition_id or external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": _split_people(authors),
            "publisher": publisher,
            "department": publisher,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": category,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_definitions(self, soup):
        result = {}
        dl = soup.select_one(".book-definitions dl")
        if dl is None:
            return result

        current_key = None
        for child in dl.find_all(["dt", "dd"], recursive=False):
            name = getattr(child, "name", "")
            if name == "dt":
                current_key = _node_text(child)
            elif name == "dd" and current_key:
                value = _node_text(child)
                if value:
                    result[current_key] = value
                current_key = None
        return result

    def _extract_subjects(self, soup, definitions):
        subjects = []
        for node in soup.select(".book-definitions dd.subject a"):
            value = _node_text(node)
            if value:
                subjects.append(value)
        if not subjects and definitions.get("Materia"):
            subjects = [p.strip() for p in definitions["Materia"].split(",") if p.strip()]
        return self._dedupe_values(subjects)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        list_queue = [self._START_URL]
        seen_list_pages = set()
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        try:
            while list_queue:
                if time.time() - start_time > self._WALL_SECONDS - 60:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    break
                if limit is not None and saved >= limit:
                    break
                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                    break

                list_url = list_queue.pop(0)
                if list_url in seen_list_pages:
                    continue
                seen_list_pages.add(list_url)

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                html = self._curl_get(list_url)
                if not html:
                    print(f"[{self.site_id}] page {page}: empty/failed list response {list_url}")
                    page += 1
                    continue

                items, new_list_links, list_title = self._parse_list_page(html, list_url)
                for next_list_url in new_list_links:
                    if next_list_url not in seen_list_pages and next_list_url not in list_queue:
                        list_queue.append(next_list_url)

                new_items = []
                for item in items:
                    detail_url = item.get("url")
                    if not detail_url or detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_items.append(item)

                if not new_items:
                    print(f"[{self.site_id}] page {page}: 0 new records ({list_title or list_url})")

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - start_time > self._WALL_SECONDS - 60:
                        print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                        break

                    detail_url = item.get("url")
                    item_label = _book_id_from_url(detail_url or "") or detail_url or "unknown"
                    try:
                        time.sleep(float(getattr(self, "_delay", 1.0) or 0))
                        detail_html = self._curl_get(detail_url, referer=list_url)
                        if not detail_html:
                            print(f"[{self.site_id}] item {item_label} failed: empty detail response")
                            continue

                        paper = self._parse_detail(detail_html, detail_url, item)
                        abstract_len = len(paper.get("abstract") or "")
                        if abstract_len < 50:
                            print(
                                f"[{self.site_id}] item {item_label} skipped: "
                                f"abstract too short ({abstract_len} chars)"
                            )
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper['title'][:80]}")
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                page += 1
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    @staticmethod
    def _dedupe_values(values):
        seen = set()
        result = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @staticmethod
    def _dedupe_dicts(values, key):
        seen = set()
        result = []
        for value in values:
            marker = value.get(key)
            if not marker or marker in seen:
                continue
            seen.add(marker)
            result.append(value)
        return result
