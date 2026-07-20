# -*- coding: utf-8 -*-
"""Crawler for LANL Electronic Public Reading Room Full Paper records."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EprrLanlGovCrawler(BaseCrawler):
    site_id = "eprr-lanl-gov"
    site_name = "Custom: eprr-lanl-gov"
    base_url = "https://eprr.lanl.gov"

    _LIST_URL = "https://eprr.lanl.gov/catalog.json"
    _DETAIL_URL = "https://eprr.lanl.gov/catalog/{record_id}.json"
    _PAGE_SIZE = 25
    _DEFAULT_PDF_TEXT_PAGES = 12
    _MAX_ABSTRACT_CHARS = 5000

    def __init__(self, db_conn, delay=1.0, detail_delay=None, pdf_text_pages=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay
        self.pdf_text_pages = (
            self._DEFAULT_PDF_TEXT_PAGES if pdf_text_pages is None else pdf_text_pages
        )

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, accept=None, referer=None, binary=False, timeout=60):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
        ]
        if accept:
            cmd.extend(["-H", f"Accept: {accept}"])
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                if result.returncode == 0 and result.stdout:
                    if binary:
                        return result.stdout
                    return result.stdout.decode("utf-8", errors="replace")

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < 2:
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_json(self, url, *, referer=None):
        raw = self._curl(
            url,
            accept="application/json,text/javascript,*/*;q=0.8",
            referer=referer,
            timeout=45,
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode failed for {url}: {exc}")
            return None

    def _fetch_list_page(self, page):
        params = [
            ("f[docType_f][]", "Full Paper"),
            ("page", str(page)),
            ("per_page", str(self._PAGE_SIZE)),
        ]
        url = f"{self._LIST_URL}?{urlencode(params)}"
        return self._fetch_json(url, referer=self.base_url + "/")

    def _fetch_detail(self, record_id):
        quoted = quote(record_id, safe="")
        url = self._DETAIL_URL.format(record_id=quoted)
        return self._fetch_json(url, referer=self.base_url + "/")

    def _extract_pdf_text(self, pdf_url):
        if not pdf_url:
            return "", {}

        pdf_bytes = self._curl(
            pdf_url,
            accept="application/pdf,*/*;q=0.8",
            referer=self.base_url + "/",
            binary=True,
            timeout=120,
        )
        if not pdf_bytes:
            return "", {"pdf_bytes": 0, "pdftotext_status": "download_failed"}

        cmd = [
            "pdftotext",
            "-f",
            "1",
            "-l",
            str(self.pdf_text_pages),
            "-layout",
            "-",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                input=pdf_bytes,
                capture_output=True,
                timeout=90,
                check=False,
            )
        except FileNotFoundError:
            print(f"[{self.site_id}] pdftotext not found; cannot extract PDF text")
            return "", {"pdf_bytes": len(pdf_bytes), "pdftotext_status": "not_found"}
        except subprocess.TimeoutExpired:
            print(f"[{self.site_id}] pdftotext timed out for {pdf_url}")
            return "", {"pdf_bytes": len(pdf_bytes), "pdftotext_status": "timeout"}

        text = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        status = "ok" if text.strip() else f"empty_exit_{result.returncode}"
        if not text.strip() and stderr:
            print(f"[{self.site_id}] pdftotext produced no text for {pdf_url}: {stderr}")

        return text, {
            "pdf_bytes": len(pdf_bytes),
            "pdftotext_exit": result.returncode,
            "pdftotext_status": status,
            "pdftotext_stderr": stderr[:500],
        }

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        return text.strip()

    @staticmethod
    def _one_line(value):
        return re.sub(r"\s+", " ", EprrLanlGovCrawler._clean_text(value)).strip()

    @staticmethod
    def _attr_value(record, key):
        attrs = (record or {}).get("attributes") or {}
        node = attrs.get(key)
        if isinstance(node, dict):
            nested = node.get("attributes") or {}
            value = nested.get("value")
            if value is not None:
                return value
            return node.get("value", "")
        return node or ""

    def _extract_link_from_html_value(self, html_value):
        if not html_value:
            return ""
        soup = self._parse_html(html_value)
        if soup is None:
            return ""
        link = soup.find("a", href=True)
        if not link:
            return ""
        return urljoin(self.base_url, link["href"].strip())

    @staticmethod
    def _parse_authors(raw):
        if not raw:
            return []
        authors = []
        for part in re.split(r"\s*;\s*", raw):
            name = EprrLanlGovCrawler._one_line(part)
            if not name:
                continue
            if name.lower() in {"et al.", "et al"}:
                continue
            authors.append(name)
        return authors

    @staticmethod
    def _parse_source_number(raw):
        value = EprrLanlGovCrawler._one_line(raw)
        if not value:
            return "", ""
        parts = [part.strip() for part in value.split(";") if part.strip()]
        doc_number = parts[0] if parts else value
        year = ""
        for part in parts[1:]:
            match = re.search(r"\b(19|20)\d{2}\b", part)
            if match:
                year = match.group(0)
                break
        if not year:
            match = re.search(r"\b(19|20)\d{2}\b", value)
            if match:
                year = match.group(0)
        return doc_number, year

    @staticmethod
    def _issued_date_from_text(text):
        if not text:
            return ""
        match = re.search(r"\bIssued:\s*((?:19|20)\d{2}-\d{2}-\d{2})", text)
        if match:
            return match.group(1)
        match = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+((?:19|20)\d{2})\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            month_name = match.group(1).lower()
            months = {
                "january": "01",
                "february": "02",
                "march": "03",
                "april": "04",
                "may": "05",
                "june": "06",
                "july": "07",
                "august": "08",
                "september": "09",
                "october": "10",
                "november": "11",
                "december": "12",
            }
            return f"{match.group(2)}-{months[month_name]}"
        return ""

    @staticmethod
    def _is_noise_line(line):
        lower = line.lower()
        if lower in {
            "approved for public release;",
            "approved for public release; distribution is unlimited.",
            "distribution is unlimited.",
            "disclaimer:",
            "an affirmative action/equal opportunity employer",
            "this page intentionally left blank",
        }:
            return True
        if "affirmative action/equal opportunity employer" in lower:
            return True
        if "los alamos national laboratory requests that the publisher identify" in lower:
            return True
        if "laboratory strongly supports academic freedom" in lower:
            return True
        return False

    def _clean_pdf_text(self, raw):
        text = self._clean_text(raw)
        lines = []
        for line in text.splitlines():
            cleaned = self._one_line(line)
            if not cleaned or self._is_noise_line(cleaned):
                continue
            lines.append(cleaned)
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _select_abstract(self, raw_pdf_text):
        text = self._clean_pdf_text(raw_pdf_text)
        if not text:
            return ""

        patterns = [
            r"(?im)^abstract\s*$",
            r"(?im)^executive summary\s*$",
            r"(?im)^preface\s*$",
            r"(?im)^subject:\s+.+$",
            r"(?im)^dear\s+.+:$",
            r"(?im)^1\.0\s+background\s*$",
            r"(?im)^1\.0\s+introduction\s*$",
            r"(?im)^background\s*$",
            r"(?im)^introduction\s*$",
        ]
        selected = ""
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                selected = text[match.start():]
                break
        if not selected:
            selected = text

        selected = re.sub(r"\n+", "\n", selected).strip()
        selected = re.sub(r"[ \t]+", " ", selected)
        if len(selected) > self._MAX_ABSTRACT_CHARS:
            selected = selected[: self._MAX_ABSTRACT_CHARS].rsplit(" ", 1)[0].strip()
        return selected

    def _parse_detail(self, record_id, list_record, detail_json):
        data = (detail_json or {}).get("data") or {}
        if not data:
            raise ValueError("empty detail data")

        title = self._one_line(self._attr_value(data, "displayTitle"))
        if not title:
            title = record_id

        author_raw = self._one_line(
            self._attr_value(data, "displayName")
            or self._attr_value(list_record, "displayName")
        )
        authors = self._parse_authors(author_raw)

        source_raw = self._one_line(
            self._attr_value(data, "displaySource")
            or self._attr_value(list_record, "displaySource")
        )
        doc_number, source_year = self._parse_source_number(source_raw)
        pdf_url = self._extract_link_from_html_value(self._attr_value(data, "url"))
        public_url = (
            ((detail_json or {}).get("links") or {}).get("self")
            or ((data or {}).get("links") or {}).get("self")
            or f"{self.base_url}/catalog/{quote(record_id, safe='')}"
        )

        pdf_text, pdf_meta = self._extract_pdf_text(pdf_url)
        abstract = self._select_abstract(pdf_text)
        published_date = self._issued_date_from_text(pdf_text) or source_year

        metadata = {
            "record_id": record_id,
            "document_number": doc_number,
            "display_source": source_raw,
            "display_author": author_raw,
            "doc_type": "Full Paper",
            "detail_api_url": self._DETAIL_URL.format(record_id=quote(record_id, safe="")),
            "list_api_url": self._LIST_URL,
            "pdf_text_pages": self.pdf_text_pages,
            "abstract_source": "linked_pdf_text",
            **pdf_meta,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": record_id,
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": "Full Paper",
            "keywords": json.dumps(["Full Paper"], ensure_ascii=False),
            "published_date": published_date,
            "url": public_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "Los Alamos National Laboratory",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        total_pages = None

        while True:
            if limit is not None and saved >= limit:
                break

            data = self._fetch_list_page(page)
            if data is None:
                print(f"[{self.site_id}] failed to fetch list page {page}; stopping")
                break

            page_meta = ((data.get("meta") or {}).get("pages") or {})
            if total_pages is None:
                total_pages = page_meta.get("total_pages")
                total_count = page_meta.get("total_count")
                if total_count is not None:
                    print(f"[{self.site_id}] Total Full Paper records: {total_count}")

            items = data.get("data") or []
            if not items:
                break

            for index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                record_id = self._one_line(item.get("id"))
                item_label = record_id or f"page {page} row {index}"
                try:
                    if not record_id:
                        raise ValueError("missing record id")

                    detail = self._fetch_detail(record_id)
                    if detail is None:
                        print(f"[{self.site_id}] item {item_label} failed: empty detail response")
                        continue

                    paper = self._parse_detail(record_id, item, detail)
                    abstract_len = len(paper.get("abstract") or "")
                    if abstract_len < 50:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({abstract_len})"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[eprr-lanl-gov] item {item_label} failed: {exc}")
                    continue
                finally:
                    time.sleep(self.detail_delay)

            page += 1
            if total_pages is not None and page > int(total_pages):
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
