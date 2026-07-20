# -*- coding: utf-8 -*-
"""Crawler for Santé publique France press releases."""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class SantePubliqueFranceFrPresseCrawler(BaseCrawler):
    site_id = "santepubliquefrance-fr-presse"
    site_name = "Custom: santepubliquefrance-fr-presse"
    base_url = "https://www.santepubliquefrance.fr"

    START_URL = "https://www.santepubliquefrance.fr/presse"
    LIST_ENDPOINT = "/presse?page={page}"
    DETAIL_ENDPOINT = "/presse/<slug>"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__SPF_PRESSE_CURL_META__:"

    MONTHS = {
        "janvier": 1,
        "fevrier": 2,
        "mars": 3,
        "avril": 4,
        "mai": 5,
        "juin": 6,
        "juillet": 7,
        "aout": 8,
        "septembre": 9,
        "octobre": 10,
        "novembre": 11,
        "decembre": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the Drupal-rendered press list and each detail page."""
        saved = 0
        page = 0
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw, list_effective_url = self._curl_get(
                list_url,
                context=f"list page {page}",
                referer=self.base_url + "/",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_effective_url or list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            print(
                f"[{self.site_id}] page {page}: discovered {len(records)} records "
                "from /presse?page=N HTML list endpoint"
            )

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"page {page} item {idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self.detail_delay)
                    detail_raw, effective_url = self._curl_get(
                        detail_url,
                        context=f"item {item_label} detail",
                        referer=list_effective_url or list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_label} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(
                        detail_soup,
                        detail_raw,
                        record,
                        effective_url or detail_url,
                    )
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[santepubliquefrance-fr-presse] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup, page):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
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
            "-A",
            self.USER_AGENT,
            "-H",
            accept
            or (
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            "-w",
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                body, http_code, effective_url = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _parse_list(self, soup, list_url):
        container = soup.select_one(".view-page-list.view-list-press") or soup
        records = []
        seen = set()

        for row in container.select(".view-content.view-list-press > .views-row"):
            link = row.select_one(".views-field-view-node a[href]")
            title_el = row.select_one(".views-field-title .field-content")
            if link is None or title_el is None:
                continue

            detail_url = urljoin(list_url, link.get("href", "").strip())
            if not self._is_press_detail_url(detail_url) or detail_url in seen:
                continue
            seen.add(detail_url)

            date_el = (
                row.select_one(".views-field-field-displayed-pub-date time[datetime]")
                or row.select_one(".views-field-field-date time[datetime]")
                or row.select_one("time[datetime]")
            )
            pdf_el = (
                row.select_one(".views-field-field-document a.download[href]")
                or row.select_one(".views-field-field-document a[href*='.pdf']")
                or row.select_one("a.download[href*='.pdf']")
            )

            title = self._clean_text(title_el.get_text(" ", strip=True))
            if not title:
                continue

            records.append(
                {
                    "title": title,
                    "url": detail_url,
                    "published_date": self._parse_date(
                        date_el.get("datetime") if date_el else ""
                    ),
                    "list_date_text": self._clean_text(
                        date_el.get_text(" ", strip=True) if date_el else ""
                    ),
                    "pdf_url": urljoin(list_url, pdf_el.get("href", "").strip())
                    if pdf_el
                    else "",
                    "pdf_label": self._clean_text(
                        pdf_el.get("aria-label")
                        or pdf_el.get_text(" ", strip=True)
                        if pdf_el
                        else ""
                    ),
                }
            )

        return records

    def _parse_detail(self, soup, raw, record, effective_url):
        settings = self._drupal_settings(soup)
        current_path = (
            settings.get("path", {}).get("currentPath", "")
            if isinstance(settings, dict)
            else ""
        )
        node_id = ""
        match = re.search(r"node/(\d+)", current_path or "")
        if match:
            node_id = match.group(1)

        canonical_el = soup.select_one('link[rel="canonical"][href]')
        canonical_url = (
            canonical_el.get("href", "").strip()
            if canonical_el
            else effective_url or record.get("url") or ""
        )
        canonical_url = urljoin(self.base_url, canonical_url)

        title_el = soup.select_one("h1.node-full__header__title") or soup.select_one("h1")
        title = self._clean_text(title_el.get_text(" ", strip=True) if title_el else "")
        if not title:
            title = record.get("title") or ""
        if not title:
            raise RuntimeError("detail page has no title")

        pub_el = (
            soup.select_one(".node-full__header__bottom time[pubdate][datetime]")
            or soup.select_one(".node-full__header__bottom time[datetime]")
            or soup.select_one("time[pubdate][datetime]")
        )
        published_date = self._parse_date(pub_el.get("datetime") if pub_el else "")
        if not published_date:
            published_date = record.get("published_date") or ""

        pdf_el = (
            soup.select_one(".node-full__header__bottom .field--name-field-document a.download[href]")
            or soup.select_one(".field--name-field-document a.download[href]")
            or soup.select_one("a.download[href*='.pdf']")
            or soup.select_one("a[href*='.pdf']")
        )
        pdf_url = record.get("pdf_url") or ""
        if pdf_el:
            pdf_url = urljoin(canonical_url, pdf_el.get("href", "").strip())

        pdf_name_el = soup.select_one(".field--name-field-document .field--name-name .field__item")
        pdf_filename = self._clean_text(pdf_name_el.get_text(" ", strip=True) if pdf_name_el else "")
        if not pdf_filename and pdf_url:
            pdf_filename = urlparse(pdf_url).path.rstrip("/").split("/")[-1]

        description_el = soup.select_one(
            ".node-full__content__description "
            ".field--name-field-description .field__item"
        )
        description = self._element_text(description_el)

        content_parts = []
        content_scope = soup.select_one(
            ".node-full__content--background .field--name-field-content"
        )
        if content_scope is None:
            content_scope = soup.select_one(".field--name-field-content")

        if content_scope is not None:
            for el in content_scope.select(
                ".paragraph--type--wysiwyg .field--name-field-text .field__item"
            ):
                text = self._element_text(el)
                if text:
                    content_parts.append(text)

        abstract_parts = []
        for part in [description] + content_parts:
            if part and part not in abstract_parts:
                abstract_parts.append(part)
        abstract = "\n\n".join(abstract_parts).strip()

        header = soup.select_one("#SPF-header")
        regions = self._decode_attr_list(header.get("data-piano-regions") if header else "")
        themes = (
            self._decode_attr_list(header.get("data-piano-thematics") if header else "")
            or self._decode_attr_list(header.get("data-piano-themes") if header else "")
        )
        document_types = self._decode_attr_list(
            header.get("data-piano-document-types") if header else ""
        )
        keywords = []
        for value in themes + regions:
            if value and value not in keywords:
                keywords.append(value)

        external_id = node_id or self._external_id_from_url(canonical_url)
        metadata = {
            "source": "Drupal press-release HTML",
            "listEndpoint": self.LIST_ENDPOINT,
            "detailEndpoint": self.DETAIL_ENDPOINT,
            "currentPath": current_path,
            "nodeId": node_id,
            "effectiveUrl": effective_url,
            "listRecord": record,
            "regions": regions,
            "themes": themes,
            "documentTypes": document_types,
            "pdfFileName": pdf_filename,
            "description": description,
            "contentPartCount": len(content_parts),
            "rawHtmlLength": len(raw or ""),
        }

        return {
            "external_id": external_id,
            "title": title,
            "authors": [],
            "abstract": abstract,
            "category": "communiqué de presse",
            "keywords": keywords,
            "published_date": published_date,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "Santé publique France",
            "metadata": metadata,
        }

    def _drupal_settings(self, soup):
        script = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
        if script is None:
            return {}
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _has_next_page(self, soup, page):
        button = soup.select_one(".js-load-more[data-total-pages]")
        if button is not None:
            try:
                total_pages = int(button.get("data-total-pages") or "0")
                if total_pages > 0:
                    return page + 1 < total_pages
            except ValueError:
                pass
        return soup.select_one(".pager__item--next a[href]") is not None

    def _list_url(self, page):
        if page == 0:
            return self.START_URL
        return urljoin(self.base_url, self.LIST_ENDPOINT.format(page=page))

    def _is_press_detail_url(self, url):
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
            return False
        path = parsed.path.rstrip("/")
        return "/presse/" in path and path not in ("/presse", "/index.php/presse")

    def _external_id_from_url(self, url):
        path = urlparse(url).path.strip("/")
        path = re.sub(r"^index\.php/", "", path)
        return path or url

    def _decode_attr_list(self, value):
        if not value:
            return []
        raw = unescape(value).strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [self._clean_text(item) for item in parsed if self._clean_text(item)]
        except (TypeError, ValueError):
            pass
        return [self._clean_text(raw)] if raw else []

    def _element_text(self, element):
        if element is None:
            return ""
        return self._clean_text(element.get_text(" ", strip=True))

    def _parse_date(self, raw):
        value = self._clean_text(raw or "")
        if not value:
            return ""
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

        normalized = self._strip_accents(value.lower())
        match = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", normalized)
        if match:
            day = int(match.group(1))
            month = self.MONTHS.get(match.group(2))
            year = int(match.group(3))
            if month:
                return f"{year:04d}-{month:02d}-{day:02d}"
        return ""

    @staticmethod
    def _strip_accents(value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value)).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()
