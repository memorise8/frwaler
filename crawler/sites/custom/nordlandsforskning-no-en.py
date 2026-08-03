# -*- coding: utf-8 -*-
"""Crawler for Nordlandsforskning publications.

Starting URL: https://www.nordlandsforskning.no/en/publications

The English URL redirects through Weglot to the Squarespace collection at
``/publikasjoner``. Squarespace exposes both collection pages and item pages as
JSON when ``format=json`` is supplied.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse, unquote

from crawler.base_crawler import BaseCrawler


_SITE_ID = "nordlandsforskning-no-en"
_BASE_URL = "https://www.nordlandsforskning.no"
_START_URL = f"{_BASE_URL}/en/publications"
_JSON_LIST_URL = f"{_BASE_URL}/publikasjoner?format=json"
_SAFETY_CAP = 200
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_CHARS = 100
_SUMMARY_LABELS = {"summary", "sammendrag", "abstract"}


class NordlandsforskningNoEnCrawler(BaseCrawler):
    site_id = "nordlandsforskning-no-en"
    site_name = "Custom: nordlandsforskning-no-en"
    base_url = "https://www.nordlandsforskning.no"

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        page = 1
        next_url = _JSON_LIST_URL
        seen_urls: set[str] = set()
        seen_page_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while next_url:
            if page > _SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {_SAFETY_CAP} pages reached")
                break
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time > _MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if next_url in seen_page_urls:
                print(f"[{self.site_id}] page {page}: pagination loop detected")
                break
            seen_page_urls.add(next_url)

            payload = self._fetch_json(next_url)
            if not isinstance(payload, dict):
                print(f"[{self.site_id}] page {page}: failed to fetch JSON")
                break

            items = payload.get("items")
            if not isinstance(items, list) or not items:
                print(f"[{self.site_id}] page {page}: 0 records")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time > _MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly")
                    next_url = None
                    break

                if not isinstance(item, dict):
                    continue

                detail_url = self._absolute_url(item.get("fullUrl") or "")
                if not detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    detail_item = self._fetch_detail_item(detail_url)
                    if not detail_item:
                        raise ValueError("detail JSON unavailable")
                    paper = self._paper_from_item(detail_item, item, detail_url)
                    abstract = paper.get("abstract") or ""
                    if len(abstract.strip()) < _MIN_ABSTRACT_CHARS:
                        print(f"[{self.site_id}] short abstract skipped: {detail_url}")
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new records")
                break

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            next_page_url = pagination.get("nextPageUrl")
            next_url = self._json_url(next_page_url) if next_page_url else None
            page += 1

        print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")
        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, retries: int = 3, timeout: int = 45) -> str | None:
        delays = (1, 3, 9)
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-skL",
                        "--max-time",
                        str(timeout),
                        "-H",
                        f"User-Agent: {self.USER_AGENT}",
                        "-H",
                        "Accept: application/json,text/html,application/xhtml+xml,*/*;q=0.8",
                        url,
                    ],
                    capture_output=True,
                    timeout=timeout + 10,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl attempt {attempt + 1}/{retries} failed "
                    f"for {url}: {stderr or 'empty response'}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt + 1}/{retries} failed for {url}: {exc}")

            if attempt < retries - 1:
                time.sleep(delays[attempt])
        return None

    def _fetch_json(self, url: str) -> dict[str, Any] | None:
        raw = self._curl_get(url)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] JSON parse failed for {url}: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def _fetch_detail_item(self, detail_url: str) -> dict[str, Any] | None:
        data = self._fetch_json(self._json_url(detail_url))
        if not isinstance(data, dict):
            return None
        item = data.get("item")
        return item if isinstance(item, dict) else None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _paper_from_item(self, detail_item: dict[str, Any], list_item: dict[str, Any], detail_url: str) -> dict[str, Any]:
        item = dict(list_item)
        item.update({k: v for k, v in detail_item.items() if v is not None})

        title = self._clean_text(item.get("title") or "")
        if not title:
            raise ValueError("missing title")

        body_html = item.get("body") or ""
        soup = self._make_soup(body_html)
        if soup is None:
            raise ValueError("failed to parse body HTML")

        labels = self._extract_labels(soup)
        links = self._extract_links(soup)
        abstract = self._extract_abstract(soup, item.get("excerpt"))

        authors = self._split_people(labels.get("av") or labels.get("by"))
        if not authors:
            authors = self._authors_from_tags(item.get("tags") or [])
        publisher = labels.get("utgiver") or labels.get("publisher")
        raw_date = labels.get("dato") or labels.get("date")
        published_date = self._parse_date(raw_date) or self._millis_date(item.get("publishOn"))
        listed_date = self._millis_date(item.get("publishOn"))

        pdf_url = links.get("pdf_url")
        original_filename = self._filename_from_url(pdf_url)
        doi = links.get("doi") or self._extract_doi(body_html)
        download_label = self._download_label(labels)
        category = self._category_from_item(item, download_label)
        journal = self._journal_from_fields(publisher, category, download_label)
        series, volume, issue = self._series_fields(download_label)

        external_id = str(item.get("id") or item.get("urlId") or detail_url)
        post_number = external_id or item.get("urlId")

        metadata = {
            "posted_date": item.get("publishOn"),
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": publisher if journal else None,
            "series": series,
            "volume": volume,
            "issue": issue,
            "node_id": item.get("id"),
            "collection_id": item.get("collectionId"),
            "urlId": item.get("urlId"),
            "post_number": post_number,
            "addedOn": item.get("addedOn"),
            "updatedOn": item.get("updatedOn"),
            "recordType": item.get("recordType"),
            "recordTypeLabel": item.get("recordTypeLabel"),
            "author_raw": item.get("author"),
            "tags_raw": item.get("tags"),
            "categories_raw": item.get("categories"),
            "labels": labels,
            "handle": links.get("handle"),
            "download_label": download_label,
            "start_url": _START_URL,
            "detail_json_url": self._json_url(detail_url),
            "squarespace_raw": item,
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": publisher,
            "department": None,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(str(t).strip() for t in item.get("tags", []) if str(t).strip()),
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    def _make_soup(self, raw_html: str):
        from bs4 import BeautifulSoup

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw_html or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
        return None

    def _extract_labels(self, soup) -> dict[str, str]:
        labels: dict[str, str] = {}
        for ptag in soup.find_all("p"):
            strongs = ptag.find_all("strong")
            if not strongs:
                continue
            label_text = self._clean_text(" ".join(s.get_text(" ", strip=True) for s in strongs))
            norm = self._norm_label(label_text)
            if not norm:
                continue
            paragraph = self._clean_text(ptag.get_text(" ", strip=True))
            value = self._value_after_label(paragraph, label_text)
            labels[norm] = value
        return labels

    def _extract_links(self, soup) -> dict[str, str | None]:
        result: dict[str, str | None] = {"pdf_url": None, "handle": None, "doi": None}
        for a_tag in soup.find_all("a", href=True):
            href = html.unescape(str(a_tag.get("href") or "").strip())
            if not href:
                continue
            absolute = self._absolute_url(href)
            lower = absolute.lower()
            if result["pdf_url"] is None and self._looks_like_pdf(absolute):
                result["pdf_url"] = absolute
            if result["handle"] is None and "hdl.handle.net/" in lower:
                result["handle"] = absolute
            if result["doi"] is None and "doi.org/" in lower:
                result["doi"] = absolute.split("doi.org/", 1)[1].strip().strip("/")
        return result

    def _extract_abstract(self, soup, excerpt: Any) -> str:
        paragraphs = [self._clean_text(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if p]
        collected: list[str] = []
        collecting = False

        for text in paragraphs:
            norm = self._norm_label(text)
            if norm in _SUMMARY_LABELS:
                collecting = True
                continue
            if not collecting:
                continue
            if self._norm_label(text.split(":", 1)[0]) in {"av", "by", "utgiver", "publisher", "dato", "date"}:
                continue
            collected.append(text)

        if collected:
            return self._clean_text("\n\n".join(collected))

        excerpt_text = self._clean_text(excerpt or "")
        if len(excerpt_text) >= _MIN_ABSTRACT_CHARS:
            return excerpt_text

        candidates = []
        for text in paragraphs:
            if len(text) < _MIN_ABSTRACT_CHARS:
                continue
            norm_prefix = self._norm_label(text.split(":", 1)[0])
            if norm_prefix in {"av", "by", "utgiver", "publisher", "dato", "date", "language", "sprak"}:
                continue
            candidates.append(text)
        return self._clean_text("\n\n".join(candidates[:3]))

    # ------------------------------------------------------------------
    # Field normalization
    # ------------------------------------------------------------------

    def _json_url(self, url: str | None) -> str | None:
        if not url:
            return None
        absolute = self._absolute_url(url)
        parsed = urlparse(absolute)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["format"] = "json"
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _absolute_url(self, url: str) -> str:
        return urljoin(_BASE_URL, url)

    def _clean_text(self, value: Any) -> str:
        text = html.unescape(str(value or "")).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _norm_label(self, value: Any) -> str:
        text = self._clean_text(value).rstrip(":")
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    def _value_after_label(self, paragraph: str, label_text: str) -> str:
        if not paragraph:
            return ""
        label = self._clean_text(label_text).rstrip(":")
        if paragraph.lower().startswith(label.lower()):
            value = paragraph[len(label):]
            return self._clean_text(value.lstrip(": "))
        return ""

    def _split_people(self, raw: str | None) -> list[str]:
        if not raw:
            return []
        text = self._clean_text(raw)
        text = re.sub(r"\s+(?:og|and)\s+", ";", text, flags=re.IGNORECASE)
        text = text.replace(" & ", ";")
        pieces = re.split(r";|,(?=\s*\S+\s+\S+)", text)
        return [self._clean_text(p) for p in pieces if self._clean_text(p)]

    def _authors_from_tags(self, tags: list[Any]) -> list[str]:
        authors = []
        for tag in tags:
            text = self._clean_text(tag)
            if not text or re.fullmatch(r"\d{4}", text):
                continue
            authors.append(text)
        return authors

    def _parse_date(self, raw: str | None) -> str | None:
        text = self._clean_text(raw)
        if not text:
            return None
        for pattern, order in (
            (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", "dmy"),
            (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", "dmy"),
            (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "ymd"),
        ):
            m = re.match(pattern, text)
            if not m:
                continue
            if order == "dmy":
                day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{year:04d}-{month:02d}-{day:02d}"
        if re.fullmatch(r"\d{4}", text):
            return text
        return None

    def _millis_date(self, value: Any) -> str | None:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
        except Exception:
            return None

    def _looks_like_pdf(self, url: str) -> bool:
        path = unquote(urlparse(url).path).lower()
        return path.endswith(".pdf")

    def _filename_from_url(self, url: str | None) -> str | None:
        if not url:
            return None
        name = os.path.basename(unquote(urlparse(url).path))
        return name or None

    def _extract_doi(self, body_html: str) -> str | None:
        m = re.search(r"\b(10\.\d{4,9}/[^\s<>\"]+)", body_html or "", flags=re.IGNORECASE)
        if not m:
            return None
        return m.group(1).rstrip(").,;")

    def _download_label(self, labels: dict[str, str]) -> str | None:
        for key in labels:
            if key.startswith("download") or key.startswith("lastned"):
                return key
        return None

    def _category_from_item(self, item: dict[str, Any], download_label: str | None) -> str | None:
        categories = item.get("categories")
        if isinstance(categories, list) and categories:
            return "; ".join(self._clean_text(c) for c in categories if self._clean_text(c))
        label = download_label or ""
        if "article" in label:
            return "Article"
        if "arbeidsnotat" in label or "workingpaper" in label:
            return "Working paper"
        if "rapport" in label or "report" in label:
            return "Report"
        return None

    def _journal_from_fields(self, publisher: str | None, category: str | None, download_label: str | None) -> str | None:
        if not publisher:
            return None
        label = download_label or ""
        if category == "Article" or "article" in label:
            return publisher
        return None

    def _series_fields(self, download_label: str | None) -> tuple[str | None, str | None, str | None]:
        if not download_label:
            return None, None, None
        m = re.search(r"(nfrapport|nfarbeidsnotat|report|workingpaper)(\d{1,4})?(\d{4})?", download_label)
        if not m:
            return None, None, None
        series = m.group(1)
        issue = None
        if m.group(2) and m.group(3):
            issue = f"{m.group(2)}/{m.group(3)}"
        return series, None, issue
