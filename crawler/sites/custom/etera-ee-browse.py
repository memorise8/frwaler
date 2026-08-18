# -*- coding: utf-8 -*-
"""Crawler for ETERA browse records.

ETERA is a MediaINFO Angular application.  The visible browse page is an app
shell; records are loaded through ``POST /api/browse`` and item-level catalog
metadata is loaded through ``GET /api/item/{id}/meta``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EteraEeBrowseCrawler(BaseCrawler):
    site_id = "etera-ee-browse"
    site_name = "Custom: etera-ee-browse"
    base_url = "https://www.etera.ee"
    DELIVERY_ORDER = "newest_first"

    _START_URL = "https://www.etera.ee/browse"
    _BROWSE_API = "https://www.etera.ee/api/browse"
    _ITEM_META_API = "https://www.etera.ee/api/item/{item_id}/meta"
    _OAI_DOWNLOAD = "https://www.etera.ee/api/oai2/download/{item_id}.pdf"

    _PAGE_SIZE = 25
    _CURL_TIMEOUT = 45
    _MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network and parsing helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, method="GET", data=None, accept=None, referer=None,
              timeout=None):
        timeout = timeout or self._CURL_TIMEOUT
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept or 'application/json,text/html,application/xhtml+xml,*/*;q=0.8'}",
            "-H",
            "Accept-Language: et-EE,et;q=0.9,en-US;q=0.7,en;q=0.6",
        ]
        body = None
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if method.upper() == "POST":
            body = (data or "").encode("utf-8", errors="replace")
            cmd.extend([
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
            ])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    input=body,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[etera-ee-browse] BeautifulSoup({parser}) failed: {exc}")
        return None

    @classmethod
    def _clean_text(cls, value):
        if value is None:
            return ""
        text = unescape(str(value))
        if "<" in text and ">" in text:
            soup = cls._parse_html(text)
            if soup is not None:
                text = soup.get_text(" ", strip=True)
        text = text.replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @staticmethod
    def _dedupe(items):
        seen = set()
        result = []
        for item in items:
            value = str(item or "").strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @classmethod
    def _flatten_values(cls, values):
        flattened = []
        if values is None:
            return flattened
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, list):
                parts = [cls._one_line(v) for v in value if cls._one_line(v)]
                if parts:
                    flattened.append(" > ".join(parts))
            elif isinstance(value, dict):
                text = cls._one_line(
                    value.get("name") or value.get("title") or value.get("value")
                    or json.dumps(value, ensure_ascii=False)
                )
                if text:
                    flattened.append(text)
            else:
                text = cls._one_line(value)
                if text:
                    flattened.append(text)
        return cls._dedupe(flattened)

    @staticmethod
    def _parse_json(raw, context):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[etera-ee-browse] JSON parse failed for {context}: {exc}")
            return None

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _browse_payload(self, start):
        return {
            "filters": [{"key": "-typ", "value": "page"}],
            "start": start,
            "limit": self._PAGE_SIZE,
            "fields": {},
            "sort": [{"key": "date_created", "asc": False}],
            "dateSearch": None,
            "dateFilter": None,
            "dateInputs": None,
            "dateCalendar": None,
            "favorite": False,
            "layout": "card",
            "screen": "browse",
        }

    def _fetch_browse_page(self, start):
        payload = json.dumps(self._browse_payload(start), ensure_ascii=False)
        raw = self._curl(
            self._BROWSE_API,
            method="POST",
            data=payload,
            accept="application/json",
            referer=self._START_URL,
        )
        data = self._parse_json(raw, f"browse start={start}")
        if not isinstance(data, dict):
            return None
        return data

    def _fetch_item_meta(self, item_id):
        raw = self._curl(
            self._ITEM_META_API.format(item_id=item_id),
            accept="application/json",
            referer=urljoin(self.base_url, f"/zoom/{item_id}/view"),
        )
        data = self._parse_json(raw, f"item {item_id} meta")
        if not isinstance(data, dict):
            return None
        if data.get("error"):
            message = data.get("error", {}).get("message") or data["error"]
            raise RuntimeError(f"metadata API error: {message}")
        return data

    @staticmethod
    def _field(meta, key):
        if not isinstance(meta, dict):
            return None
        if key in meta and isinstance(meta[key], dict):
            return meta[key]
        for value in meta.values():
            if isinstance(value, dict) and value.get("machineName") == key:
                return value
        return None

    @classmethod
    def _field_values(cls, meta, key):
        field = cls._field(meta, key)
        if not field:
            return []
        return cls._flatten_values(field.get("values"))

    @staticmethod
    def _date_from_raw(raw, mask=None):
        text = re.sub(r"\D", "", str(raw or ""))
        if len(text) < 4:
            return ""

        year = text[:4]
        month = text[4:6] if len(text) >= 6 else ""
        day = text[6:8] if len(text) >= 8 else ""
        mask_text = str(mask or "")

        if "MM" not in mask_text or not month or month == "00":
            return year
        if "DD" not in mask_text or not day or day == "00":
            return f"{year}-{month}"
        return f"{year}-{month}-{day}"

    @classmethod
    def _published_date(cls, item, meta):
        field = cls._field(meta, "cf22") or cls._field(meta, "date_to")
        values = (field or {}).get("values") or []
        raw = values[0] if values else item.get("date_to")
        mask = (field or {}).get("mask") or item.get("date_mask")
        return cls._date_from_raw(raw, mask)

    @classmethod
    def _extract_external_links(cls, meta):
        links = []
        for value in cls._field_values(meta, "cf23"):
            if value.startswith("http"):
                links.append(value)
            else:
                match = re.search(r"https?://\S+", value)
                if match:
                    links.append(match.group(0).rstrip(".,)"))
        return cls._dedupe(links)

    @classmethod
    def _extract_doi(cls, values):
        for value in values:
            match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", value, re.I)
            if match:
                return match.group(0).rstrip(".,);")
        return ""

    @classmethod
    def _item_url(cls, item):
        path = item.get("path") or ""
        if not path:
            item_id = item.get("obj_id") or item.get("id")
            path = f"/zoom/{item_id}/view"
        absolute = urljoin(cls.base_url, path)
        parsed = urlparse(absolute)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    @classmethod
    def _description_fields(cls, meta):
        descriptions = []
        needles = (
            "abstract",
            "description",
            "summary",
            "kirjeld",
            "tutvustus",
            "sisukokku",
            "kokkuvote",
            "kokkuvõte",
        )
        for field in meta.values():
            if not isinstance(field, dict):
                continue
            label = cls._one_line(field.get("label")).lower()
            machine = cls._one_line(field.get("machineName")).lower()
            if any(needle in label or needle in machine for needle in needles):
                descriptions.extend(cls._flatten_values(field.get("values")))
        return cls._dedupe(descriptions)

    @classmethod
    def _build_abstract(cls, *, title, item, meta, published_date, authors,
                        publisher, categories, keywords, classification,
                        languages, rights, external_links):
        description = " ".join(cls._description_fields(meta)).strip()
        image_count = item.get("images") or (cls._field_values(meta, "images") or [""])[0]

        parts = []
        if title:
            parts.append(f'Title: "{title}".')
        if classification:
            parts.append(f"Type: {', '.join(classification)}.")
        if published_date:
            parts.append(f"Publication date: {published_date}.")
        if authors:
            parts.append(f"Creator/author: {', '.join(authors)}.")
        if publisher:
            parts.append(f"Publisher or publication place: {', '.join(publisher)}.")
        if categories:
            parts.append(f"Categories: {'; '.join(categories)}.")
        if keywords:
            parts.append(f"Keywords: {', '.join(keywords)}.")
        if languages:
            parts.append(f"Language: {', '.join(languages)}.")
        if rights:
            parts.append(f"Access rights: {', '.join(rights)}.")
        if image_count:
            parts.append(f"Digitized page/image count: {image_count}.")
        if external_links:
            parts.append(f"External catalog link: {external_links[0]}.")

        catalog_summary = " ".join(parts)
        if description and len(description) >= 100:
            return description
        if description:
            return f"{description} {catalog_summary}".strip()
        return catalog_summary.strip()

    @classmethod
    def _paper_from_item(cls, item, meta):
        item_id = str(item.get("obj_id") or item.get("id") or "").strip()
        if not item_id:
            raise RuntimeError("missing item id")

        title_values = cls._field_values(meta, "title")
        title = title_values[0] if title_values else cls._one_line(item.get("title"))
        if not title:
            raise RuntimeError("missing title")

        published_date = cls._published_date(item, meta)
        authors = cls._field_values(meta, "creator")
        publisher = cls._field_values(meta, "publisher")
        categories = cls._field_values(meta, "category")
        keywords = cls._field_values(meta, "tags")
        classification = cls._field_values(meta, "classification")
        languages = cls._field_values(meta, "language")
        rights = cls._field_values(meta, "cf13")
        identifiers = cls._field_values(meta, "identifier")
        external_links = cls._extract_external_links(meta)
        doi = cls._extract_doi(identifiers + external_links)

        abstract = cls._build_abstract(
            title=title,
            item=item,
            meta=meta,
            published_date=published_date,
            authors=authors,
            publisher=publisher,
            categories=categories,
            keywords=keywords,
            classification=classification,
            languages=languages,
            rights=rights,
            external_links=external_links,
        )

        pdf_url = ""
        if any(value.lower() == "vaba" for value in rights):
            pdf_url = cls._OAI_DOWNLOAD.format(item_id=item_id)

        metadata = {
            "source": "MediaINFO /api/browse + /api/item/{id}/meta",
            "itemType": item.get("itemType"),
            "type": item.get("type"),
            "classification": classification,
            "category": categories,
            "language": languages,
            "rights": rights,
            "identifiers": identifiers,
            "externalLinks": external_links,
            "thumbnail": urljoin(cls.base_url, item.get("thumbnail") or ""),
            "imageCount": item.get("images"),
            "dateMask": item.get("date_mask"),
            "rawMeta": {
                key: value for key, value in meta.items()
                if key != "_list" and isinstance(value, dict)
            },
        }

        return {
            "id": None,
            "site_id": cls.site_id,
            "external_id": item_id,
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": "; ".join(categories or classification),
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": cls._item_url(item),
            "pdf_url": pdf_url,
            "doi": doi,
            "department": "; ".join(publisher),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        start = (self.delivery_cursor or {}).get("offset", 0)

        raw_start = self._curl(
            self._START_URL,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        if raw_start:
            self._parse_html(raw_start)
        else:
            print(f"[{self.site_id}] start page fetch failed; continuing with API")

        while True:
            if limit is not None and saved >= limit:
                break

            data = self._fetch_browse_page(start)
            if not data:
                print(f"[{self.site_id}] browse page at start={start} failed; stopping")
                break

            results = data.get("results") or {}
            items = results.get("docs") or []
            if not items:
                print(f"[{self.site_id}] no more items at start={start}")
                break

            total = results.get("count")
            if start == 0 and total is not None:
                print(f"[{self.site_id}] total browse records: {total}")

            for index, item in enumerate(items, start=start + 1):
                if limit is not None and saved >= limit:
                    break

                item_id = str(item.get("obj_id") or item.get("id") or index)
                try:
                    time.sleep(self.detail_delay)
                    meta = self._fetch_item_meta(item_id)
                    if not meta:
                        raise RuntimeError("empty metadata response")

                    paper = self._paper_from_item(item, meta)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[etera-ee-browse] item {item_id} failed: {exc}")
                    continue

            self._advance_cursor({"offset": start + self._PAGE_SIZE}, items_done=len(items))
            start += self._PAGE_SIZE

        print(f"[{self.site_id}] done. saved: {saved}")
        return saved
