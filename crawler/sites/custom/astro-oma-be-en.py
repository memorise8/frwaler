# -*- coding: utf-8 -*-
"""Royal Observatory of Belgium annual reports crawler.

Starting URL:
https://www.astro.oma.be/en/information/publications/annual-reports/

The site is an old WordPress install.  Its REST API is unavailable, and the
annual report records are exposed as a static HTML list whose item links point
directly to PDFs.  We therefore use the HTML page as the list endpoint and each
PDF as the detail/source endpoint.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from email.message import Message
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except ImportError:  # pragma: no cover - crawler runtime dependency
    _BeautifulSoup = None


def _make_soup(raw: str):
    """Build BeautifulSoup with the required parser fallback chain."""
    if _BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[astro-oma-be-en] BeautifulSoup parser {parser} failed: {exc}")
    return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class AstroOmaBeEnCrawler(BaseCrawler):
    site_id = "astro-oma-be-en"
    site_name = "Custom: astro-oma-be-en"
    base_url = "https://www.astro.oma.be"

    START_URL = (
        "https://www.astro.oma.be/en/information/publications/annual-reports/"
    )
    MAX_PAGES = 200
    MAX_WALL_SECONDS = 25 * 60
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 120
    MIN_ABSTRACT_CHARS = 100
    PDF_TEXT_LAST_PAGE = 30
    WP_PAGE_ID = "1452"
    PUBLISHER = "Royal Observatory of Belgium"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl(
        self,
        url: str,
        *,
        accept: str = "text/html,application/xhtml+xml,*/*;q=0.8",
        method: str = "GET",
        binary: bool = False,
        retries: int = 3,
    ) -> str | bytes | None:
        """Fetch a URL with curl and exponential-backoff retry."""
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
            f"Accept: {accept}",
        ]
        if method == "HEAD":
            cmd.append("-I")
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                if result.returncode == 0 and result.stdout:
                    if binary:
                        return result.stdout
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                if err:
                    print(
                        f"[astro-oma-be-en] curl returned no body for {url}: {err}"
                    )
            except Exception as exc:
                print(
                    f"[astro-oma-be-en] curl error attempt {attempt + 1}/"
                    f"{retries} for {url}: {exc}"
                )

            if attempt < retries - 1:
                wait = self.BACKOFF_SECONDS[attempt]
                print(f"[astro-oma-be-en] retrying in {wait}s: {url}")
                time.sleep(wait)

        return None

    def _curl_headers(self, url: str) -> dict[str, str]:
        raw = self._curl(
            url,
            accept="application/pdf,*/*;q=0.8",
            method="HEAD",
            binary=False,
        )
        if not isinstance(raw, str):
            return {}

        headers: dict[str, str] = {}
        for block in re.split(r"\r?\n\r?\n", raw.strip()):
            if not block.lower().startswith("http/"):
                continue
            current: dict[str, str] = {}
            for line in block.splitlines()[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                current[key.strip().lower()] = value.strip()
            if current:
                headers = current
        return headers

    # ------------------------------------------------------------------
    # list parsing
    # ------------------------------------------------------------------

    def _list_url_for_page(self, page: int) -> str:
        if page <= 1:
            return self.START_URL
        return f"{self.START_URL.rstrip('/')}/page/{page}/"

    def _extract_page_id(self, html: str) -> str | None:
        m = re.search(r"\bpage-id-(\d+)\b", html)
        if m:
            return m.group(1)
        m = re.search(r"[?&]p=(\d+)", html)
        return m.group(1) if m else self.WP_PAGE_ID

    def _parse_list_page(self, html: str, page_url: str) -> tuple[list[dict], bool]:
        soup = _make_soup(html)
        if soup is None:
            return self._parse_list_page_regex(html, page_url), False

        page_id = self._extract_page_id(html)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        current_section = ""

        content_root = soup.find(id=re.compile(r"builder-module-541c3a477a922"))
        if content_root is None:
            content_root = soup.find("div", id=re.compile(r"post-\d+")) or soup

        for node in content_root.find_all(["p", "li"]):
            if node.name == "p":
                text = _clean_text(node.get_text(" ", strip=True))
                if text:
                    current_section = text
                continue

            link = node.find("a", href=True)
            if not link:
                continue
            title = _clean_text(link.get_text(" ", strip=True))
            href = link.get("href") or ""
            if not self._looks_like_annual_report(title, href):
                continue

            pdf_url = self._normalize_url(href)
            if pdf_url in seen:
                continue
            seen.add(pdf_url)

            year = self._extract_year(title, pdf_url)
            filename = self._filename_from_url(pdf_url)
            listed_raw, listed_date = self._listed_date_from_url(pdf_url)
            items.append(
                {
                    "title": title or f"Annual Report {year}",
                    "pdf_url": pdf_url,
                    "url": pdf_url,
                    "source_page_url": page_url,
                    "source_page_id": page_id,
                    "section": current_section,
                    "report_year": year,
                    "post_number": year,
                    "external_id": filename or urlparse(pdf_url).path,
                    "original_filename": filename,
                    "listed_date_raw": listed_raw,
                    "listed_date": listed_date,
                    "list_text": _clean_text(node.get_text(" ", strip=True)),
                }
            )

        has_next = bool(
            soup.find("a", rel=lambda value: value and "next" in value)
            or soup.select_one("a.next, a.page-numbers.next")
        )
        if not has_next:
            for a in soup.find_all("a", href=True):
                text = _clean_text(a.get_text(" ", strip=True)).lower()
                if text in {"next", "next page", "older posts", "older"}:
                    has_next = True
                    break

        return items, has_next

    def _parse_list_page_regex(self, html: str, page_url: str) -> list[dict]:
        out: list[dict[str, Any]] = []
        page_id = self._extract_page_id(html)
        for href, title in re.findall(
            r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</a>',
            html,
            re.I | re.S,
        ):
            title_text = _clean_text(re.sub(r"<[^>]+>", " ", title))
            if not self._looks_like_annual_report(title_text, href):
                continue
            pdf_url = self._normalize_url(href)
            year = self._extract_year(title_text, pdf_url)
            filename = self._filename_from_url(pdf_url)
            listed_raw, listed_date = self._listed_date_from_url(pdf_url)
            out.append(
                {
                    "title": title_text or f"Annual Report {year}",
                    "pdf_url": pdf_url,
                    "url": pdf_url,
                    "source_page_url": page_url,
                    "source_page_id": page_id,
                    "section": "",
                    "report_year": year,
                    "post_number": year,
                    "external_id": filename or urlparse(pdf_url).path,
                    "original_filename": filename,
                    "listed_date_raw": listed_raw,
                    "listed_date": listed_date,
                    "list_text": title_text,
                }
            )
        return out

    def _looks_like_annual_report(self, title: str, href: str) -> bool:
        text = f"{title} {href}".lower()
        if ".pdf" not in text:
            return False
        if "annualreport" in text:
            return True
        return bool(re.search(r"\bannual\s+report\s+(19|20)\d{2}\b", text))

    def _normalize_url(self, href: str) -> str:
        url = urljoin(self.base_url, href)
        if url.startswith("http://www.astro.oma.be/"):
            url = "https://" + url[len("http://") :]
        return url

    # ------------------------------------------------------------------
    # detail/PDF parsing
    # ------------------------------------------------------------------

    def _fetch_pdf_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        pdf_url = item["pdf_url"]
        headers = self._curl_headers(pdf_url)
        pdf_bytes = self._curl(
            pdf_url,
            accept="application/pdf,*/*;q=0.8",
            binary=True,
        )
        if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
            raise RuntimeError("empty PDF response")

        pdfinfo = self._extract_pdfinfo(pdf_bytes)
        text = self._extract_pdf_text(pdf_bytes)
        abstract = self._abstract_from_text(text)

        original_filename = (
            self._filename_from_content_disposition(headers.get("content-disposition"))
            or item.get("original_filename")
            or self._filename_from_url(pdf_url)
        )

        pdf_date_raw = (
            pdfinfo.get("CreationDate")
            or pdfinfo.get("ModDate")
            or item.get("listed_date_raw")
            or item.get("report_year")
        )
        pdf_date = self._parse_date(pdf_date_raw) or self._year_date(
            item.get("report_year")
        )

        keywords_raw = pdfinfo.get("Keywords", "")
        keywords = self._keywords_from_pdfinfo(keywords_raw)

        return {
            "abstract": abstract,
            "published_date": pdf_date,
            "published_date_raw": pdf_date_raw,
            "listed_date": item.get("listed_date") or pdf_date,
            "listed_date_raw": item.get("listed_date_raw") or pdf_date_raw,
            "original_filename": original_filename,
            "pdfinfo": pdfinfo,
            "keywords": keywords,
            "pdf_text_sample": text[:1000],
            "content_length": headers.get("content-length"),
            "content_type": headers.get("content-type"),
            "content_disposition": headers.get("content-disposition"),
        }

    def _extract_pdfinfo(self, pdf_bytes: bytes) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["pdfinfo", "-"],
                input=pdf_bytes,
                capture_output=True,
                timeout=45,
            )
        except Exception as exc:
            print(f"[astro-oma-be-en] pdfinfo failed: {exc}")
            return {}

        raw = result.stdout.decode("utf-8", errors="replace")
        info: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            info[key.strip()] = value.strip()
        return info

    def _extract_pdf_text(self, pdf_bytes: bytes) -> str:
        try:
            result = subprocess.run(
                [
                    "pdftotext",
                    "-f",
                    "1",
                    "-l",
                    str(self.PDF_TEXT_LAST_PAGE),
                    "-layout",
                    "-",
                    "-",
                ],
                input=pdf_bytes,
                capture_output=True,
                timeout=90,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            print(f"[astro-oma-be-en] pdftotext failed: {exc}")

        try:
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(pdf_bytes))
            chunks: list[str] = []
            for page in reader.pages[: self.PDF_TEXT_LAST_PAGE]:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(chunks)
        except Exception as exc:
            print(f"[astro-oma-be-en] pypdf fallback failed: {exc}")
            return ""

    def _abstract_from_text(self, text: str) -> str:
        paragraphs = self._pdf_paragraphs(text)
        for paragraph in paragraphs:
            low = paragraph.lower()
            if "foreword" in low and len(paragraph) >= self.MIN_ABSTRACT_CHARS:
                return self._trim_abstract(paragraph)

        for paragraph in paragraphs:
            low = paragraph.lower()
            if (
                "mission and objectives" in low
                or low.startswith("introduction")
                or "scientific activities" in low
            ) and len(paragraph) >= self.MIN_ABSTRACT_CHARS:
                return self._trim_abstract(paragraph)

        for paragraph in paragraphs:
            if len(paragraph) >= self.MIN_ABSTRACT_CHARS:
                return self._trim_abstract(paragraph)
        return ""

    def _pdf_paragraphs(self, text: str) -> list[str]:
        text = text.replace("\r", "\n").replace("\x0c", "\n\n")
        paragraphs: list[str] = []
        current = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                if current:
                    paragraphs.append(_clean_text(current))
                    current = ""
                continue
            if self._skip_pdf_line(line):
                continue
            if current:
                if current.endswith("-"):
                    current = current[:-1] + line
                else:
                    current += " " + line
            else:
                current = line
        if current:
            paragraphs.append(_clean_text(current))

        out: list[str] = []
        for paragraph in paragraphs:
            if self._skip_pdf_paragraph(paragraph):
                continue
            out.append(paragraph)
        return out

    def _skip_pdf_line(self, line: str) -> bool:
        if re.fullmatch(r"\d{1,4}", line):
            return True
        low = line.lower()
        skip_exact = {
            "annual report",
            "rapport annuel",
            "jaarverslag",
            "scientific report",
            "summary",
            "table of contents",
            "contents",
        }
        return low in skip_exact

    def _skip_pdf_paragraph(self, paragraph: str) -> bool:
        low = paragraph.lower()
        if len(paragraph) < self.MIN_ABSTRACT_CHARS:
            return True
        if low.startswith("cover illustration"):
            return True
        if "................................................................" in paragraph:
            return True
        if re.search(r"\b(department|section)\s+\d\b", low) and len(paragraph) < 300:
            return True
        if low.count(" publications") >= 4:
            return True
        return False

    def _trim_abstract(self, text: str) -> str:
        text = _clean_text(text)
        text = re.sub(r"\s+Ronald Van der Linden\s+Director General.*$", "", text)
        if len(text) <= 1600:
            return text
        cut = text[:1600]
        last_stop = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("! "), cut.rfind("? "))
        if last_stop >= 500:
            return cut[: last_stop + 1].strip()
        return cut.strip()

    # ------------------------------------------------------------------
    # field helpers
    # ------------------------------------------------------------------

    def _extract_year(self, title: str, url: str) -> str | None:
        m = re.search(r"\b(19|20)\d{2}\b", f"{title} {url}")
        return m.group(0) if m else None

    def _year_date(self, year: str | None) -> str | None:
        if year and re.fullmatch(r"(19|20)\d{2}", year):
            return f"{year}-12-31"
        return None

    def _listed_date_from_url(self, url: str) -> tuple[str | None, str | None]:
        m = re.search(r"/wp-content/uploads/((19|20)\d{2})/(\d{2})/", url)
        if not m:
            return None, None
        raw = f"{m.group(1)}/{m.group(3)}"
        return raw, f"{m.group(1)}-{m.group(3)}-01"

    def _parse_date(self, raw: str | None) -> str | None:
        if not raw:
            return None
        value = str(raw).strip()
        m = re.search(r"\b((19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", value)
        if m:
            return f"{m.group(1)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"

        m = re.search(r"D:((19|20)\d{2})(\d{2})(\d{2})", value)
        if m:
            return f"{m.group(1)}-{m.group(3)}-{m.group(4)}"

        m = re.search(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
            r"(\d{1,2}).*?\s+((19|20)\d{2})\b",
            value,
            re.I,
        )
        if m:
            months = {
                "jan": "01",
                "feb": "02",
                "mar": "03",
                "apr": "04",
                "may": "05",
                "jun": "06",
                "jul": "07",
                "aug": "08",
                "sep": "09",
                "oct": "10",
                "nov": "11",
                "dec": "12",
            }
            month = months[m.group(1)[:3].lower()]
            return f"{m.group(3)}-{month}-{int(m.group(2)):02d}"

        m = re.fullmatch(r"(19|20)\d{2}", value)
        if m:
            return f"{value}-12-31"
        return None

    def _filename_from_url(self, url: str) -> str | None:
        tail = os.path.basename(urlparse(url).path.rstrip("/"))
        tail = unquote(tail)
        return tail or None

    def _filename_from_content_disposition(self, value: str | None) -> str | None:
        if not value:
            return None
        msg = Message()
        msg["content-disposition"] = value
        filename = msg.get_filename()
        return unquote(filename) if filename else None

    def _keywords_from_pdfinfo(self, value: str) -> str:
        if value:
            parts = [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
            if parts:
                return ", ".join(parts)
        return "annual report, astronomy, geophysics, Royal Observatory of Belgium"

    # ------------------------------------------------------------------
    # crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        started_at = time.time()
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > self.MAX_PAGES:
                    print(
                        f"[astro-oma-be-en] safety cap of {self.MAX_PAGES} pages reached"
                    )
                    break
                if time.time() - started_at > self.MAX_WALL_SECONDS - 30:
                    print("[astro-oma-be-en] wall-clock budget reached, stopping cleanly")
                    break

                if page == 1 or page % 10 == 0:
                    print(
                        f"[astro-oma-be-en] page {page}: saved {saved}/{limit_or_inf}"
                    )

                list_url = self._list_url_for_page(page)
                raw = self._curl(list_url)
                if not isinstance(raw, str) or not raw.strip():
                    print(f"[astro-oma-be-en] empty list page {page}, stopping")
                    break

                items, has_next = self._parse_list_page(raw, list_url)
                page_new = 0

                for index, item in enumerate(items, start=1):
                    if limit is not None and saved >= limit:
                        break
                    pdf_url = item.get("pdf_url")
                    if not pdf_url or pdf_url in seen_urls:
                        continue
                    seen_urls.add(pdf_url)
                    page_new += 1

                    try:
                        time.sleep(self._delay)
                        detail = self._fetch_pdf_detail(item)
                        abstract = detail.get("abstract") or ""
                        if len(abstract) < 50:
                            print(
                                f"[astro-oma-be-en] item {index} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        report_year = item.get("report_year")
                        post_number = item.get("post_number") or report_year
                        external_id = item.get("external_id") or pdf_url
                        listed_date = detail.get("listed_date")
                        published_date = detail.get("published_date")
                        original_filename = detail.get("original_filename")

                        metadata = {
                            "posted_date": detail.get("listed_date_raw") or listed_date,
                            "listed_date": listed_date,
                            "originalFilename": original_filename,
                            "journal_raw": None,
                            "series": "Annual reports",
                            "volume": report_year,
                            "issue": None,
                            "wp_page_id": item.get("source_page_id") or self.WP_PAGE_ID,
                            "source_page_url": item.get("source_page_url"),
                            "source_endpoint": self.START_URL,
                            "detail_endpoint": pdf_url,
                            "node_id": item.get("source_page_id") or self.WP_PAGE_ID,
                            "native_id": external_id,
                            "post_number": post_number,
                            "report_year": report_year,
                            "section": item.get("section"),
                            "list_text": item.get("list_text"),
                            "listed_date_raw": detail.get("listed_date_raw"),
                            "published_date_raw": detail.get("published_date_raw"),
                            "pdfinfo": detail.get("pdfinfo"),
                            "pdf_text_sample": detail.get("pdf_text_sample"),
                            "content_length": detail.get("content_length"),
                            "content_type": detail.get("content_type"),
                            "content_disposition": detail.get("content_disposition"),
                        }

                        self._save_paper(
                            {
                                "id": f"{self.site_id}:{external_id}",
                                "site_id": self.site_id,
                                "external_id": external_id,
                                "post_number": post_number,
                                "title": item.get("title"),
                                "abstract": abstract,
                                "published_date": published_date,
                                "listed_date": listed_date,
                                "posted_date": listed_date,
                                "authors": None,
                                "publisher": self.PUBLISHER,
                                "department": None,
                                "journal": None,
                                "url": item.get("url"),
                                "pdf_url": pdf_url,
                                "keywords": detail.get("keywords"),
                                "category": "Annual Reports",
                                "doi": None,
                                "original_filename": original_filename,
                                "metadata": _json_dumps(metadata),
                            }
                        )
                        saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[astro-oma-be-en] item {index} failed: {exc}")
                        continue

                if not items:
                    print(f"[astro-oma-be-en] page {page}: no records, stopping")
                    break
                if page_new == 0:
                    print(
                        f"[astro-oma-be-en] page {page}: no new records after "
                        "deduplication, stopping"
                    )
                    break
                if not has_next:
                    break
                page += 1
        except KeyboardInterrupt:
            raise

        print(f"[astro-oma-be-en] crawl complete: saved {saved}/{limit_or_inf}")
        return saved
