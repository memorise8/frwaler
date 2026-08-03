# -*- coding: utf-8 -*-
"""Crawler for Ministry of Transport documents and publications.

The live site is Silverstripe CMS. Its "See more" button calls the same
results URL through XHR with ``start`` and ``setsize`` parameters and expects a
JSON payload containing ``ResultsList`` HTML, ``totalItems`` and ``loadmore``.

Some server IPs receive an Incapsula block page. When that happens this
crawler falls back to archived Strategic Intentions search pages and archived
PDF redirects from the Wayback Machine. The fallback is deliberately limited by
what the archive exposes, but the live path paginates through the real endpoint.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

from crawler.base_crawler import BaseCrawler


_PARSERS = ("html5lib", "lxml", "html.parser")
_RETRY_DELAYS = (1, 3, 9)
_MONTHS = {
    "jan": "01",
    "january": "01",
    "feb": "02",
    "february": "02",
    "mar": "03",
    "march": "03",
    "apr": "04",
    "april": "04",
    "may": "05",
    "jun": "06",
    "june": "06",
    "jul": "07",
    "july": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "sept": "09",
    "september": "09",
    "oct": "10",
    "october": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}


class _ResolvedDownload:
    def __init__(self, pdf_url: str | None,
                 original_filename: str | None,
                 archive_pdf_url: str | None):
        self.pdf_url = pdf_url
        self.original_filename = original_filename
        self.archive_pdf_url = archive_pdf_url


def _make_soup(raw: str):
    """Parse HTML with a tolerant parser fallback chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None

    for parser in _PARSERS:
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception:
            continue
    return None


def _clean_text(value: str | None) -> str:
    text = unescape(value or "")
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def _strip_wayback_url(url: str) -> str:
    """Convert a Wayback-rewritten URL back to its original site URL."""
    if not url:
        return ""
    url = unescape(url)
    url = re.sub(r"^https?://web\.archive\.org/web/\d+[a-z_]*?/", "", url)
    url = re.sub(r"^/web/\d+[a-z_]*?/", "", url)
    return url


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _is_incapsula(text: str | None) -> bool:
    if not text:
        return False
    low = text[:3000].lower()
    return (
        "incapsula" in low
        or "_incapsula_resource" in low
        or "request unsuccessful. incapsula" in low
        or "incident_id=" in low
    )


def _parse_date(day: str | None = None, month_year: str | None = None,
                raw: str | None = None) -> str | None:
    """Return ISO YYYY-MM-DD for site date strings such as '13' + 'Mar 2026'."""
    raw = _clean_text(raw)
    if raw:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

        m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", raw)
        if m:
            month = _MONTHS.get(m.group(2).lower())
            if month:
                return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"

        m = re.search(r"([A-Za-z]+)\s+(\d{4})", raw)
        if m:
            month = _MONTHS.get(m.group(1).lower())
            if month:
                return f"{m.group(2)}-{month}-01"

    day = _clean_text(day)
    month_year = _clean_text(month_year)
    if day and month_year:
        m = re.search(r"([A-Za-z]+)\s+(\d{4})", month_year)
        if m:
            month = _MONTHS.get(m.group(1).lower())
            if month:
                return f"{m.group(2)}-{month}-{day.zfill(2)}"
    return None


class TransportGovtNZDocumentsAndPublicCrawler(BaseCrawler):
    site_id = "transport-govt-nz-documents-and-public"
    site_name = "Custom: transport-govt-nz-documents-and-public"
    base_url = "https://www.transport.govt.nz"

    _START_URL = (
        "https://www.transport.govt.nz/documents-and-publications/results"
        "?Keyword=&TopicID=&DocumentTypeID=222&SortBy=NewestToOldest"
        "&action_results=Search"
    )
    _ALT_STRATEGIC_URL = (
        "https://www.transport.govt.nz/about-us/what-we-do/"
        "strategic-intentions-documents-search/SearchForm"
        "?DocumentTypeID=222&action_results=Search"
    )
    _ALT_STRATEGIC_RESULTS_URL = (
        "https://www.transport.govt.nz/about-us/what-we-do/"
        "strategic-intentions-documents-search/results"
    )
    _CDX_API = "http://web.archive.org/cdx/search/cdx"
    _WAYBACK = "https://web.archive.org/web"
    _PAGE_SIZE = 20
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_text(self, url: str, *, headers: dict[str, str] | None = None,
                   timeout: int = 35, retries: int = 3) -> str | None:
        cmd = [
            "curl",
            "-skL",
            "--tls-max",
            "1.3",
            "--compressed",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept-Language: en-NZ,en;q=0.9",
        ]
        for key, value in (headers or {}).items():
            cmd.extend(["-H", f"{key}: {value}"])
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 5
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                if result.returncode == 0 and attempt == retries - 1:
                    return body
                if attempt < retries - 1:
                    wait = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    print(
                        f"[{self.site_id}] empty response "
                        f"(attempt {attempt + 1}/{retries}) for {url}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    print(
                        f"[{self.site_id}] curl error "
                        f"(attempt {attempt + 1}/{retries}) for {url}: {exc}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _curl_head(self, url: str, *, timeout: int = 35, retries: int = 3) -> str | None:
        cmd = [
            "curl",
            "-skL",
            "--tls-max",
            "1.3",
            "--max-time",
            str(timeout),
            "-I",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                raise RuntimeError("empty HEAD response")
            except Exception as exc:
                if attempt < retries - 1:
                    wait = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    print(
                        f"[{self.site_id}] HEAD error "
                        f"(attempt {attempt + 1}/{retries}) for {url}: {exc}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] HEAD failed after 3 attempts for {url}: {exc}")
        return None

    def _pdf_text(self, url: str, *, timeout: int = 70) -> str | None:
        """Fetch a PDF through curl and pipe it through pdftotext."""
        curl_cmd = [
            "curl",
            "-skL",
            "--tls-max",
            "1.3",
            "--compressed",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                pdf = subprocess.run(
                    curl_cmd, capture_output=True, timeout=timeout + 5
                )
                if not pdf.stdout or pdf.stdout[:5] != b"%PDF-":
                    raise RuntimeError("download did not return a PDF")
                text = subprocess.run(
                    ["pdftotext", "-f", "3", "-l", "7", "-", "-"],
                    input=pdf.stdout,
                    capture_output=True,
                    timeout=35,
                )
                if text.returncode == 0 and text.stdout.strip():
                    return text.stdout.decode("utf-8", errors="replace")
                text = subprocess.run(
                    ["pdftotext", "-f", "1", "-l", "5", "-", "-"],
                    input=pdf.stdout,
                    capture_output=True,
                    timeout=35,
                )
                if text.returncode == 0 and text.stdout.strip():
                    return text.stdout.decode("utf-8", errors="replace")
                raise RuntimeError("pdftotext returned no text")
            except Exception as exc:
                if attempt < 2:
                    wait = _RETRY_DELAYS[attempt]
                    print(
                        f"[{self.site_id}] PDF text error "
                        f"(attempt {attempt + 1}/3) for {url}: {exc}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] PDF text failed for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # URL and endpoint helpers
    # ------------------------------------------------------------------

    def _page_url(self, start: int = 0) -> str:
        pairs = parse_qsl(urlparse(self._START_URL).query, keep_blank_values=True)
        params = dict(pairs)
        params["start"] = str(start)
        params["setsize"] = str(self._PAGE_SIZE)
        parts = list(urlparse(self._START_URL))
        parts[4] = urlencode(params)
        return urlunparse(parts)

    def _ajax_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": self._START_URL,
            "X-Requested-With": "XMLHttpRequest",
        }

    def _canonical_url(self, href: str) -> str:
        href = _strip_wayback_url(href)
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return urljoin(self.base_url, href)

    def _archive_url(self, timestamp: str, original_url: str) -> str:
        return f"{self._WAYBACK}/{timestamp}/{original_url}"

    def _cdx_snapshots(self, url_pattern: str, limit: int = 10) -> list[dict[str, str]]:
        cdx_url = (
            f"{self._CDX_API}?url={quote(url_pattern, safe=':/?&=*')}"
            f"&output=json&limit={limit}&fl=timestamp,original,statuscode"
            f"&filter=statuscode:200&from=20240101&collapse=urlkey"
        )
        raw = self._curl_text(
            cdx_url,
            headers={"Accept": "application/json"},
            timeout=60,
            retries=2,
        )
        if not raw or not raw.lstrip().startswith("["):
            return []
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] CDX JSON parse error: {exc}")
            return []
        if len(rows) < 2:
            return []
        header = rows[0]
        return [dict(zip(header, row)) for row in rows[1:]]

    def _resolve_download(self, url: str, archive_url: str | None = None) -> _ResolvedDownload:
        candidates = [archive_url, url] if archive_url else [url]
        for candidate in candidates:
            if not candidate:
                continue
            headers = self._curl_head(candidate)
            if not headers:
                continue

            original_pdf = None
            for match in re.finditer(r"<([^>]+)>;\s*rel=\"original\"", headers):
                if ".pdf" in match.group(1).lower():
                    original_pdf = match.group(1)
            if not original_pdf:
                locs = re.findall(r"(?im)^location:\s*(\S+)\s*$", headers)
                for loc in reversed(locs):
                    clean = self._canonical_url(loc)
                    if ".pdf" in clean.lower():
                        original_pdf = clean
                        break

            final_archive = None
            m = re.search(
                r"(?im)^location:\s*(https://web\.archive\.org/web/\d+/[^\r\n]+\.pdf)\s*$",
                headers,
            )
            if m:
                final_archive = m.group(1).strip()
            elif candidate and ".pdf" in candidate.lower():
                final_archive = candidate

            pdf_url = original_pdf or (url if ".pdf" in url.lower() else None)
            filename = _filename_from_url(pdf_url) or _filename_from_url(final_archive)
            return _ResolvedDownload(pdf_url, filename, final_archive)

        fallback_pdf = url if ".pdf" in url.lower() else None
        return _ResolvedDownload(fallback_pdf, _filename_from_url(fallback_pdf), archive_url)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _records_from_response(self, raw: str, source_url: str) -> tuple[list[dict[str, Any]], bool | None]:
        if not raw:
            return [], None
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            try:
                data = json.loads(raw)
                html = data.get("ResultsList") or data.get("results") or ""
                return self._parse_listing_html(html, source_url), data.get("loadmore")
            except Exception as exc:
                print(f"[{self.site_id}] JSON response parse failed: {exc}")
                return [], None
        return self._parse_listing_html(raw, source_url), self._has_next_from_html(raw)

    def _parse_listing_html(self, raw: str, source_url: str) -> list[dict[str, Any]]:
        soup = _make_soup(raw)
        if soup is None:
            return []

        records: list[dict[str, Any]] = []
        for item in soup.select(".listing-item"):
            try:
                data = item.find(id=re.compile(r"^publication-\d+$"))
                if data is None:
                    continue
                external_id = data.get("id", "").replace("publication-", "").strip()
                if not external_id:
                    continue

                title = _clean_text(
                    item.select_one(".listing-title").get_text(" ", strip=True)
                    if item.select_one(".listing-title")
                    else ""
                )
                desc_el = item.select_one(".listing-description")
                abstract = _clean_text(desc_el.get_text(" ", strip=True) if desc_el else "")

                day_el = item.select_one(".datestamp-num--day")
                month_el = item.select_one(".datestamp-month")
                date_raw = _clean_text(
                    f"{day_el.get_text(' ', strip=True) if day_el else ''} "
                    f"{month_el.get_text(' ', strip=True) if month_el else ''}"
                )
                published_date = _parse_date(
                    day_el.get_text(" ", strip=True) if day_el else None,
                    month_el.get_text(" ", strip=True) if month_el else None,
                    date_raw,
                )

                dl = item.find("a", href=re.compile(r"/download/\d+"))
                download_url = self._canonical_url(dl.get("href", "")) if dl else None
                archive_download_url = None
                if dl and "web.archive.org" in dl.get("href", ""):
                    archive_download_url = urljoin("https://web.archive.org", dl.get("href"))
                file_text = _clean_text(dl.get_text(" ", strip=True) if dl else "")
                file_type = None
                m = re.search(r"Download\s+([A-Za-z0-9]+)", file_text, re.I)
                if m:
                    file_type = m.group(1).upper()

                labels = []
                for label in item.select(".proactiverelease"):
                    text = _clean_text(label.get_text(" ", strip=True))
                    if text and text not in labels:
                        labels.append(text)

                records.append({
                    "external_id": external_id,
                    "post_number": external_id if external_id.isdigit() else None,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "date_raw": date_raw,
                    "url": f"{self._START_URL}#publication-{external_id}",
                    "download_url": download_url,
                    "archive_download_url": archive_download_url,
                    "file_type": file_type,
                    "category": "; ".join(labels) if labels else "Strategic Intentions",
                    "source_url": source_url,
                    "source": "live-list",
                })
            except Exception as exc:
                print(f"[{self.site_id}] parse item failed: {exc}")
                continue
        return records

    def _parse_common_downloads(self, raw: str, source_url: str,
                                archive_timestamp: str | None = None) -> list[dict[str, Any]]:
        soup = _make_soup(raw)
        if soup is None:
            return []

        records: list[dict[str, Any]] = []
        for link in soup.select("a.link--block[id^='publication-commondownloads-']"):
            try:
                external_id = link.get("id", "").rsplit("-", 1)[-1]
                doc_type = _clean_text(
                    link.select_one(".linkblock-headermeta").get_text(" ", strip=True)
                    if link.select_one(".linkblock-headermeta")
                    else ""
                )
                if "strategic intentions" not in doc_type.lower():
                    continue

                title = _clean_text(
                    link.select_one(".linkblock-title").get_text(" ", strip=True)
                    if link.select_one(".linkblock-title")
                    else ""
                )
                href = link.get("href", "")
                download_url = self._canonical_url(href)
                archive_download_url = None
                if href.startswith("/web/"):
                    archive_download_url = urljoin("https://web.archive.org", href)
                elif archive_timestamp:
                    archive_download_url = self._archive_url(archive_timestamp, download_url)

                file_text = _clean_text(link.get_text(" ", strip=True))
                file_type = None
                m = re.search(r"Download\s+([A-Za-z0-9]+)", file_text, re.I)
                if m:
                    file_type = m.group(1).upper()

                published_date = self._date_from_title(title)
                records.append({
                    "external_id": external_id,
                    "post_number": external_id if external_id.isdigit() else None,
                    "title": title,
                    "abstract": "",
                    "published_date": published_date,
                    "listed_date": published_date,
                    "date_raw": published_date or "",
                    "url": f"{self._ALT_STRATEGIC_URL}#publication-commondownloads-{external_id}",
                    "download_url": download_url,
                    "archive_download_url": archive_download_url,
                    "file_type": file_type,
                    "category": doc_type or "Strategic Intentions",
                    "source_url": source_url,
                    "source": "wayback-common-downloads",
                })
            except Exception as exc:
                print(f"[{self.site_id}] common download parse failed: {exc}")
                continue
        return records

    def _has_next_from_html(self, raw: str) -> bool | None:
        soup = _make_soup(raw)
        if soup is None:
            return None
        button = soup.select_one("[data-search-ajax]")
        if button is not None:
            return True
        tally = soup.select_one("[data-search-resulttally]")
        total = soup.select_one("[data-search-totalresults]")
        try:
            if tally and total:
                return int(tally.get("value", "0")) < int(total.get("value", "0"))
        except ValueError:
            return None
        return False

    def _date_from_title(self, title: str) -> str | None:
        m = re.search(r"(20\d{2}|19\d{2})\s*[–-]\s*(?:\d{2}|20\d{2}|19\d{2})", title)
        if m:
            return f"{m.group(1)}-01-01"
        m = re.search(r"(20\d{2}|19\d{2})", title)
        if m:
            return f"{m.group(1)}-01-01"
        return None

    def _abstract_from_pdf_text(self, text: str | None) -> tuple[str | None, str | None]:
        if not text:
            return None, None

        cleaned = _clean_text(text)
        date = _parse_date(raw=cleaned)
        starts = [
            "The New Zealand transport system",
            "The Ministry and the wider",
            "A productive, safe and efficient transport system",
            "Transport underpins New Zealanders",
            "The Ministry is the Government",
            "We are the Government",
            "The Statement of Intent",
        ]
        pos = -1
        for marker in starts:
            found = cleaned.find(marker)
            if found >= 0 and (pos < 0 or found < pos):
                pos = found
        if pos > 0:
            cleaned = cleaned[pos:]
        cleaned = re.sub(r"Strategic Intentions \| Te Manatū Waka [^ ]+ \d+", " ", cleaned)
        cleaned = _clean_text(cleaned)
        if len(cleaned) > 1200:
            cleaned = cleaned[:1200].rsplit(" ", 1)[0]
        return (cleaned if cleaned else None), date

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def _record_to_paper(self, record: dict[str, Any]) -> dict[str, Any] | None:
        download_url = record.get("download_url")
        archive_download_url = record.get("archive_download_url")
        resolved = self._resolve_download(download_url, archive_download_url) if download_url else _ResolvedDownload(None, None, None)

        pdf_fetch_url = resolved.archive_pdf_url or archive_download_url or resolved.pdf_url or download_url
        abstract = _clean_text(record.get("abstract"))
        pdf_date = None
        if len(abstract) < 100 and pdf_fetch_url:
            pdf_text = self._pdf_text(pdf_fetch_url)
            pdf_abstract, pdf_date = self._abstract_from_pdf_text(pdf_text)
            if pdf_abstract and len(pdf_abstract) > len(abstract):
                abstract = pdf_abstract

        if len(abstract) < 50:
            print(
                f"[{self.site_id}] skip {record.get('external_id')}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        published_date = record.get("published_date") or pdf_date
        listed_date = record.get("listed_date") or published_date
        original_filename = resolved.original_filename or _filename_from_url(resolved.pdf_url)

        metadata = {
            "posted_date": record.get("date_raw") or listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "publication_id": record.get("external_id"),
            "download_id": record.get("external_id"),
            "post_number": record.get("post_number"),
            "document_type_id": "222",
            "document_type": "Strategic Intentions",
            "file_type": record.get("file_type"),
            "source": record.get("source"),
            "source_url": record.get("source_url"),
            "download_url": download_url,
            "archive_download_url": archive_download_url,
            "archive_pdf_url": resolved.archive_pdf_url,
        }

        return {
            "id": f"{self.site_id}:{record.get('external_id')}",
            "site_id": self.site_id,
            "external_id": str(record.get("external_id")),
            "post_number": record.get("post_number"),
            "title": record.get("title"),
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": "Ministry of Transport New Zealand",
            "department": None,
            "journal": None,
            "url": record.get("url"),
            "pdf_url": resolved.pdf_url or download_url,
            "keywords": "Strategic Intentions",
            "category": record.get("category") or "Strategic Intentions",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _save_record(self, record: dict[str, Any]) -> bool:
        paper = self._record_to_paper(record)
        if paper is None:
            return False
        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        def budget_left() -> bool:
            if time.time() - start_time < self._MAX_SECONDS - 30:
                return True
            print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
            return False

        try:
            saved = self._crawl_live(limit, seen_urls, start_time)
            if limit is not None and saved >= limit:
                print(f"[{self.site_id}] done. total saved: {saved}")
                return saved

            if saved == 0 and budget_left():
                print(f"[{self.site_id}] live endpoint unavailable; trying archive fallback")
                saved += self._crawl_archive_fallback(
                    limit, seen_urls, saved, limit_or_inf, start_time
                )
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved

    def _crawl_live(self, limit, seen_urls: set[str], start_time: float) -> int:
        saved = 0
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page in range(1, self._MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time >= self._MAX_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            page_url = self._page_url((page - 1) * self._PAGE_SIZE)
            raw = self._curl_text(page_url, headers=self._ajax_headers(), retries=2)
            if not raw:
                print(f"[{self.site_id}] page {page}: empty response")
                break
            if _is_incapsula(raw):
                print(f"[{self.site_id}] page {page}: Incapsula block detected")
                break

            records, has_next = self._records_from_response(raw, page_url)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records")
                break

            new_this_page = 0
            for idx, record in enumerate(records, 1):
                if limit is not None and saved >= limit:
                    break
                dedupe = record.get("url") or record.get("download_url")
                if not dedupe or dedupe in seen_urls:
                    continue
                seen_urls.add(dedupe)
                new_this_page += 1

                try:
                    time.sleep(self._delay)
                    if self._save_record(record):
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item "
                        f"{record.get('external_id') or idx} failed: {exc}"
                    )
                    continue

            if new_this_page == 0:
                print(f"[{self.site_id}] page {page}: all records already seen")
                break
            if has_next is False:
                break
        else:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")

        return saved

    def _crawl_archive_fallback(self, limit, seen_urls: set[str], saved_so_far: int,
                                limit_or_inf: str, start_time: float) -> int:
        saved = 0
        known_snapshot = {
            "timestamp": "20250213211639",
            "original": self._ALT_STRATEGIC_RESULTS_URL,
            "statuscode": "200",
        }
        snapshot_batches = [[known_snapshot]]
        cdx_searched = False

        while snapshot_batches:
            snapshots = snapshot_batches.pop(0)
            for snap in reversed(snapshots):
                if limit is not None and saved_so_far + saved >= limit:
                    break
                if time.time() - start_time >= self._MAX_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
                    return saved

                timestamp = snap.get("timestamp", "")
                original = snap.get("original") or self._ALT_STRATEGIC_RESULTS_URL
                archive_page = self._archive_url(timestamp, original)
                raw = self._curl_text(archive_page, retries=2, timeout=60)
                if not raw or _is_incapsula(raw):
                    continue

                page_number = 1
                print(
                    f"[{self.site_id}] page {page_number}: "
                    f"saved {saved_so_far + saved}/{limit_or_inf}"
                )
                records = self._parse_listing_html(raw, original)
                if not records:
                    records = self._parse_common_downloads(raw, original, timestamp)
                if not records:
                    continue

                new_this_page = 0
                for idx, record in enumerate(records, 1):
                    if limit is not None and saved_so_far + saved >= limit:
                        break
                    dedupe = record.get("url") or record.get("download_url")
                    if not dedupe or dedupe in seen_urls:
                        continue
                    seen_urls.add(dedupe)
                    new_this_page += 1

                    try:
                        time.sleep(self._delay)
                        if self._save_record(record):
                            saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f"[{self.site_id}] item "
                            f"{record.get('external_id') or idx} failed: {exc}"
                        )
                        continue

                if limit is not None and saved_so_far + saved >= limit:
                    return saved
                if new_this_page == 0:
                    break

            if cdx_searched or (limit is not None and saved_so_far + saved >= limit):
                break
            cdx_searched = True
            snapshots = self._cdx_snapshots(
                "transport.govt.nz/about-us/what-we-do/"
                "strategic-intentions-documents-search/results*",
                limit=20,
            )
            if not snapshots:
                snapshots = self._cdx_snapshots(
                    "transport.govt.nz/about-us/what-we-do/"
                    "strategic-intentions-documents-search/SearchForm*",
                    limit=20,
                )
            if snapshots:
                snapshot_batches.append(snapshots)

        return saved
