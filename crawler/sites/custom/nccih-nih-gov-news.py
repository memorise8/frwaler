# -*- coding: utf-8 -*-
"""Crawler for NCCIH NIH press releases.

Target: https://www.nccih.nih.gov/news/press-releases

The site is a Gatsby static build. Listing pages are exposed as
/page-data/news/press-releases[/page-N]/page-data.json, while internal detail
pages have their own page-data JSON keyed by the press-release slug.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

# Absolute import: spec_from_file_location gives no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler  # noqa: E402


_SITE_ID = "nccih-nih-gov-news"
_BASE_URL = "https://www.nccih.nih.gov"
_START_URL = f"{_BASE_URL}/news/press-releases"
_LIST_DATA_URL = f"{_BASE_URL}/page-data/news/press-releases/page-data.json"
_LIST_DATA_URL_PAGE = f"{_BASE_URL}/page-data/news/press-releases/page-{{page}}/page-data.json"
_DETAIL_DATA_URL = f"{_BASE_URL}/page-data{{path}}/page-data.json"
_MAX_PAGES = 200
_MAX_SECONDS = 25 * 60
_TIME_STOP_MARGIN_SECONDS = 30
_RETRY_WAITS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 50
_DEFAULT_PUBLISHER = (
    "National Center for Complementary and Integrative Health; "
    "National Institutes of Health"
)
_DEFAULT_DEPARTMENT = "National Center for Complementary and Integrative Health"


def _clean_text(value):
    if value is None:
        return ""
    text = unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = text.replace("\r", "\n").replace("\f", "\n")
    text = re.sub(r"[ \t\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _one_line(value):
    return re.sub(r"\s+", " ", _clean_text(value)).strip()


def _make_soup(raw, context="html"):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw or ""

    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable for {context}: {exc}")
        return None

    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception as exc:
            last_exc = exc
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")

    print(f"[{_SITE_ID}] all BeautifulSoup parsers failed for {context}: {last_exc}")
    return None


def _html_to_text(raw_html):
    if not raw_html:
        return ""

    soup = _make_soup(raw_html, context="fragment")
    if soup is None:
        text = re.sub(r"<[^>]+>", " ", raw_html)
        return _one_line(text)

    for bad in soup.select("script, style, noscript, svg"):
        bad.decompose()

    parts = []
    for node in soup.find_all(["p", "li", "h2", "h3", "h4"], recursive=True):
        text = _one_line(node.get_text(" ", strip=True))
        if text:
            parts.append(text)

    if not parts:
        parts.append(_one_line(soup.get_text(" ", strip=True)))

    seen = set()
    unique_parts = []
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_parts.append(part)

    return _clean_text("\n\n".join(unique_parts))


def _parse_iso_date(raw):
    raw = _one_line(raw)
    if not raw:
        return ""

    match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
    if match:
        return match.group(0)

    normalized = re.sub(r"Z$", "+00:00", raw)
    try:
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        pass

    raw = re.sub(r"^\w+,\s+", "", raw)
    raw = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _split_keywords(raw):
    if not raw:
        return []
    values = []
    for item in re.split(r"\s*[,;|]\s*", str(raw)):
        value = _one_line(item)
        if value and value.lower() not in {v.lower() for v in values}:
            values.append(value)
    return values


def _extract_doi(raw):
    if not raw:
        return ""
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False, default=str)
    patterns = (
        r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)",
        r"\bDOI:\s*(10\.[^\s\"'<>]+)",
        r"\b(10\.\d{4,9}/[^\s\"'<>]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I)
        if match:
            return unescape(match.group(1)).rstrip(".,);")
    return ""


def _extract_journal(text):
    if not text:
        return "", ""

    patterns = (
        r"\bpublished\b[^.\n]{0,140}?\bin\s+(?:the\s+)?journal\s+([^.\n]+)",
        r"\bpublished\b[^.\n]{0,140}?\bin\s+([^.\n]+)",
        r"\bpublished\s+(?:online\s+)?(?:on\s+[^.,;]+?\s+)?in\s+the\s+journal\s+([^.\n]+)",
        r"\bpublished\s+(?:online\s+)?(?:on\s+[^.,;]+?\s+)?in\s+([^.\n]+)",
        r"\bappears\s+in\s+([^.\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        raw = _one_line(match.group(1))
        raw = re.sub(r"^(the\s+)?journal\s+", "", raw, flags=re.I).strip()
        raw = re.split(r",\s+", raw, maxsplit=1)[0]
        raw = raw.strip(" .;:()")
        if raw:
            return raw, raw
    return "", ""


def _filename_from_url(url):
    if not url:
        return None
    path = urlparse(url).path
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if "." in tail and len(tail) <= 200:
        return tail
    return None


class NccihNihGovNewsCrawler(BaseCrawler):
    site_id = "nccih-nih-gov-news"
    site_name = "Custom: nccih-nih-gov-news"
    base_url = "https://www.nccih.nih.gov"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, context="request", accept=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--fail",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
        ]
        if accept:
            cmd.extend(["-H", f"Accept: {accept}"])
        cmd.append(url)

        last_error = "unknown error"
        for attempt in range(1, 4):
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
                raw = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and raw.strip():
                    return raw
                if result.returncode == 0:
                    last_error = "empty response"
                else:
                    last_error = f"exit={result.returncode} stderr={stderr[:500]}"

            if attempt < 3:
                wait = _RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"{attempt}/3 for {url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _fetch_json(self, url, *, context):
        raw = self._curl_get(
            url,
            context=context,
            accept="application/json,text/plain,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} JSON parse failed for {url}: {exc}")
            return None

    def _list_url(self, page):
        if page == 1:
            return _LIST_DATA_URL
        return _LIST_DATA_URL_PAGE.format(page=page)

    def _detail_data_url(self, path):
        return _DETAIL_DATA_URL.format(path=path)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_edges(data):
        try:
            edges = data["result"]["data"]["allNewsJson"]["edges"]
        except (KeyError, TypeError):
            return []
        if not isinstance(edges, list):
            return []
        return edges

    @staticmethod
    def _page_context(data):
        try:
            context = data["result"].get("pageContext", {})
        except (AttributeError, KeyError, TypeError):
            return {}
        return context if isinstance(context, dict) else {}

    def _parse_list_items(self, data):
        items = []
        for edge in self._extract_edges(data):
            try:
                news = edge.get("node", {}).get("news", {})
                if not isinstance(news, dict):
                    continue
                title = _one_line(news.get("title"))
                if not title:
                    continue

                internal_path = news.get("url") or ""
                external_url = news.get("externalUrl") or ""
                external_news = bool(news.get("externalNews"))
                detail_url = external_url if external_news and external_url else urljoin(self.base_url, internal_path)

                items.append(
                    {
                        "native": news,
                        "title": title,
                        "url": detail_url,
                        "internal_path": internal_path,
                        "external_news": external_news,
                        "external_url": external_url,
                        "listed_date_raw": news.get("immediateReleaseDate") or "",
                        "listed_date": _parse_iso_date(news.get("immediateReleaseDate") or ""),
                        "updated_date": _parse_iso_date(news.get("updatedDate") or ""),
                        "summary_text": _html_to_text(news.get("summary") or ""),
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] list item parse failed: {exc}")
                continue
        return items

    @staticmethod
    def _detail_news(data):
        try:
            news = data["result"]["data"]["newsJson"]["news"]
        except (KeyError, TypeError):
            return {}
        return news if isinstance(news, dict) else {}

    def _fetch_detail_record(self, item):
        if item.get("external_news"):
            return {}

        path = item.get("internal_path") or ""
        if not path.startswith("/"):
            return {}

        url = self._detail_data_url(path)
        data = self._fetch_json(url, context=f"detail {item.get('url')}")
        if not data:
            return {}
        return self._detail_news(data)

    def _extract_pdf_url(self, detail_news):
        candidates = []
        for key in ("pdfUrl", "pdfURL", "downloadUrl", "fileUrl"):
            value = detail_news.get(key)
            if value:
                candidates.append(value)

        for resource in detail_news.get("additionalResources") or []:
            if not isinstance(resource, dict):
                continue
            for key in ("url", "href", "fileUrl"):
                value = resource.get(key)
                if value:
                    candidates.append(value)

        for block in detail_news.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            for key in ("description", "hyperlink"):
                value = block.get(key)
                if not value:
                    continue
                for match in re.findall(r"https?://[^\"'<>]+?\.pdf(?:[?#][^\"'<>]*)?", str(value), flags=re.I):
                    candidates.append(match)
                for match in re.findall(r"href=[\"']([^\"']+?\.pdf(?:[?#][^\"']*)?)[\"']", str(value), flags=re.I):
                    candidates.append(match)

        for candidate in candidates:
            absolute = urljoin(self.base_url, str(candidate).strip())
            if re.search(r"\.pdf(?:[?#]|$)", absolute, flags=re.I):
                return absolute
        return None

    def _detail_body(self, detail_news):
        parts = []
        for block in detail_news.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            heading = _one_line(block.get("heading"))
            description = _html_to_text(block.get("description") or "")
            if heading:
                parts.append(heading)
            if description:
                parts.append(description)
        return _clean_text("\n\n".join(part for part in parts if part))

    def _build_paper(self, item, detail_news, page):
        native = item.get("native") or {}
        merged = dict(native)
        if detail_news:
            merged.update({k: v for k, v in detail_news.items() if v not in (None, "")})

        native_id = str(native.get("id") or detail_news.get("id") or "").strip()
        uuid = _one_line(detail_news.get("uuid") or native.get("uuid") or "")
        post_number = native_id or uuid or None
        external_id = post_number or item.get("internal_path") or item.get("url")

        title = _one_line(detail_news.get("title") or item.get("title"))
        listed_date_raw = item.get("listed_date_raw") or merged.get("immediateReleaseDate") or ""
        listed_date = _parse_iso_date(listed_date_raw) or item.get("listed_date") or ""
        published_date = listed_date

        body_text = self._detail_body(detail_news) if detail_news else ""
        summary_text = item.get("summary_text") or _html_to_text(merged.get("summary") or "")
        abstract = body_text if len(body_text) >= len(summary_text) else summary_text
        abstract = _clean_text(abstract)

        metadata_obj = merged.get("metadata") if isinstance(merged.get("metadata"), dict) else {}
        keywords = _split_keywords(metadata_obj.get("keyword") if metadata_obj else "")
        category = "Press Release"
        if category not in keywords:
            keywords.insert(0, category)

        raw_for_doi = json.dumps(merged, ensure_ascii=False, default=str)
        doi = _extract_doi(raw_for_doi) or _extract_doi(abstract)
        journal, journal_raw = _extract_journal(abstract)
        pdf_url = self._extract_pdf_url(detail_news) if detail_news else None
        original_filename = _filename_from_url(pdf_url)

        publisher = _DEFAULT_PUBLISHER
        if item.get("external_news") and native.get("externalOrganization"):
            publisher = f"{native.get('externalOrganization')}; {publisher}"

        url = item.get("url") or urljoin(self.base_url, item.get("internal_path") or "")
        internal_url = urljoin(self.base_url, item.get("internal_path") or "") if item.get("internal_path") else ""

        metadata = {
            "source": "NCCIH Gatsby page-data JSON",
            "start_url": _START_URL,
            "list_endpoint": _LIST_DATA_URL if page == 1 else _LIST_DATA_URL_PAGE.format(page=page),
            "detail_endpoint": self._detail_data_url(item.get("internal_path")) if detail_news else None,
            "page": page,
            "posted_date": listed_date_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": native_id,
            "news_id": native_id,
            "uuid": uuid,
            "post_number": post_number,
            "externalNews": bool(item.get("external_news")),
            "externalUrl": item.get("external_url") or "",
            "externalOrganization": native.get("externalOrganization") or "",
            "internal_url": internal_url,
            "updatedDate": merged.get("updatedDate") or "",
            "updated_date": _parse_iso_date(merged.get("updatedDate") or ""),
            "updatedYear": merged.get("updatedYear"),
            "subtitle": _one_line(merged.get("subtitle") or ""),
            "seo": metadata_obj,
            "pressContact": detail_news.get("pressContact") if detail_news else None,
            "coSponsors": detail_news.get("coSponsors") if detail_news else None,
            "raw_list_record": native,
            "raw_detail_record": detail_news or None,
        }

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "",
            "publisher": publisher,
            "department": _DEFAULT_DEPARTMENT,
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords),
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    def _time_budget_exhausted(self, started_at):
        return time.monotonic() - started_at >= _MAX_SECONDS - _TIME_STOP_MARGIN_SECONDS

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        page = 1
        total_pages = None

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        while page <= _MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            if self._time_budget_exhausted(started_at):
                print(f"[{self.site_id}] approaching 25-minute budget at page {page}; exiting cleanly")
                break

            list_url = self._list_url(page)
            data = self._fetch_json(list_url, context=f"list page {page}")
            if not data:
                print(f"[{self.site_id}] page {page}: no list data; stopping")
                break

            context = self._page_context(data)
            if total_pages is None:
                try:
                    total_pages = int(context.get("newsnumPages") or 0) or None
                except (TypeError, ValueError):
                    total_pages = None

            items = self._parse_list_items(data)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            page_new = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_exhausted(started_at):
                    print(f"[{self.site_id}] approaching 25-minute budget during page {page}; exiting cleanly")
                    return saved

                url = item.get("url") or item.get("internal_path") or ""
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                page_new += 1

                try:
                    time.sleep(self.detail_delay)
                    detail_news = self._fetch_detail_record(item)
                    paper = self._build_paper(item, detail_news, page)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] short abstract ({len(abstract)} chars), "
                            f"skipping: {paper.get('title', '')[:70]}"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            if page_new == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if total_pages is not None and page >= total_pages:
                break

            page += 1

        if page > _MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {_MAX_PAGES} pages")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
