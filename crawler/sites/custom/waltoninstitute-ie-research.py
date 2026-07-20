# -*- coding: utf-8 -*-
"""Walton Institute publications crawler.

Starting page:
    https://waltoninstitute.ie/research/publications

The public Next.js page embeds ``apiURL=https://wordpress.waltoninstitute.ie``
and its page bundle fetches the WordPress custom post type REST endpoint:

    /wp-json/wp/v2/publication

Most records have empty WordPress ``content`` and ``excerpt`` fields. The real
publication metadata lives in ``post_type_data.data`` as rows such as
``author``, ``journal``, ``month``, ``year``, ``url``, ``doi``, ``volume`` and
``number``. For those sparse records we build a bibliographic abstract from
those site-native fields rather than saving a blank abstract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse


# Absolute import: spec_from_file_location has no package context.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


SITE_ID = "waltoninstitute-ie-research"
BASE_URL = "https://waltoninstitute.ie"
WP_BASE_URL = "https://wordpress.waltoninstitute.ie"
START_URL = "https://waltoninstitute.ie/research/publications"
PUBLICATION_API = f"{WP_BASE_URL}/wp-json/wp/v2/publication"

PER_PAGE = 100
SAFETY_CAP_PAGES = 200
WALL_CLOCK_BUDGET_SECONDS = 25 * 60
RETRY_WAITS = (1, 3, 9)
MIN_ABSTRACT_CHARS = 100

_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
_BS_PARSER_CACHE: list[str | None] = [None]


def _make_soup(raw: str):
    """Build BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    parsers = ["html5lib", "lxml", "html.parser"]
    cached = _BS_PARSER_CACHE[0]
    if cached:
        parsers = [cached] + [p for p in parsers if p != cached]

    for parser in parsers:
        try:
            soup = BeautifulSoup(raw or "", parser)
            _BS_PARSER_CACHE[0] = parser
            return soup
        except Exception as exc:
            print(f"[{SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
    return None


def _clean_text(value) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(value) -> str:
    if not value:
        return ""
    soup = _make_soup(str(value))
    if soup is not None:
        return _clean_text(soup.get_text(" ", strip=True))
    text = re.sub(r"<[^>]+>", " ", str(value))
    return _clean_text(text)


def _first_iso_date(raw) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"

    match = re.search(r"\b(19|20)\d{2}\b", text)
    if match:
        return f"{match.group(0)}-01-01"
    return None


def _month_number(raw) -> str | None:
    if raw is None:
        return None
    text = _clean_text(raw)
    if not text:
        return None
    if text.isdigit():
        month = int(text)
        if 1 <= month <= 12:
            return f"{month:02d}"
    month_map = {
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
    return month_map.get(text[:3].lower())


def _published_date(fields: dict[str, str], listed_date: str | None) -> str | None:
    explicit = _first_iso_date(fields.get("date_of_publication"))
    if explicit:
        return explicit

    year = fields.get("year") or fields.get("tssg-ayear")
    year_match = re.search(r"\b(19|20)\d{2}\b", str(year or ""))
    if year_match:
        month = _month_number(fields.get("month")) or "01"
        return f"{year_match.group(0)}-{month}-01"

    return listed_date


def _pivot_post_type_data(data) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key = _clean_text(entry.get("type"))
        if not key:
            continue
        value = entry.get("content")
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        value = _clean_text(value)
        if value:
            out[key] = value
    return out


def _normalise_authors(value: str | None) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    text = re.sub(r"\s+\band\b\s+", ", ", text, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"\s*[,;]\s*", text) if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return "; ".join(out) if out else None


def _extract_doi(*values) -> str | None:
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        parsed = urlparse(text)
        host = (parsed.netloc or "").lower()
        path = unquote(parsed.path or "")
        if host.endswith("doi.org") and path:
            candidate = path.lstrip("/")
            if candidate.lower().startswith("10."):
                return candidate.rstrip(".,;)")
        match = _DOI_RE.search(unquote(text))
        if match:
            return re.split(r"[?#]", match.group(1), maxsplit=1)[0].rstrip(".,;)")
    return None


def _absolute_url(value: str | None) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return urljoin(WP_BASE_URL, text)


def _filename_from_url(value: str | None) -> str | None:
    if not value:
        return None
    tail = unquote(urlparse(value).path.rstrip("/").rsplit("/", 1)[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', value, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip())
    return None


def _is_pdf_url(value: str | None) -> bool:
    if not value:
        return False
    path = urlparse(value).path.lower()
    return path.endswith(".pdf")


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class WaltoninstituteIeResearchCrawler(BaseCrawler):
    site_id = "waltoninstitute-ie-research"
    site_name = "Custom: waltoninstitute-ie-research"
    base_url = "https://waltoninstitute.ie"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        self.detail_delay = delay

    def _split_curl_output(self, text: str, fallback_url: str):
        status_marker = "\n__CURL_STATUS__:"
        effective_marker = "\n__CURL_EFFECTIVE_URL__:"

        status_idx = text.rfind(status_marker)
        if status_idx == -1:
            payload = text
            status = ""
            effective_url = fallback_url
        else:
            payload = text[:status_idx]
            rest = text[status_idx + len(status_marker):]
            effective_idx = rest.rfind(effective_marker)
            if effective_idx == -1:
                status = rest.strip()
                effective_url = fallback_url
            else:
                status = rest[:effective_idx].strip()
                effective_url = rest[effective_idx + len(effective_marker):].strip() or fallback_url

        parts = re.split(r"\r?\n\r?\n", payload, maxsplit=0)
        if len(parts) == 1:
            return {}, payload, status, effective_url

        body = parts[-1]
        header_block = ""
        for part in reversed(parts[:-1]):
            if part.lstrip().upper().startswith("HTTP/"):
                header_block = part
                break
        headers: dict[str, str] = {}
        for line in header_block.splitlines()[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return headers, body, status, effective_url

    def _curl_text(self, url: str, context: str, *, method="GET", timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "-i",
            "--max-time",
            str(timeout),
            "-w",
            "\n__CURL_STATUS__:%{http_code}\n__CURL_EFFECTIVE_URL__:%{url_effective}",
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: application/json,text/html,application/xhtml+xml,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        if method.upper() == "HEAD":
            cmd.insert(5, "-I")

        for attempt, wait in enumerate(RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
                text = (result.stdout or b"").decode("utf-8", errors="replace")
                headers, body, status, effective_url = self._split_curl_output(text, url)
                status_int = int(status) if status.isdigit() else 0
                if result.returncode == 0 and (body.strip() or method.upper() == "HEAD"):
                    if status_int < 500:
                        return {
                            "headers": headers,
                            "body": body,
                            "status": status_int,
                            "effective_url": effective_url,
                        }
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                err = stderr or f"curl exit={result.returncode} http={status or 'unknown'}"
                print(f"[{self.site_id}] curl {context} attempt {attempt}/3 failed: {err[:250]}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl {context} attempt {attempt}/3 failed: {exc}")

            if attempt < len(RETRY_WAITS):
                time.sleep(wait)

        print(f"[{self.site_id}] curl {context} failed after 3 attempts: {url}")
        return None

    def _curl_json(self, url: str, context: str):
        fetched = self._curl_text(url, context, timeout=45)
        if fetched is None:
            return None, {}, 0
        body = fetched["body"]
        try:
            data = json.loads(body)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] {context}: JSON parse failed: {exc}")
            return None, fetched["headers"], fetched["status"]
        return data, fetched["headers"], fetched["status"]

    def _content_disposition_filename(self, url: str | None) -> str | None:
        if not url:
            return None
        fetched = self._curl_text(url, "pdf headers", method="HEAD", timeout=30)
        if not fetched:
            return None
        return _filename_from_content_disposition(fetched["headers"].get("content-disposition"))

    def _list_url(self, page: int) -> str:
        fields = ",".join(["id", "date", "slug", "link"])
        return f"{PUBLICATION_API}?per_page={PER_PAGE}&page={page}&_fields={fields}"

    def _detail_url(self, item_id) -> str:
        fields = ",".join(
            [
                "id",
                "date",
                "date_gmt",
                "guid",
                "modified",
                "modified_gmt",
                "slug",
                "status",
                "type",
                "link",
                "title",
                "content",
                "excerpt",
                "author",
                "featured_media",
                "tags",
                "publications",
                "publication-author",
                "post_type_data",
                "acf",
                "yoast_head_json",
                "_links",
            ]
        )
        return f"{PUBLICATION_API}/{item_id}?_fields={fields}"

    def _build_abstract(
        self,
        *,
        title: str,
        content_text: str,
        excerpt_text: str,
        fields: dict[str, str],
        authors: str | None,
        published_date: str | None,
        detail_url: str,
        publication_url: str | None,
        doi: str | None,
    ) -> str:
        if len(content_text) >= MIN_ABSTRACT_CHARS:
            return content_text
        if len(excerpt_text) >= MIN_ABSTRACT_CHARS:
            return excerpt_text

        parts = [
            f"Title: {title}",
            f"Authors: {authors}" if authors else "",
            f"Published date: {published_date}" if published_date else "",
            f"Journal: {fields.get('journal')}" if fields.get("journal") else "",
            f"Publication type: {fields.get('type')}" if fields.get("type") else "",
            f"Volume: {fields.get('volume')}" if fields.get("volume") else "",
            f"Issue: {fields.get('number')}" if fields.get("number") else "",
            f"Pages: {fields.get('pages')}" if fields.get("pages") else "",
            f"ISSN: {fields.get('issn')}" if fields.get("issn") else "",
            f"DOI: {doi}" if doi else "",
            f"Research domain: {fields.get('tssg-domain')}" if fields.get("tssg-domain") else "",
            f"Project: {fields.get('tssg-project')}" if fields.get("tssg-project") else "",
            f"Note: {fields.get('note')}" if fields.get("note") else "",
            f"Publication URL: {publication_url}" if publication_url else "",
            f"Walton detail URL: {detail_url}",
        ]
        if content_text:
            parts.insert(0, content_text)
        elif excerpt_text and excerpt_text.replace(".", "").replace("…", "").strip():
            parts.insert(0, excerpt_text)
        return ". ".join(part for part in parts if part).strip()

    def _build_paper(self, item: dict) -> dict | None:
        wp_id = item.get("id")
        if wp_id is None:
            return None

        detail_url = _absolute_url(item.get("link")) or f"{WP_BASE_URL}/?p={wp_id}"
        title = _strip_html((item.get("title") or {}).get("rendered")) or "(untitled)"
        post_type_data = (item.get("post_type_data") or {}).get("data") or []
        fields = _pivot_post_type_data(post_type_data)

        if title == "(untitled)" and fields.get("title"):
            title = fields["title"]

        listed_date = _first_iso_date(item.get("date"))
        published_date = _published_date(fields, listed_date)

        raw_publication_url = (
            fields.get("publication_url")
            or fields.get("url")
            or fields.get("doi")
            or ""
        )
        publication_url = _absolute_url(raw_publication_url) if raw_publication_url else None
        doi = _extract_doi(fields.get("doi"), fields.get("url"), fields.get("publication_url"))
        authors = _normalise_authors(fields.get("author") or fields.get("author_s_"))

        content_text = _strip_html((item.get("content") or {}).get("rendered"))
        excerpt_text = _strip_html((item.get("excerpt") or {}).get("rendered"))
        if excerpt_text and excerpt_text.replace(".", "").replace("…", "").strip() == "":
            excerpt_text = ""

        abstract = self._build_abstract(
            title=title,
            content_text=content_text,
            excerpt_text=excerpt_text,
            fields=fields,
            authors=authors,
            published_date=published_date,
            detail_url=detail_url,
            publication_url=publication_url,
            doi=doi,
        )
        if len(abstract) < 50:
            print(f"[{self.site_id}] item {wp_id} skipped: abstract <50 chars")
            return None
        if len(abstract) < MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {wp_id} skipped: abstract <{MIN_ABSTRACT_CHARS} chars")
            return None

        pdf_url = publication_url if _is_pdf_url(publication_url) else None
        original_filename = _filename_from_url(pdf_url)
        if pdf_url and not original_filename:
            original_filename = self._content_disposition_filename(pdf_url)

        keyword_values = [
            fields.get("tssg-domain"),
            fields.get("tssg-project"),
            fields.get("type"),
        ]
        keywords = ", ".join(v for v in keyword_values if v) or None
        department = fields.get("tssg-domain") or None
        category = fields.get("type") or None
        journal = fields.get("journal") or None
        publication_native_id = fields.get("id") or None

        raw_item = {
            k: v
            for k, v in item.items()
            if k not in {"content", "excerpt"}
        }
        metadata = {
            "posted_date": item.get("date"),
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": fields.get("journal"),
            "series": fields.get("series"),
            "volume": fields.get("volume"),
            "issue": fields.get("number"),
            "pages": fields.get("pages"),
            "month": fields.get("month"),
            "year": fields.get("year") or fields.get("tssg-ayear"),
            "wp_id": wp_id,
            "node_id": wp_id,
            "wordpress_id": wp_id,
            "publication_native_id": publication_native_id,
            "slug": item.get("slug"),
            "guid": (item.get("guid") or {}).get("rendered"),
            "modified": item.get("modified"),
            "modified_gmt": item.get("modified_gmt"),
            "date_gmt": item.get("date_gmt"),
            "status": item.get("status"),
            "type": item.get("type"),
            "publication_url": publication_url,
            "field_map": fields,
            "post_type_data": post_type_data,
            "raw_item": raw_item,
        }

        return {
            "id": f"{self.site_id}-{wp_id}",
            "site_id": self.site_id,
            "external_id": str(wp_id),
            "post_number": str(wp_id),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "Walton Institute",
            "department": department,
            "journal": journal,
            "url": detail_url,
            "meta_url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def crawl(self, limit=None):
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise ValueError("limit must be an integer or None")
            if limit <= 0:
                return 0

        start = time.monotonic()
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else "inf"

        try:
            while page <= SAFETY_CAP_PAGES:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start >= WALL_CLOCK_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                    break

                data, headers, status = self._curl_json(self._list_url(page), f"list page {page}")
                if isinstance(data, dict) and data.get("code") == "rest_post_invalid_page_number":
                    print(f"[{self.site_id}] page {page}: API reported end of pagination")
                    break
                if data is None:
                    print(f"[{self.site_id}] page {page}: fetch failed; stopping pagination")
                    break
                if not isinstance(data, list):
                    print(f"[{self.site_id}] page {page}: unexpected response type {type(data).__name__}")
                    break
                if not data:
                    print(f"[{self.site_id}] page {page}: empty list; done")
                    break

                new_records = 0
                for list_item in data:
                    if limit is not None and saved >= limit:
                        break
                    if time.monotonic() - start >= WALL_CLOCK_BUDGET_SECONDS - 30:
                        print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting item loop")
                        break

                    item_id = list_item.get("id") if isinstance(list_item, dict) else None
                    list_url = _absolute_url(list_item.get("link")) if isinstance(list_item, dict) else None
                    dedupe_url = list_url or (f"{PUBLICATION_API}/{item_id}" if item_id is not None else None)
                    if not dedupe_url:
                        continue
                    if dedupe_url in seen_urls:
                        continue
                    seen_urls.add(dedupe_url)
                    new_records += 1

                    try:
                        if self.detail_delay:
                            time.sleep(self.detail_delay)
                        detail_data, _, detail_status = self._curl_json(
                            self._detail_url(item_id),
                            f"item {item_id}",
                        )
                        if not isinstance(detail_data, dict):
                            print(f"[{self.site_id}] item {item_id} failed: non-object detail response")
                            continue
                        if detail_status >= 400:
                            print(f"[{self.site_id}] item {item_id} failed: http {detail_status}")
                            continue

                        paper = self._build_paper(detail_data)
                        if paper is None:
                            continue
                        self._save_paper(paper)
                        saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_id or dedupe_url} failed: {exc}")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                if new_records == 0:
                    print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                    break

                link_header = headers.get("link", "")
                if link_header and 'rel="next"' not in link_header:
                    print(f"[{self.site_id}] page {page}: no next page link; done")
                    break

                page += 1

            if page > SAFETY_CAP_PAGES:
                print(f"[{self.site_id}] reached safety page cap {SAFETY_CAP_PAGES}; stopping")
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted at page {page}, saved {saved}")
            raise

        print(f"[{self.site_id}] done: saved {saved}/{limit_or_inf}")
        return saved
