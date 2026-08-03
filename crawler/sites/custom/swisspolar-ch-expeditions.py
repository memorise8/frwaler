# -*- coding: utf-8 -*-
"""Crawler for Swiss Polar Institute ACE publications.

List source:
    https://swisspolar.ch/wp-json/wp/v2/pages?slug=ace-publications

The WordPress page is the site's real publication list: each record is a
formatted citation paragraph. Per-record abstracts and normalized scholarly
metadata are available through DOI records in the Crossref works API.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import quote, unquote, urljoin, urlparse

# spec_from_file_location gives this module no package context, so use an
# absolute import after making the repository root importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_SITE_ID = "swisspolar-ch-expeditions"
_BASE_URL = "https://swisspolar.ch"
_START_URL = "https://swisspolar.ch/expeditions/ace/ace-publications/"
_WP_LIST_API = (
    "https://swisspolar.ch/wp-json/wp/v2/pages"
    "?slug=ace-publications&page={page}&per_page=1"
    "&_fields=id,date,date_gmt,modified,modified_gmt,slug,link,title,content"
)
_CROSSREF_API = "https://api.crossref.org/works/{doi}"
_PAGE_CAP = 200
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_CHARS = 100
_BACKOFFS = (1, 3, 9)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _clean_text(value):
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(raw):
    """Parse malformed HTML defensively: html5lib -> lxml -> html.parser."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup import failed: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _curl_get(url, *, timeout=30, retries=3, accept=None):
    """Fetch a URL with curl, TLS 1.3 max, and retry backoff."""
    headers = [
        "-H",
        f"User-Agent: {_USER_AGENT}",
        "-H",
        accept or "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
    ]
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--max-time",
        str(timeout),
        *headers,
        url,
    ]

    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout + 5,
            )
            body = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and body.strip():
                return body
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[{_SITE_ID}] curl empty/error for {url} "
                f"(attempt {attempt + 1}/{retries}, rc={result.returncode}) {stderr}"
            )
        except Exception as exc:
            print(
                f"[{_SITE_ID}] curl failed for {url} "
                f"(attempt {attempt + 1}/{retries}): {exc}"
            )

        if attempt < retries - 1:
            time.sleep(_BACKOFFS[min(attempt, len(_BACKOFFS) - 1)])

    return None


def _curl_head_filename(url, *, timeout=20):
    """Best-effort filename from Content-Disposition."""
    if not url:
        return None

    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skIL",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_USER_AGENT}",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except Exception:
        return None
    if result.returncode != 0:
        return None

    headers = result.stdout.decode("utf-8", errors="replace")
    match = re.search(
        r"(?im)^content-disposition:.*?filename\*?=(?:UTF-8''|\"?)([^\"\r\n;]+)",
        headers,
    )
    if not match:
        return None
    return unquote(match.group(1).strip().strip('"')) or None


def _filename_from_url(url):
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if not tail or len(tail) > 200:
        return None
    return tail


def _extract_doi(value):
    if not value:
        return None
    text = unquote(str(value)).strip()
    text = re.sub(r"(?i)^doi:\s*", "", text)
    if "doi.org/" in text.lower():
        text = re.split(r"(?i)doi\.org/", text, maxsplit=1)[-1]

    match = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", text, re.I)
    if not match:
        return None
    doi = match.group(1).strip()
    doi = doi.rstrip(").,;]}>")
    return doi or None


def _doi_url(doi):
    if not doi:
        return None
    return f"https://doi.org/{doi}"


def _strip_markup(value):
    if not value:
        return ""
    text = re.sub(r"(?i)</?(jats:)?p[^>]*>", " ", str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_text(text)


def _parse_date(raw):
    if not raw:
        return None
    text = _clean_text(raw)
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%Y"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y":
                return dt.strftime("%Y-01-01")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _date_from_parts(value):
    if not value:
        return None
    parts = value
    if isinstance(value, dict):
        parts = value.get("date-parts") or []
    if parts and isinstance(parts[0], list):
        parts = parts[0]
    if not parts:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError):
        return None


def _extract_last_updated(raw_text):
    text = _clean_text(raw_text)
    match = re.search(r"Last updated on\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text)
    if not match:
        return None, None
    raw = match.group(1)
    return _parse_date(raw), raw


def _join_title_parts(parts):
    cleaned = [_clean_text(part) for part in parts if _clean_text(part)]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]

    title = cleaned[0]
    for part in cleaned[1:]:
        if title and part and title[-1].isalnum() and part[0].islower():
            title += part
        else:
            title += " " + part
    return _clean_text(title)


def _first_nonempty(*values):
    for value in values:
        if value not in (None, "", []):
            return value
    return None


class SwissPolarChExpeditionsCrawler(BaseCrawler):
    site_id = "swisspolar-ch-expeditions"
    site_name = "Custom: swisspolar-ch-expeditions"
    base_url = "https://swisspolar.ch"

    def crawl(self, limit=None):
        saved = 0
        target = float("inf") if limit is None else max(0, int(limit))
        limit_or_inf = "inf" if limit is None else str(target)
        start = time.monotonic()
        seen_urls = set()

        try:
            for page in range(1, _PAGE_CAP + 1):
                if saved >= target:
                    break
                if time.monotonic() - start >= _MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget nearly exhausted; saved {saved}")
                    break
                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                records, has_next = self._fetch_list_page(page)
                if not records:
                    print(f"[{self.site_id}] page {page}: 0 records; stopping")
                    break

                new_on_page = 0
                for index, record in enumerate(records, start=1):
                    if saved >= target:
                        break
                    if time.monotonic() - start >= _MAX_WALL_SECONDS - 30:
                        print(f"[{self.site_id}] wall-clock budget nearly exhausted; saved {saved}")
                        return saved

                    key = record.get("url") or record.get("doi") or record.get("external_id")
                    if key and key in seen_urls:
                        continue
                    if key:
                        seen_urls.add(key)
                    new_on_page += 1

                    try:
                        detail = self._fetch_detail(record)
                        paper = self._build_paper(record, detail, index)
                        abstract = _clean_text(paper.get("abstract"))
                        if len(abstract) < _MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {record.get('post_number') or index} "
                                f"abstract too short ({len(abstract)} chars), skipping"
                            )
                            continue

                        paper["abstract"] = abstract
                        self._save_paper(paper)
                        saved += 1

                        sleep_for = self._delay if self._delay is not None else 1.0
                        if sleep_for > 0:
                            time.sleep(sleep_for)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        label = record.get("post_number") or record.get("doi") or index
                        print(f"[{self.site_id}] item {label} failed: {exc}")
                        continue

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page}: no new URLs; stopping")
                    break
                if not has_next:
                    break
            else:
                print(f"[{self.site_id}] reached safety page cap {_PAGE_CAP}")
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done: saved {saved}")
        return saved

    def _fetch_list_page(self, page):
        """Return publication citation records from the WordPress page API."""
        if page < 1:
            return [], False

        url = _WP_LIST_API.format(page=page)
        raw = _curl_get(url, accept="Accept: application/json,*/*;q=0.8")
        if not raw:
            return [], False

        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] list JSON parse failed on page {page}: {exc}")
            return [], False

        if not isinstance(data, list) or not data:
            return [], False

        page_obj = data[0]
        content = ((page_obj.get("content") or {}).get("rendered") or "")
        if not content:
            return [], False

        soup = _make_soup(content)
        if soup is None:
            return [], False

        page_text = soup.get_text(" ", strip=True)
        listed_date, listed_raw = _extract_last_updated(page_text)
        listed_date = listed_date or _parse_date(page_obj.get("modified"))
        listed_raw = listed_raw or page_obj.get("modified")

        page_meta = {
            "wp_page_id": page_obj.get("id"),
            "source_page_url": page_obj.get("link") or _START_URL,
            "source_page_api": url,
            "wp_date": page_obj.get("date"),
            "wp_date_gmt": page_obj.get("date_gmt"),
            "wp_modified": page_obj.get("modified"),
            "wp_modified_gmt": page_obj.get("modified_gmt"),
            "listed_date": listed_date,
            "listed_date_raw": listed_raw,
        }

        records = self._parse_publications(soup, page_meta)
        return records, False

    def _parse_publications(self, soup, page_meta):
        records = []
        current_project = None
        root = soup.find("div", class_="entry-content") or soup.find("main") or soup

        for tag in root.find_all(["h3", "h4", "p"]):
            if tag.name in ("h3", "h4"):
                heading = _clean_text(tag.get_text(" ", strip=True))
                if heading.lower().startswith("project:"):
                    current_project = heading
                continue

            links = tag.find_all("a", href=True)
            if not links:
                continue

            citation_text = _clean_text(tag.get_text(" ", strip=True))
            doi = None
            canonical_href = None
            title_parts = []

            for link in links:
                href = (link.get("href") or "").strip()
                link_text = _clean_text(link.get_text(" ", strip=True))
                if link_text:
                    title_parts.append(link_text)
                if not doi:
                    doi = _extract_doi(href) or _extract_doi(link_text)
                if not canonical_href and href:
                    canonical_href = href

            if not doi:
                doi = _extract_doi(citation_text)

            title = _join_title_parts(title_parts)
            if not title:
                continue

            url = _doi_url(doi)
            if not url and canonical_href:
                url = urljoin(_BASE_URL, canonical_href)
            if not url:
                continue

            year_match = re.search(r"\((\d{4})\)", citation_text)
            year = year_match.group(1) if year_match else None

            authors = ""
            if year_match:
                authors = citation_text[: year_match.start()].strip().rstrip(".")
            authors = _clean_text(authors)

            journal_raw = None
            for em in tag.find_all("em"):
                if em.find_parent("a") is None:
                    journal_raw = _clean_text(em.get_text(" ", strip=True))
                    break

            native_id = doi or url
            post_number = doi or urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

            record = {
                "external_id": native_id,
                "post_number": post_number,
                "doi": doi,
                "url": url,
                "title": title,
                "authors": authors,
                "year": year,
                "journal_raw": journal_raw,
                "project": current_project,
                "category": "ACE publications",
                "citation_text": citation_text,
                "citation_html": str(tag),
            }
            record.update(page_meta)
            records.append(record)

        return records

    def _fetch_detail(self, record):
        doi = record.get("doi")
        if not doi:
            return {}

        encoded = quote(doi, safe="")
        url = _CROSSREF_API.format(doi=encoded)
        raw = _curl_get(url, accept="Accept: application/json,*/*;q=0.8")
        if not raw:
            return {}

        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] Crossref JSON parse failed for {doi}: {exc}")
            return {}

        if data.get("status") != "ok" or not isinstance(data.get("message"), dict):
            print(f"[{self.site_id}] Crossref record unavailable for {doi}")
            return {"crossref_raw": data}

        msg = data["message"]
        abstract = _strip_markup(msg.get("abstract"))

        published_date = None
        date_key_used = None
        for key in ("published", "published-print", "published-online", "created", "deposited"):
            published_date = _date_from_parts(msg.get(key))
            if published_date:
                date_key_used = key
                break

        authors = []
        for author in msg.get("author") or []:
            if not isinstance(author, dict):
                continue
            literal = _clean_text(author.get("name"))
            given = _clean_text(author.get("given"))
            family = _clean_text(author.get("family"))
            name = literal or _clean_text(f"{given} {family}")
            if name:
                authors.append(name)

        journal = None
        container = msg.get("container-title") or []
        if container:
            journal = _clean_text(container[0])

        pdf_url = None
        for link in msg.get("link") or []:
            if not isinstance(link, dict):
                continue
            link_url = link.get("URL")
            content_type = (link.get("content-type") or "").lower()
            if link_url and ("pdf" in content_type or link_url.lower().split("?")[0].endswith(".pdf")):
                pdf_url = link_url
                break

        original_filename = _filename_from_url(pdf_url)
        if pdf_url and not original_filename:
            original_filename = _curl_head_filename(pdf_url)

        return {
            "doi": msg.get("DOI") or doi,
            "title": _clean_text((msg.get("title") or [""])[0]),
            "abstract": abstract,
            "published_date": published_date,
            "date_key_used": date_key_used,
            "authors": authors,
            "publisher": _clean_text(msg.get("publisher")),
            "journal": journal,
            "keywords": msg.get("subject") or [],
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "series": _clean_text(_first_nonempty(msg.get("series-title"), msg.get("series"))),
            "volume": _clean_text(msg.get("volume")),
            "issue": _clean_text(msg.get("issue")),
            "page": _clean_text(msg.get("page")),
            "crossref_type": msg.get("type"),
            "crossref_url": msg.get("URL"),
            "crossref_raw": msg,
        }

    def _build_paper(self, record, detail, index):
        doi = detail.get("doi") or record.get("doi")
        url = detail.get("crossref_url") or record.get("url") or _doi_url(doi)
        published_date = (
            detail.get("published_date")
            or (f"{record.get('year')}-01-01" if record.get("year") else None)
        )
        listed_date = record.get("listed_date")
        original_filename = detail.get("original_filename") or _filename_from_url(detail.get("pdf_url"))
        journal = detail.get("journal") or record.get("journal_raw")
        keywords = detail.get("keywords") or []
        if not keywords:
            keywords = ["ACE", "Antarctic Circumnavigation Expedition"]

        external_id = doi or record.get("external_id") or f"wp-{record.get('wp_page_id')}-{index}"
        post_number = record.get("post_number") or external_id

        metadata = {
            "posted_date": record.get("listed_date_raw") or listed_date,
            "originalFilename": original_filename,
            "journal_raw": record.get("journal_raw") or journal,
            "series": detail.get("series"),
            "volume": detail.get("volume"),
            "issue": detail.get("issue"),
            "page": detail.get("page"),
            "node_id": record.get("wp_page_id"),
            "wp_page_id": record.get("wp_page_id"),
            "wp_date": record.get("wp_date"),
            "wp_date_gmt": record.get("wp_date_gmt"),
            "wp_modified": record.get("wp_modified"),
            "wp_modified_gmt": record.get("wp_modified_gmt"),
            "source_page_url": record.get("source_page_url"),
            "source_page_api": record.get("source_page_api"),
            "citation_text": record.get("citation_text"),
            "citation_html": record.get("citation_html"),
            "project": record.get("project"),
            "doi": doi,
            "post_number": post_number,
            "crossref_date_key": detail.get("date_key_used"),
            "crossref_type": detail.get("crossref_type"),
            "crossref_url": detail.get("crossref_url"),
            "crossref_raw": detail.get("crossref_raw"),
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [])}

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": detail.get("title") or record.get("title"),
            "abstract": detail.get("abstract") or "",
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": detail.get("authors") or record.get("authors"),
            "publisher": detail.get("publisher") or "Swiss Polar Institute",
            "department": record.get("project"),
            "journal": journal,
            "url": url,
            "pdf_url": detail.get("pdf_url"),
            "keywords": ", ".join(_clean_text(k) for k in keywords if _clean_text(k)),
            "category": record.get("category"),
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }
