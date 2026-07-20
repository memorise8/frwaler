# -*- coding: utf-8 -*-
"""NHGRI budget and financial information crawler.

Discovery notes:
  - The target page is a Drupal 10 rendered HTML page (node 18071).
  - Drupal JSON:API routes for this node are not publicly exposed.
  - The real list endpoint is the rendered HTML page.
  - The current-document route (/FY2025CJ) is a Drupal redirect detail
    endpoint that resolves to the underlying PDF.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)
from crawler.base_crawler import BaseCrawler


START_URL = "https://www.genome.gov/about-nhgri/Budget-Financial-Information"
SITE_ID = "genome-gov-about-nhgri"
BASE_URL = "https://www.genome.gov"
PUBLISHER = "National Human Genome Research Institute; National Institutes of Health"
DEPARTMENT = "National Human Genome Research Institute"
PAGE_NODE_ID = "18071"
MAX_PAGES = 200
MAX_SECONDS = 24.5 * 60
PDF_TEXT_PAGES = 5
PARSER_CHAIN = ("html5lib", "lxml", "html.parser")

PAGE_DESCRIPTION = (
    "Information about how the National Human Genome Research Institute "
    "receives its funding and decides how to spend its annual budget."
)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(raw: str) -> BeautifulSoup:
    last_exc: Exception | None = None
    for parser in PARSER_CHAIN:
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            last_exc = exc
            continue
    try:
        return BeautifulSoup(raw or "", "html.parser")
    except Exception:
        if last_exc:
            raise last_exc
        raise


def _iso_from_http_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except Exception:
        return None
    if not dt:
        return None
    date_s = dt.date().isoformat()
    if date_s == "1970-01-01":
        return None
    return date_s


def _iso_from_upload_path(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/(?:files|images)/(\d{4})-(\d{2})/", url)
    if match:
        return f"{match.group(1)}-{match.group(2)}-01"
    return None


def _parse_pdfinfo_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # pdfinfo usually prints: Mon Mar 11 22:09:10 2024 KST
    parts = raw.split()
    if len(parts) >= 5:
        normalized = " ".join(parts[:5])
        try:
            return datetime.strptime(normalized, "%a %b %d %H:%M:%S %Y").date().isoformat()
        except Exception:
            return None
    return None


def _fiscal_year(title: str, fallback: str | None = None) -> str | None:
    for value in (title, fallback or ""):
        match = re.search(r"\b(?:FY|Fiscal\s+Year)\s*(\d{2,4})\b", value, re.I)
        if not match:
            continue
        year = match.group(1)
        if len(year) == 2:
            year = f"20{year}" if int(year) <= 50 else f"19{year}"
        return year
    return None


def _slug(text: str) -> str:
    text = unquote(text or "")
    text = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", text)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return text.lower() or "document"


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    filename = unquote(path.split("/")[-1])
    return filename or None


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", value, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', value, re.I)
    if m:
        return unquote(m.group(1).strip())
    return None


def _category(title: str, section: str | None = None) -> str:
    lower = title.lower()
    if "justification of estimates" in lower or "congressional justification" in lower:
        return "Congressional Justification"
    if "director's statement" in lower or "budget request" in lower:
        return "Budget Request Statement"
    if "funding history" in lower:
        return "Funding History"
    if "funding by mechanisms" in lower:
        return "Funding Mechanisms"
    return section or "Budget and Financial Information"


class GenomeGovAboutNhgriCrawler(BaseCrawler):
    site_id = "genome-gov-about-nhgri"
    site_name = "Custom: genome-gov-about-nhgri"
    base_url = "https://www.genome.gov"

    def _curl_get(self, url: str, *, binary: bool = False, timeout: int = 60):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
            url,
        ]
        retry_delays = (1, 3, 9)
        attempts = len(retry_delays) + 1
        for attempt in range(1, attempts + 1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    if binary:
                        return result.stdout
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                msg = err or f"curl exited {result.returncode}"
            except Exception as exc:
                msg = str(exc)

            if attempt < attempts:
                wait = retry_delays[attempt - 1]
                print(
                    f"[{self.site_id}] curl GET failed ({attempt}/{attempts}) "
                    f"for {url}: {msg}; retrying in {wait}s"
                )
                time.sleep(wait)
            else:
                print(f"[{self.site_id}] curl GET failed after {attempts} attempts for {url}: {msg}")
        return None

    def _curl_head(self, url: str, timeout: int = 45) -> dict:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skIL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-w",
            "\nCURL_EFFECTIVE_URL:%{url_effective}\nCURL_HTTP_CODE:%{http_code}\n",
            url,
        ]
        retry_delays = (1, 3, 9)
        attempts = len(retry_delays) + 1
        for attempt in range(1, attempts + 1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw:
                    return self._parse_curl_head(raw)
                err = result.stderr.decode("utf-8", errors="replace").strip()
                msg = err or f"curl exited {result.returncode}"
            except Exception as exc:
                msg = str(exc)

            if attempt < attempts:
                wait = retry_delays[attempt - 1]
                print(
                    f"[{self.site_id}] curl HEAD failed ({attempt}/{attempts}) "
                    f"for {url}: {msg}; retrying in {wait}s"
                )
                time.sleep(wait)
            else:
                print(f"[{self.site_id}] curl HEAD failed after {attempts} attempts for {url}: {msg}")
        return {"headers": {}, "effective_url": url, "http_code": None, "raw": ""}

    @staticmethod
    def _parse_curl_head(raw: str) -> dict:
        blocks: list[dict] = []
        current: dict[str, str] = {}
        status = None
        effective_url = None
        http_code = None

        for line in raw.replace("\r\n", "\n").split("\n"):
            if line.startswith("CURL_EFFECTIVE_URL:"):
                effective_url = line.split(":", 1)[1].strip()
                continue
            if line.startswith("CURL_HTTP_CODE:"):
                http_code = line.split(":", 1)[1].strip()
                continue
            if line.startswith("HTTP/"):
                if current:
                    blocks.append({"status": status, "headers": current})
                current = {}
                parts = line.split()
                status = parts[1] if len(parts) > 1 else None
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                current[key.strip().lower()] = value.strip()

        if current:
            blocks.append({"status": status, "headers": current})

        final_block = blocks[-1] if blocks else {"status": status, "headers": {}}
        return {
            "headers": final_block.get("headers", {}),
            "status": final_block.get("status"),
            "effective_url": effective_url,
            "http_code": http_code,
            "blocks": blocks,
            "raw": raw,
        }

    def _extract_pdf(self, pdf_bytes: bytes) -> tuple[str, dict]:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(pdf_bytes)
                tmp = handle.name

            info: dict[str, str] = {}
            try:
                result = subprocess.run(
                    ["pdfinfo", tmp],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    info[key.strip()] = value.strip()
            except Exception as exc:
                print(f"[{self.site_id}] pdfinfo failed: {exc}")

            text = ""
            try:
                result = subprocess.run(
                    ["pdftotext", "-l", str(PDF_TEXT_PAGES), tmp, "-"],
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
                text = _clean(result.stdout.decode("utf-8", errors="replace"))
            except Exception as exc:
                print(f"[{self.site_id}] pdftotext failed: {exc}")

            return text, info
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    def _parse_list_page(self, html: str, page_url: str) -> tuple[list[dict], str | None]:
        soup = _make_soup(html)
        article = soup.select_one("article") or soup.select_one("main") or soup

        records: list[dict] = []
        seen: set[str] = set()
        section = ""
        fiscal_heading = ""

        for element in article.find_all(["h2", "h3", "h4", "h5", "a"]):
            name = (element.name or "").lower()
            text = _clean(element.get_text(" ", strip=True))

            if name in {"h2", "h3", "h4"} and text:
                section = text
                continue

            if name == "h5" and text:
                fiscal_heading = text
                continue

            if name != "a" or not element.get("href"):
                continue

            href = element.get("href", "").strip()
            if not href or href == "#":
                continue

            headline = element.select_one(".headline")
            title = _clean((headline or element).get_text(" ", strip=True))
            title = re.sub(r"^Download\s+", "", title, flags=re.I).strip()
            lower_title = title.lower()
            lower_href = href.lower()

            is_candidate = (
                lower_href.endswith(".pdf")
                or "fy2025cj" in lower_href
                or "justification of estimates" in lower_title
                or "director's statement" in lower_title
                or "funding history" in lower_title
                or "funding by mechanisms" in lower_title
            )
            if not is_candidate or not title:
                continue

            item_url = urljoin(page_url, href)
            if item_url in seen:
                continue
            seen.add(item_url)

            fiscal_year = _fiscal_year(title, fiscal_heading)
            records.append(
                {
                    "title": title,
                    "item_url": item_url,
                    "source_href": href,
                    "section": section or None,
                    "fiscal_heading": fiscal_heading or None,
                    "fiscal_year": fiscal_year,
                    "category": _category(title, section),
                }
            )

        next_link = soup.select_one(
            'a[rel="next"], li.pager__item--next a[href], a.pager__link--next[href]'
        )
        next_url = urljoin(page_url, next_link["href"]) if next_link and next_link.get("href") else None
        return records, next_url

    def _build_paper(self, item: dict) -> dict | None:
        head = self._curl_head(item["item_url"])
        headers = head.get("headers") or {}
        effective_url = head.get("effective_url") or item["item_url"]
        content_type = headers.get("content-type", "")

        pdf_url = effective_url if "application/pdf" in content_type.lower() else item["item_url"]
        if not pdf_url.lower().split("?", 1)[0].endswith(".pdf") and "application/pdf" not in content_type.lower():
            print(f"[{self.site_id}] Skipping non-PDF endpoint: {item['item_url']}")
            return None

        pdf_bytes = self._curl_get(item["item_url"], binary=True, timeout=90)
        if not pdf_bytes:
            print(f"[{self.site_id}] Skipping empty PDF response: {item['item_url']}")
            return None

        abstract, pdf_info = self._extract_pdf(pdf_bytes)
        if abstract and len(abstract) < 100:
            abstract = _clean(f"{abstract} {PAGE_DESCRIPTION} Document title: {item['title']}.")

        if len(abstract) < 50:
            print(
                f"[{self.site_id}] Skipping '{item['title'][:70]}': "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        filename = (
            _filename_from_content_disposition(headers.get("content-disposition"))
            or _filename_from_url(pdf_url)
            or _filename_from_url(item["item_url"])
            or f"{_slug(item['title'])}.pdf"
        )
        external_id = _slug(filename)
        fiscal_year = item.get("fiscal_year") or _fiscal_year(filename)
        post_number = fiscal_year or external_id

        upload_date = _iso_from_upload_path(pdf_url)
        last_modified_raw = headers.get("last-modified")
        last_modified_date = _iso_from_http_date(last_modified_raw)
        creation_date = _parse_pdfinfo_date(pdf_info.get("CreationDate"))
        listed_date = upload_date or last_modified_date
        published_date = creation_date or listed_date
        listed_raw = None
        if upload_date:
            listed_raw = re.search(r"/(?:files|images)/(\d{4}-\d{2})/", pdf_url or "")
            listed_raw = listed_raw.group(1) if listed_raw else upload_date
        elif last_modified_date:
            listed_raw = last_modified_raw

        keywords = [
            "NHGRI",
            "budget",
            "financial information",
            item.get("category") or "",
            f"fiscal year {fiscal_year}" if fiscal_year else "",
        ]
        keywords_s = ", ".join(k for k in keywords if k)

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": PAGE_NODE_ID,
            "drupal_internal__nid": PAGE_NODE_ID,
            "post_number": post_number,
            "raw_list_item": item,
            "source_page_url": START_URL,
            "source_href": item.get("source_href"),
            "source_section": item.get("section"),
            "fiscal_heading": item.get("fiscal_heading"),
            "fiscal_year": fiscal_year,
            "detail_endpoint": item.get("item_url"),
            "pdf_final_url": pdf_url,
            "http_code": head.get("http_code"),
            "response_headers": headers,
            "head_blocks": head.get("blocks"),
            "content_type": content_type,
            "content_length": headers.get("content-length"),
            "last_modified": last_modified_raw,
            "date_source": {
                "listed_date": (
                    "upload_path" if upload_date else "last_modified" if last_modified_date else "none"
                ),
                "published_date": "pdf_creation_date" if creation_date else "listed_date" if listed_date else "none",
            },
            "pdf_info": pdf_info,
            "department": DEPARTMENT,
            "discovery": {
                "list_endpoint": START_URL,
                "detail_endpoint_kind": "pdf_or_redirect",
                "jsonapi_status": "not exposed publicly",
            },
        }

        redirect_id = None
        for block in head.get("blocks") or []:
            block_headers = block.get("headers") or {}
            if block_headers.get("x-redirect-id"):
                redirect_id = block_headers.get("x-redirect-id")
                break
        if redirect_id:
            metadata["redirect_id"] = redirect_id

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": item["title"],
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": DEPARTMENT,
            "publisher": PUBLISHER,
            "department": DEPARTMENT,
            "journal": None,
            "url": item["item_url"],
            "pdf_url": pdf_url,
            "keywords": keywords_s,
            "category": item.get("category"),
            "doi": None,
            "original_filename": filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        page = 1
        page_url = START_URL
        limit_label = str(limit) if limit is not None else "inf"
        started_at = time.time()
        seen_urls: set[str] = set()
        seen_pages: set[str] = set()

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        while page <= MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - started_at
            if elapsed >= MAX_SECONDS:
                print(
                    f"[{self.site_id}] Approaching 25 minute wall-clock budget "
                    f"({elapsed / 60:.1f} min); stopping cleanly."
                )
                break

            if page_url in seen_pages:
                print(f"[{self.site_id}] Page loop detected at {page_url}; stopping.")
                break
            seen_pages.add(page_url)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            raw = self._curl_get(page_url, binary=False, timeout=60)
            if not raw:
                print(f"[{self.site_id}] Empty list page at page {page}; stopping.")
                break

            records, next_url = self._parse_list_page(raw, page_url)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping.")
                break

            page_new_records = 0
            for idx, item in enumerate(records, 1):
                if limit is not None and saved >= limit:
                    break

                dedupe_url = item.get("item_url")
                if dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                page_new_records += 1

                try:
                    if self._delay:
                        time.sleep(self._delay)
                    paper = self._build_paper(item)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            if page_new_records == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping.")
                break

            if not next_url:
                break

            page += 1
            page_url = next_url

        if page > MAX_PAGES:
            print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached; stopping.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
