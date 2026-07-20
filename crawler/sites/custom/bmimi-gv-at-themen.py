# -*- coding: utf-8 -*-
"""Crawler for BMIMI Menschen, Qualifikation und Gender publications."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BmimiGvAtThemenCrawler(BaseCrawler):
    site_id = "bmimi-gv-at-themen"
    site_name = "Custom: bmimi-gv-at-themen"
    base_url = "https://www.bmimi.gv.at"

    START_URL = (
        "https://www.bmimi.gv.at/themen/innovation/publikationen/"
        "menschen_qualifikation_gender.html"
    )
    CATEGORY_PATH = "/themen/innovation/publikationen/menschen_qualifikation_gender/"
    CATEGORY = "Menschen, Qualifikation und Gender"
    PUBLISHER = "Bundesministerium fuer Innovation, Mobilitaet und Infrastruktur (BMIMI)"
    PUBLISHER_DISPLAY = "Bundesministerium für Innovation, Mobilität und Infrastruktur (BMIMI)"

    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    CURL_META_MARKER = "__BMIMI_GV_AT_CURL_META__:"
    MIN_ABSTRACT_CHARS = 50
    WALL_CLOCK_SECONDS = 25 * 60
    SAFETY_PAGE_CAP = 200

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl the BMIMI category and its publication detail pages."""
        saved = 0
        item_number = 0
        page = 1
        page_url = self.START_URL
        seen_urls = set()
        seen_page_urls = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        print(f"[{self.site_id}] list endpoint: {self.START_URL} (static HTML)")
        print(f"[{self.site_id}] detail endpoint: category detail HTML links")
        print(f"[{self.site_id}] pdf header endpoint: /dam/jcr:<uuid>/<filename> (range GET)")

        try:
            while page_url and page <= self.SAFETY_PAGE_CAP:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - started_at > self.WALL_CLOCK_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                    break
                if page_url in seen_page_urls:
                    print(f"[{self.site_id}] pagination loop detected at {page_url}; stopping")
                    break
                seen_page_urls.add(page_url)

                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                raw, effective_list_url, list_headers = self._curl_get_text(
                    page_url,
                    context=f"list page {page}",
                    referer=self.base_url + "/themen/innovation/publikationen.html",
                )
                if not raw:
                    print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                    break

                soup = self._make_soup(raw, context=f"list page {page}")
                if soup is None:
                    print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                    break

                records = self._parse_list_records(soup, effective_list_url or page_url, page)
                print(f"[{self.site_id}] page {page}: discovered {len(records)} records")
                if not records:
                    break

                new_records = 0
                for record in records:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_records += 1

                    if limit is not None and saved >= limit:
                        break
                    if time.monotonic() - started_at > self.WALL_CLOCK_SECONDS - 30:
                        print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                        break

                    item_number += 1
                    try:
                        time.sleep(self.detail_delay)
                        detail_raw, effective_detail_url, detail_headers = self._curl_get_text(
                            detail_url,
                            context=f"item {item_number} detail",
                            referer=effective_list_url or page_url,
                        )
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")

                        detail_soup = self._make_soup(
                            detail_raw,
                            context=f"item {item_number} detail",
                        )
                        if detail_soup is None:
                            raise RuntimeError("detail HTML could not be parsed")

                        parsed = self._parse_detail(
                            detail_soup,
                            record,
                            effective_detail_url or detail_url,
                        )
                        pdf_url = parsed.get("pdf_url") or ""
                        pdf_headers = {}
                        if pdf_url:
                            pdf_headers = self._curl_get_headers(
                                pdf_url,
                                context=f"item {item_number} pdf header",
                                referer=effective_detail_url or detail_url,
                            )

                        original_filename = (
                            self._filename_from_content_disposition(
                                pdf_headers.get("content-disposition", "")
                            )
                            or self._filename_from_url(pdf_url)
                        )
                        pdf_last_modified_raw = pdf_headers.get("last-modified") or ""
                        pdf_last_modified_date = self._parse_http_date(pdf_last_modified_raw)
                        published_date = (
                            parsed.get("published_date")
                            or self._infer_publication_date(parsed, original_filename)
                            or pdf_last_modified_date
                        )
                        listed_date = parsed.get("listed_date") or pdf_last_modified_date or published_date

                        abstract = self._clean_text(parsed.get("abstract") or "")
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_number} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        title = parsed.get("title") or record.get("title") or ""
                        if not title:
                            raise RuntimeError("parsed record has no title")

                        external_id = parsed.get("external_id") or record.get("external_id")
                        post_number = parsed.get("post_number") or record.get("post_number")
                        keywords = self._dedupe(
                            (parsed.get("keywords") or [])
                            + [self.CATEGORY, self.PUBLISHER_DISPLAY]
                        )
                        pdf_node_id = self._jcr_id_from_url(pdf_url)
                        metadata = {
                            "source": "BMIMI Magnolia HTML category and detail pages",
                            "list_endpoint": self.START_URL,
                            "detail_endpoint": effective_detail_url or detail_url,
                            "pdf_header_endpoint": pdf_url,
                            "list_page": page,
                            "list_record": record,
                            "list_headers": list_headers,
                            "detail_headers": detail_headers,
                            "pdf_headers": pdf_headers,
                            "posted_date": pdf_last_modified_raw or listed_date,
                            "originalFilename": original_filename,
                            "journal_raw": None,
                            "series": None,
                            "volume": None,
                            "issue": None,
                            "detail_slug": parsed.get("detail_slug"),
                            "node_id": pdf_node_id,
                            "pdf_node_id": pdf_node_id,
                            "subtitle": parsed.get("subtitle") or "",
                            "meta_description": parsed.get("meta_description") or "",
                            "pdf_links": parsed.get("pdf_links") or [],
                            "image_url": parsed.get("image_url") or "",
                            "body_years": parsed.get("body_years") or [],
                            "raw_title": record.get("title") or "",
                            "post_number": post_number,
                            "listed_date": listed_date,
                            "published_date": published_date,
                            "category": self.CATEGORY,
                        }

                        paper = {
                            "id": f"{self.site_id}:{external_id}",
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": post_number,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "posted_date": listed_date,
                            "authors": parsed.get("authors") or "",
                            "publisher": self.PUBLISHER_DISPLAY,
                            "department": self.PUBLISHER,
                            "journal": "",
                            "url": effective_detail_url or detail_url,
                            "pdf_url": pdf_url or None,
                            "keywords": ", ".join(keywords) if keywords else "",
                            "category": self.CATEGORY,
                            "doi": "",
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        counter = f"{saved}/{limit}" if limit is not None else str(saved)
                        print(f"[{self.site_id}] saved {counter}: {title[:90]}")
                    except Exception as exc:
                        print(f"[bmimi-gv-at-themen] item {item_number} failed: {exc}")
                        continue

                if limit is not None and saved >= limit:
                    break
                if new_records == 0:
                    print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                    break

                next_url = self._next_page_url(soup, effective_list_url or page_url)
                if not next_url:
                    break
                page_url = next_url
                page += 1

            if page > self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] reached safety cap of {self.SAFETY_PAGE_CAP} pages")
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get_text(self, url, context="request", referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: de-AT,de;q=0.9,en;q=0.7",
            "-D",
            "-",
            "-w",
            "\n" + self.CURL_META_MARKER + "%{http_code}\t%{url_effective}\t%{content_type}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        body, effective_url, headers = self._run_curl(cmd, context)
        return body, effective_url, headers

    def _curl_get_headers(self, url, context="request", referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: application/pdf,*/*;q=0.8",
            "-r",
            "0-0",
            "-D",
            "-",
            "-o",
            "/dev/null",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        _body, _effective_url, headers = self._run_curl(cmd, context, allow_empty_body=True)
        return headers

    def _run_curl(self, cmd, context, allow_empty_body=False):
        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                body, status_code, effective_url, _content_type, headers = self._split_curl_response(text)
                if result.returncode == 0 and 200 <= status_code < 400:
                    if allow_empty_body or body.strip():
                        return body, effective_url, headers
                    last_error = f"curl exit=0 http={status_code} empty body"
                else:
                    stderr = result.stderr.decode("utf-8", errors="replace").strip()
                    last_error = (
                        f"curl exit={result.returncode} http={status_code} "
                        f"stderr={stderr[:200]}"
                    )
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None, None, {}

    def _split_curl_response(self, text):
        status_code = 0
        effective_url = ""
        content_type = ""
        if self.CURL_META_MARKER in text:
            text, meta_raw = text.rsplit(self.CURL_META_MARKER, 1)
            parts = meta_raw.strip().split("\t", 2)
            while len(parts) < 3:
                parts.append("")
            try:
                status_code = int(parts[0])
            except (TypeError, ValueError):
                status_code = 0
            effective_url = parts[1]
            content_type = parts[2]

        header_blocks = []
        body = text
        while body.startswith("HTTP/"):
            marker = "\r\n\r\n" if "\r\n\r\n" in body else "\n\n"
            if marker not in body:
                break
            block, body = body.split(marker, 1)
            header_blocks.append(block)

        headers = self._parse_header_block(header_blocks[-1] if header_blocks else "")
        if not status_code:
            status_code = self._status_from_header(header_blocks[-1] if header_blocks else "")
        if not content_type:
            content_type = headers.get("content-type", "")
        return body, status_code, effective_url, content_type, headers

    @staticmethod
    def _parse_header_block(block):
        headers = {}
        for line in block.splitlines()[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return headers

    @staticmethod
    def _status_from_header(block):
        first = (block.splitlines() or [""])[0]
        match = re.search(r"\s(\d{3})(?:\s|$)", first)
        if not match:
            return 0
        return int(match.group(1))

    def _make_soup(self, raw, context="HTML"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] {context} parse failed with {parser}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list_records(self, soup, list_url, page):
        records = []
        main = soup.select_one("main#content") or soup
        for idx, link in enumerate(main.select("ul.overview a[href], nav a[href]"), start=1):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
                continue
            if not parsed.path.startswith(self.CATEGORY_PATH) or not parsed.path.endswith(".html"):
                continue
            if parsed.path == urlparse(self.START_URL).path:
                continue

            title = self._clean_text(link.get_text(" ", strip=True))
            slug = self._slug_from_url(url)
            records.append({
                "title": title,
                "url": url,
                "external_id": slug,
                "post_number": slug,
                "detail_slug": slug,
                "list_position": idx,
                "list_page": page,
                "list_url": list_url,
            })
        return self._dedupe_records(records)

    def _parse_detail(self, soup, record, effective_url):
        main = soup.select_one("main#content") or soup
        title_node = main.select_one("h1 .title") or main.select_one("h1")
        subtitle_node = main.select_one("h1 .subtitle")
        title = self._clean_text(title_node.get_text(" ", strip=True) if title_node else record.get("title", ""))
        subtitle = self._clean_text(subtitle_node.get_text(" ", strip=True) if subtitle_node else "")
        meta_description = self._meta_content(soup, "description")
        keywords = self._keywords_from_meta(soup)
        pdf_links = self._extract_pdf_links(main)
        pdf_url = pdf_links[0]["url"] if pdf_links else ""
        image_url = self._extract_image_url(main)

        abstract = self._extract_abstract(main, subtitle, meta_description)
        body_text = self._clean_text(main.get_text(" ", strip=True))
        body_years = self._extract_years(" ".join([body_text, " ".join(p.get("url", "") for p in pdf_links)]))
        detail_slug = self._slug_from_url(effective_url)

        return {
            "external_id": detail_slug or record.get("external_id"),
            "post_number": detail_slug or record.get("post_number"),
            "detail_slug": detail_slug,
            "title": title,
            "subtitle": subtitle,
            "abstract": abstract,
            "published_date": self._parse_visible_date(main),
            "listed_date": None,
            "authors": self._extract_authors(body_text),
            "keywords": keywords,
            "pdf_url": pdf_url,
            "pdf_links": pdf_links,
            "image_url": image_url,
            "meta_description": meta_description,
            "body_years": body_years,
            "url": effective_url,
        }

    def _extract_abstract(self, main, subtitle, meta_description):
        pieces = []
        if subtitle:
            pieces.append(subtitle)

        content_nodes = main.find_all(["p", "h2", "h3", "li"], recursive=True)
        for node in content_nodes:
            if self._is_navigation_node(node):
                continue
            clone = BeautifulSoup(str(node), "html.parser")
            for selector in ("span.fileinfo", ".sr-only"):
                for unwanted in clone.select(selector):
                    unwanted.decompose()
            text = self._clean_text(clone.get_text(" ", strip=True))
            if not text:
                continue
            if self._looks_like_file_only_text(text):
                continue
            pieces.append(text)

        abstract = self._clean_text(" ".join(pieces))
        if len(abstract) < len(meta_description or ""):
            abstract = meta_description
        return abstract

    def _extract_pdf_links(self, main):
        links = []
        for link in main.select('a[href*=".pdf"], a.file[href]'):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            text = self._clean_text(link.get_text(" ", strip=True))
            fileinfo = ""
            info_node = link.select_one(".fileinfo")
            if info_node:
                fileinfo = self._clean_text(info_node.get_text(" ", strip=True))
            links.append({
                "url": url,
                "label": text,
                "fileinfo": fileinfo,
                "node_id": self._jcr_id_from_url(url),
                "originalFilename": self._filename_from_url(url),
            })
        return self._dedupe_pdf_links(links)

    def _extract_image_url(self, main):
        image = main.select_one("figure img[src], figure img[data-src], img[src], img[data-src]")
        if not image:
            return ""
        src = image.get("data-src") or image.get("src") or ""
        return urljoin(self.base_url, src) if src else ""

    def _parse_visible_date(self, main):
        text = self._clean_text(main.get_text(" ", strip=True))
        match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if match:
            return match.group(0)
        match = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
        if match:
            day, month, year = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return None

    def _infer_publication_date(self, parsed, original_filename):
        text = " ".join([
            parsed.get("title") or "",
            parsed.get("subtitle") or "",
            parsed.get("abstract") or "",
            original_filename or "",
        ])
        years = self._extract_years(text)
        if not years:
            return None
        return f"{max(years):04d}-01-01"

    @staticmethod
    def _extract_years(text):
        current_year = datetime.utcnow().year
        years = []
        for match in re.finditer(r"\b(19\d{2}|20\d{2})\b", text or ""):
            year = int(match.group(1))
            if 1990 <= year <= current_year:
                years.append(year)
        return years

    def _extract_authors(self, body_text):
        authors = []
        for match in re.finditer(r"\(([^()]{10,220})\)", body_text or ""):
            value = self._clean_text(match.group(1))
            if ";" in value and re.search(r"\b(19|20)\d{2}\b", value):
                first = value.split(";", 1)[0]
                first = re.sub(r",?\s*(im Auftrag|Wien|Graz|Endbericht).*$", "", first).strip()
                if 2 <= len(first) <= 120 and not first.lower().startswith("pdf"):
                    authors.append(first)
        return "; ".join(self._dedupe(authors))

    def _keywords_from_meta(self, soup):
        raw = self._meta_content(soup, "keywords")
        if not raw:
            return []
        return [part.strip() for part in raw.split(",") if part.strip()]

    def _meta_content(self, soup, name):
        node = soup.select_one(f'meta[name="{name}"]')
        if node and node.get("content"):
            return self._clean_text(node["content"])
        node = soup.select_one(f'meta[property="og:{name}"]')
        if node and node.get("content"):
            return self._clean_text(node["content"])
        return ""

    def _next_page_url(self, soup, current_url):
        candidates = [
            'a[rel="next"][href]',
            "li.page-item.next a[href]",
            "li.next a[href]",
            "a.next[href]",
        ]
        for selector in candidates:
            link = soup.select_one(selector)
            if link and link.get("href"):
                return urljoin(current_url, link["href"])
        for link in soup.select("a[href]"):
            text = self._clean_text(link.get_text(" ", strip=True)).lower()
            title = self._clean_text(link.get("title") or "").lower()
            if "nächste seite" in text or "naechste seite" in text or "nächste seite" in title:
                return urljoin(current_url, link["href"])
        return None

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _slug_from_url(url):
        path = urlparse(url or "").path
        tail = path.rstrip("/").split("/")[-1]
        return re.sub(r"\.html?$", "", tail, flags=re.IGNORECASE)

    @staticmethod
    def _jcr_id_from_url(url):
        match = re.search(r"/dam/jcr:([^/]+)/", url or "")
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        tail = urlparse(url).path.rstrip("/").split("/")[-1]
        tail = unquote(tail)
        if "." in tail and len(tail) <= 240:
            return tail
        return None

    @staticmethod
    def _filename_from_content_disposition(value):
        if not value:
            return None
        match = re.search(r"filename\*\s*=\s*([^']*)''([^;]+)", value, flags=re.IGNORECASE)
        if match:
            return unquote(match.group(2).strip().strip('"'))
        match = re.search(r'filename\s*=\s*"([^"]+)"', value, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"filename\s*=\s*([^;]+)", value, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().strip('"')
        return None

    @staticmethod
    def _parse_http_date(value):
        if not value:
            return None
        try:
            return parsedate_to_datetime(value).date().isoformat()
        except (TypeError, ValueError, IndexError, AttributeError):
            return None

    @staticmethod
    def _is_navigation_node(node):
        parent = node
        while parent is not None:
            classes = parent.get("class") or []
            node_id = parent.get("id") or ""
            if parent.name in ("nav", "aside", "footer"):
                return True
            if "overview" in classes or "breadcrumb" in classes or "subnavigation" in node_id:
                return True
            parent = parent.parent
        return False

    @staticmethod
    def _looks_like_file_only_text(text):
        low = text.lower()
        return (
            "(pdf," in low
            and len(text) < 180
            and not any(mark in text for mark in (".", ":", ";"))
        )

    @staticmethod
    def _dedupe(values):
        seen = set()
        result = []
        for value in values:
            value = str(value).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _dedupe_records(self, records):
        seen = set()
        result = []
        for record in records:
            url = record.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            result.append(record)
        return result

    def _dedupe_pdf_links(self, links):
        seen = set()
        result = []
        for link in links:
            url = link.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            result.append(link)
        return result
