# -*- coding: utf-8 -*-
"""Crawler for enc.hal.science (École Nationale des Chartes) via HAL public API."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EncHalScienceSearchCrawler(BaseCrawler):
    site_id = "enc-hal-science-search"
    site_name = "Custom: enc-hal-science-search"
    base_url = "https://enc.hal.science"

    _START_URL = (
        "https://enc.hal.science/search/index/?q=%2A&rows=30"
        "&submitType_s=file&docType_s=ART+OR+ETABTHESE+OR+COMM+OR+MEM"
    )
    _SEARCH_API = "https://api.archives-ouvertes.fr/search/enc/"
    _PAGE_SIZE = 30
    _MIN_ABSTRACT_CHARS = 100
    _RETRY_WAITS = (1, 3, 9)
    _MAX_PAGES = 200
    _WALL_CLOCK_BUDGET = 25 * 60  # seconds

    _FIELDS = ",".join((
        "docid",
        "halId_s",
        "version_i",
        "uri_s",
        "title_s",
        "en_title_s",
        "fr_title_s",
        "abstract_s",
        "en_abstract_s",
        "fr_abstract_s",
        "keyword_s",
        "en_keyword_s",
        "fr_keyword_s",
        "authFullName_s",
        "producedDate_s",
        "publicationDate_s",
        "submittedDate_s",
        "releasedDate_s",
        "fileMain_s",
        "doiId_s",
        "docType_s",
        "domain_s",
        "primaryDomain_s",
        "labStructName_s",
        "instStructName_s",
        "journalTitle_s",
        "bookTitle_s",
        "conferenceTitle_s",
        "language_s",
        "licence_s",
        "citationRef_s",
        "label_s",
    ))

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, accept=None, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept or 'application/json,text/html;q=0.9,*/*;q=0.8'}",
            "-H", "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
            url,
        ]
        last_error = "unknown error"
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout:
                    return stdout.decode("utf-8", errors="replace")
                last_error = f"exit={result.returncode} stderr={stderr}"

            wait = self._RETRY_WAITS[attempt - 1]
            if attempt < 3:
                print(
                    f"[{self.site_id}] curl failed {attempt}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_json(self, url, *, label):
        raw = self._curl(url, accept="application/json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] invalid JSON from {label}: {exc}")
            return None

    # ------------------------------------------------------------------
    # HTML / text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception:
                continue
        return None

    @classmethod
    def _strip_html(cls, value):
        text = str(value or "")
        if "<" not in text or ">" not in text:
            return text
        soup = cls._parse_html(text)
        if soup is None:
            return re.sub(r"<[^>]+>", " ", text)
        return soup.get_text(" ", strip=True)

    @classmethod
    def _clean_text(cls, value):
        if value is None:
            return ""
        text = cls._strip_html(value)
        text = unescape(str(text))
        text = text.replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
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
    def _values(cls, doc, key):
        value = doc.get(key)
        if value in (None, ""):
            return []
        if isinstance(value, list):
            raw_items = value
        else:
            raw_items = [value]
        return cls._dedupe(cls._one_line(item) for item in raw_items)

    @classmethod
    def _first_value(cls, doc, *keys):
        for key in keys:
            values = cls._values(doc, key)
            if values:
                return values[0]
        return ""

    @classmethod
    def _parse_date(cls, raw):
        text = cls._one_line(raw)
        if not text:
            return ""
        m = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", text)
        if m:
            return m.group(0)
        m = re.search(r"\b(19|20)\d{2}-\d{2}\b", text)
        if m:
            return m.group(0)
        m = re.search(r"\b(19|20)\d{2}\b", text)
        return m.group(0) if m else ""

    # ------------------------------------------------------------------
    # HAL API list fetch (all fields in one call — no per-item detail needed)
    # ------------------------------------------------------------------

    def _api_url(self, params):
        return self._SEARCH_API + "?" + urlencode(params, doseq=True)

    def _fetch_list(self, start):
        params = [
            ("q", "*"),
            ("rows", str(self._PAGE_SIZE)),
            ("start", str(start)),
            ("fq", "submitType_s:file"),
            ("fq", "docType_s:(ART OR ETABTHESE OR COMM OR MEM)"),
            ("fl", self._FIELDS),
            ("wt", "json"),
        ]
        return self._fetch_json(self._api_url(params), label=f"list start={start}")

    # ------------------------------------------------------------------
    # Record normalization
    # ------------------------------------------------------------------

    def _title(self, doc):
        values = (
            self._values(doc, "title_s")
            or self._values(doc, "en_title_s")
            or self._values(doc, "fr_title_s")
        )
        if values:
            return " / ".join(values)
        return self._first_value(doc, "label_s")

    def _abstract(self, doc):
        parts = []
        for key in ("abstract_s", "en_abstract_s", "fr_abstract_s"):
            parts.extend(self._values(doc, key))
        return "\n\n".join(self._dedupe(parts))

    def _keywords(self, doc):
        terms = []
        for key in ("keyword_s", "en_keyword_s", "fr_keyword_s"):
            terms.extend(self._values(doc, key))
        return self._dedupe(terms)

    def _published_date(self, doc):
        for key in ("producedDate_s", "publicationDate_s", "releasedDate_s", "submittedDate_s"):
            parsed = self._parse_date(self._first_value(doc, key))
            if parsed:
                return parsed
        return ""

    def _paper_from_doc(self, doc):
        docid = self._first_value(doc, "docid")
        hal_id = self._first_value(doc, "halId_s")
        version = self._first_value(doc, "version_i")
        external_id = docid or (f"{hal_id}v{version}" if hal_id and version else hal_id)
        if not external_id:
            print(f"[{self.site_id}] skipping item without docid or HAL id")
            return None

        title = self._title(doc)
        if not title:
            print(f"[{self.site_id}] skipping {external_id}: missing title")
            return None

        abstract = self._abstract(doc)
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        authors = self._values(doc, "authFullName_s")
        keywords = self._keywords(doc)
        labs = self._values(doc, "labStructName_s")
        institutions = self._values(doc, "instStructName_s")
        departments = self._dedupe(labs + institutions)
        url = self._first_value(doc, "uri_s") or (
            f"{self.base_url}/{hal_id}" if hal_id else self.base_url
        )
        pdf_url = self._first_value(doc, "fileMain_s")
        doi = self._first_value(doc, "doiId_s")
        doc_type = self._first_value(doc, "docType_s")

        metadata = {
            "source_start_url": self._START_URL,
            "list_api": self._SEARCH_API,
            "hal_id": hal_id,
            "version": version,
            "docid": docid,
            "doc_type": doc_type,
            "primary_domain": self._first_value(doc, "primaryDomain_s"),
            "domains": self._values(doc, "domain_s"),
            "journal": self._first_value(doc, "journalTitle_s"),
            "book_title": self._first_value(doc, "bookTitle_s"),
            "conference_title": self._first_value(doc, "conferenceTitle_s"),
            "language": self._values(doc, "language_s"),
            "licence": self._first_value(doc, "licence_s"),
            "citation": self._first_value(doc, "citationRef_s"),
            "submitted_date": self._first_value(doc, "submittedDate_s"),
            "released_date": self._first_value(doc, "releasedDate_s"),
            "produced_date": self._first_value(doc, "producedDate_s"),
            "publication_date": self._first_value(doc, "publicationDate_s"),
            "laboratories": labs,
            "institutions": institutions,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": doc_type,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": self._published_date(doc),
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": "; ".join(departments),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        start = 0
        page = 0
        seen_urls = set()
        total = None
        t_start = time.time()
        limit_or_inf = limit if limit is not None else "∞"

        while True:
            if limit is not None and saved >= limit:
                break

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages; stopping")
                break

            elapsed = time.time() - t_start
            if elapsed > self._WALL_CLOCK_BUDGET:
                print(
                    f"[{self.site_id}] approaching 25-minute wall-clock budget "
                    f"({elapsed:.0f}s elapsed); stopping cleanly"
                )
                break

            try:
                data = self._fetch_list(start)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] list fetch error at start={start}: {exc}")
                break

            response = (data or {}).get("response") or {}
            items = response.get("docs") or []

            if total is None:
                total = response.get("numFound")
                if total is not None:
                    print(f"[{self.site_id}] API records: {total}")

            if not items:
                print(f"[{self.site_id}] no more list records at start={start}")
                break

            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            for offset, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                item_no = start + offset
                try:
                    docid = self._first_value(item, "docid")
                    uri = self._first_value(item, "uri_s")
                    dedup_key = docid or uri
                    if dedup_key and dedup_key in seen_urls:
                        continue
                    if dedup_key:
                        seen_urls.add(dedup_key)

                    paper = self._paper_from_doc(item)
                    if not paper:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_no} failed: {exc}")
                    continue

            start += len(items)
            if len(items) < self._PAGE_SIZE:
                print(f"[{self.site_id}] last page (got {len(items)} < {self._PAGE_SIZE}); done")
                break

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
