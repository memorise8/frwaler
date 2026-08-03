# -*- coding: utf-8 -*-
"""Crawler for agri.ee / ministeerium-uudised-kontakt / uuringud.

The requested page is a Drupal/VPortal HTML page. Its document lists are not
served by the public search API; the real list payload is embedded in
``script[type="application/json"][id^="datatable-"]`` blocks next to the
visible DataTables.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_SITE_ID = "agri-ee-ministeerium-uudised"


class AgriEeMinisteeriumUudisedCrawler(BaseCrawler):
    site_id = "agri-ee-ministeerium-uudised"
    site_name = "Custom: agri-ee-ministeerium-uudised"
    base_url = "https://www.agri.ee"

    START_URL = "https://www.agri.ee/ministeerium-uudised-kontakt/uuringud"
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    CURL_USER_AGENT = "Mozilla/5.0"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl all study rows embedded in the agri.ee Uuringud page."""
        saved = 0
        seen_urls = set()
        page_url = self.START_URL
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        print(f"[{_SITE_ID}] starting crawl (limit={limit_or_inf})")
        print(f"[{_SITE_ID}] list endpoint: {self.START_URL} (HTML datatable JSON)")
        print(f"[{_SITE_ID}] detail endpoint: row file/link HEAD; metadata page is source HTML")

        time_budget_reached = False
        for page in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed > self.WALL_BUDGET_SECONDS:
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page}; stopping")
                break

            if page % 10 == 0 or page == 1:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            raw = self._curl_text(page_url, context=f"list page {page}", referer=self.base_url)
            if not raw:
                print(f"[{_SITE_ID}] page {page}: empty list response; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{_SITE_ID}] page {page}: HTML parse failed; stopping")
                break

            records = self._parse_records(soup)
            if not records:
                print(f"[{_SITE_ID}] page {page}: 0 records; stopping")
                break

            page_had_new = False
            for item_index, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break
                elapsed = time.monotonic() - start_time
                if elapsed > self.WALL_BUDGET_SECONDS:
                    print(f"[{_SITE_ID}] 25-minute budget reached during page {page}; stopping")
                    time_budget_reached = True
                    break

                item_url = record.get("item_url") or ""
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                page_had_new = True

                try:
                    time.sleep(self.detail_delay)
                    headers = self._curl_headers(
                        item_url,
                        context=f"item {item_index} HEAD",
                        referer=page_url,
                    )
                    if headers is None:
                        print(f"[{_SITE_ID}] item {item_index} failed: metadata HEAD failed")
                        continue

                    paper = self._record_to_paper(record, headers, page_url)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{_SITE_ID}] item {item_index} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    label = record.get("title") or item_url or f"item {item_index}"
                    print(f"[{_SITE_ID}] item {label[:80]} failed: {exc}")
                    continue

            if time_budget_reached:
                break

            if not page_had_new:
                print(f"[{_SITE_ID}] page {page}: all URLs already seen; stopping")
                break

            next_url = self._next_page_url(soup, page_url)
            if not next_url:
                print(f"[{_SITE_ID}] page {page}: next page link absent; pagination complete")
                break
            page_url = next_url

            if page == self.MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {self.MAX_PAGES} pages reached; stopping")

        print(f"[{_SITE_ID}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_text(self, url, *, context, referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-H",
            f"User-Agent: {self.CURL_USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: et,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        body, _headers = self._run_curl(cmd, context=context)
        return body

    def _curl_headers(self, url, *, context, referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skLI",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-H",
            f"User-Agent: {self.CURL_USER_AGENT}",
            "-H",
            "Accept: application/pdf,text/html,*/*;q=0.8",
            "-H",
            "Accept-Language: et,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        body, headers = self._run_curl(cmd, context=context, headers_only=True)
        if headers:
            return headers
        if body:
            return self._parse_header_blocks(body)
        return None

    def _run_curl(self, cmd, *, context, headers_only=False):
        last_error = "unknown error"
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                    check=False,
                )
                text = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and text.strip():
                    if headers_only:
                        return text, self._parse_header_blocks(text)
                    return text, {}
                last_error = f"curl exit={result.returncode} stderr={stderr[:200]}"
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{_SITE_ID}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] {context} failed after 3 attempts: {last_error}")
        return None, None

    @staticmethod
    def _parse_header_blocks(raw_headers):
        blocks = re.split(r"\r?\n\r?\n", raw_headers.strip())
        parsed = {}
        for block in blocks:
            current = {}
            for line in block.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                current[key.strip().lower()] = value.strip()
            if current:
                parsed = current
        return parsed or None

    # ------------------------------------------------------------------
    # HTML/list parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(raw, *, context):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _parse_records(self, soup):
        records = []
        for script in soup.find_all("script", {"type": "application/json"}):
            datatable_id = script.get("id") or ""
            if not datatable_id.startswith("datatable-") or datatable_id.endswith("-options"):
                continue

            try:
                rows = json.loads(script.string or "[]")
            except Exception as exc:
                print(f"[{_SITE_ID}] datatable {datatable_id} JSON failed: {exc}")
                continue
            if not isinstance(rows, list):
                continue

            section = self._section_for_datatable(soup, datatable_id)
            for row_index, row in enumerate(rows, start=1):
                if not isinstance(row, list) or len(row) < 2:
                    continue
                try:
                    parsed = self._parse_row(row, datatable_id, row_index, section)
                except Exception as exc:
                    print(f"[{_SITE_ID}] row {datatable_id}:{row_index} failed: {exc}")
                    continue
                if parsed:
                    records.append(parsed)
        return records

    def _section_for_datatable(self, soup, datatable_id):
        table = soup.find("table", attrs={"data-json-id": datatable_id})
        pane = table.find_parent("div", attrs={"role": "tabpanel"}) if table else None
        section_title = ""
        section_anchor = ""
        if pane:
            section_anchor = pane.get("id") or ""
            title_el = pane.find(
                "div",
                id=lambda value: bool(value and value.startswith("accordion-title--")),
            )
            if title_el:
                section_title = self._clean(title_el.get_text(" ", strip=True))

        download_all_url = None
        if table:
            block = table.find_parent("div", class_=lambda value: value and "document_blocks" in value)
            if block:
                link = block.find("a", class_=lambda value: value and "js-documents-download" in value)
                if link and link.get("href"):
                    download_all_url = urljoin(self.base_url, link["href"])

        return {
            "section": section_title or "Uuringud",
            "section_anchor": section_anchor or "uuringud",
            "download_all_url": download_all_url,
        }

    def _parse_row(self, row, datatable_id, row_index, section):
        title_cell = self._make_soup(row[0], context=f"{datatable_id}:{row_index} title cell")
        date_cell = self._make_soup(row[1], context=f"{datatable_id}:{row_index} date cell")
        action_cell = self._make_soup(row[2], context=f"{datatable_id}:{row_index} action cell") if len(row) > 2 else None
        if title_cell is None or date_cell is None:
            return None

        title_link = title_cell.find("a", href=True)
        action_link = action_cell.find("a", href=True) if action_cell else None
        link = title_link or action_link
        if not link:
            return None

        item_url = urljoin(self.base_url, link.get("href") or "")
        title = self._clean(link.get_text(" ", strip=True))
        if not title:
            return None

        time_el = date_cell.find("time")
        date_raw = self._clean(time_el.get_text(" ", strip=True)) if time_el else self._clean(date_cell.get_text(" ", strip=True))
        datetime_raw = time_el.get("datetime") if time_el else ""
        listed_date = self._iso_date(datetime_raw) or self._date_from_display(date_raw)

        info_text = self._clean(title_cell.get_text(" ", strip=True))
        file_size = None
        file_type = None
        info_tail = info_text.replace(title, "", 1).strip()
        match = re.search(r"\|\s*([^|]+?)\s*\|\s*([A-Za-z0-9]+)\s*$", info_tail)
        if match:
            file_size = self._clean(match.group(1))
            file_type = self._clean(match.group(2)).lower()

        original_filename = self._filename_from_url(item_url) if self._is_pdf_url(item_url, file_type) else None
        native_slug = self._slug_from_url(item_url) or f"{datatable_id}-{row_index}"

        return {
            "title": title,
            "item_url": item_url,
            "pdf_url": item_url if self._is_pdf_url(item_url, file_type) else None,
            "listed_date": listed_date,
            "posted_date_raw": date_raw,
            "listed_datetime_raw": datetime_raw,
            "file_size": file_size,
            "file_type": file_type,
            "original_filename": original_filename,
            "native_slug": native_slug,
            "datatable_id": datatable_id,
            "row_index": row_index,
            "row_html": row,
            **section,
        }

    def _record_to_paper(self, record, headers, source_url):
        title = record.get("title") or ""
        listed_date = record.get("listed_date")
        original_filename = self._filename_from_content_disposition(headers) or record.get("original_filename")
        item_url = record.get("item_url") or ""
        pdf_url = record.get("pdf_url")
        native_slug = record.get("native_slug") or self._slug_from_url(item_url)
        external_id = native_slug or item_url
        content_type = headers.get("content-type") if headers else None
        content_length = headers.get("content-length") if headers else None
        last_modified = headers.get("last-modified") if headers else None
        section = record.get("section") or "Uuringud"

        abstract = self._build_abstract(
            title=title,
            section=section,
            listed_date=listed_date,
            item_url=item_url,
            original_filename=original_filename,
            file_size=record.get("file_size"),
            file_type=record.get("file_type"),
            content_type=content_type,
        )

        keywords = ", ".join(
            part
            for part in ["uuringud", section, record.get("file_type")]
            if part
        )

        detail_url = f"{source_url}#{record.get('section_anchor') or record.get('datatable_id')}"
        metadata = {
            "source": "Drupal/VPortal embedded datatable JSON",
            "start_url": self.START_URL,
            "list_endpoint": self.START_URL,
            "detail_endpoint": item_url,
            "detail_method": "HEAD",
            "datatable_id": record.get("datatable_id"),
            "row_index": record.get("row_index"),
            "section": section,
            "section_anchor": record.get("section_anchor"),
            "download_all_url": record.get("download_all_url"),
            "posted_date": record.get("posted_date_raw"),
            "listed_date": listed_date,
            "listed_datetime": record.get("listed_datetime_raw"),
            "native_slug": native_slug,
            "originalFilename": original_filename,
            "file_size": record.get("file_size"),
            "file_type": record.get("file_type"),
            "content_type": content_type,
            "content_length": content_length,
            "last_modified": last_modified,
            "etag": headers.get("etag") if headers else None,
            "row_html": record.get("row_html"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": native_slug,
            "title": title,
            "abstract": abstract,
            "published_date": listed_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": "Regionaal- ja Põllumajandusministeerium",
            "department": section,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": section,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Misc parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _iso_date(value):
        if not value:
            return None
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        return match.group(0) if match else None

    @staticmethod
    def _date_from_display(value):
        if not value:
            return None
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
        if not match:
            return None
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    @staticmethod
    def _is_pdf_url(url, file_type=None):
        if file_type and file_type.lower() == "pdf":
            return True
        return ".pdf" in (url or "").lower()

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if "." in tail and len(tail) <= 240:
            return tail
        return None

    @classmethod
    def _slug_from_url(cls, url):
        filename = cls._filename_from_url(url)
        if filename:
            return filename.rsplit(".", 1)[0]
        path = unquote(urlparse(url).path).rstrip("/")
        tail = path.split("/")[-1] if path else ""
        return tail or None

    @staticmethod
    def _filename_from_content_disposition(headers):
        if not headers:
            return None
        value = headers.get("content-disposition") or ""
        if not value:
            return None
        match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.I)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        match = re.search(r'filename="?([^";]+)"?', value, flags=re.I)
        if match:
            return unquote(match.group(1).strip())
        return None

    @staticmethod
    def _build_abstract(
        *,
        title,
        section,
        listed_date,
        item_url,
        original_filename,
        file_size,
        file_type,
        content_type,
    ):
        parts = [
            f"Uuringu kirje: {title}.",
            f"Valdkond: {section}.",
        ]
        if listed_date:
            parts.append(f"Agri.ee uuringute lehel kuvatud sisestamise kuupäev on {listed_date}.")
        if file_type or file_size:
            parts.append(
                "Faili metaandmed: "
                + ", ".join(part for part in [file_type, file_size] if part)
                + "."
            )
        if original_filename:
            parts.append(f"Algne failinimi on {original_filename}.")
        if content_type:
            parts.append(f"Serveri vastuse Content-Type on {content_type}.")
        parts.append(f"Allikakirje viitab dokumendile aadressil {item_url}.")
        return " ".join(parts)

    def _next_page_url(self, soup, current_url):
        """Return a server-side next page URL if one exists.

        The current page's VPortal DataTables are client-side and have
        ``paging:false`` in embedded options, so this normally returns None.
        """
        for options_script in soup.find_all("script", {"type": "application/json"}):
            sid = options_script.get("id") or ""
            if not sid.startswith("datatable-") or not sid.endswith("-options"):
                continue
            try:
                options = json.loads(options_script.string or "{}")
            except Exception:
                continue
            if isinstance(options, dict) and options.get("paging") is False:
                return None

        for link in soup.select('a[rel="next"], a[aria-label*="Järgmine"], a[aria-label*="Next"]'):
            href = link.get("href") or ""
            if href and href != "#":
                next_url = urljoin(current_url, href)
                if next_url != current_url:
                    return next_url
        return None
