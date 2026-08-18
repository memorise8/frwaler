# -*- coding: utf-8 -*-
"""Crawler for INSERM REPORT documents in HAL Science.

Uses the HAL public REST API (api.archives-ouvertes.fr/search/) filtered by
instStructAcronym_s:INSERM and docType_s:REPORT.  All needed fields are fetched
in a single list call — no per-item detail fetch — keeping the full crawl well
within the 25-minute wall-clock budget.

Starting URL reference:
  https://inserm.hal.science/search/index/?q=%2A&rows=30&sort=producedDate_tdate+desc&docType_s=REPORT
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class InsermHalScienceSearchCrawler(BaseCrawler):
    site_id = "inserm-hal-science-search"
    site_name = "Custom: inserm-hal-science-search"
    base_url = "https://inserm.hal.science"
    DELIVERY_ORDER = "oldest_first"

    _START_URL = (
        "https://inserm.hal.science/search/index/?q=%2A&rows=30"
        "&sort=producedDate_tdate+desc&docType_s=REPORT"
    )
    _SEARCH_API = "https://api.archives-ouvertes.fr/search/"
    _PAGE_SIZE = 100
    _MIN_ABSTRACT_CHARS = 100
    _RETRY_WAITS = (1, 3, 9)
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

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
        "producedDate_tdate",
        "submittedDate_tdate",
        "fileMain_s",
        "doiId_s",
        "docType_s",
        "domain_s",
        "primaryDomain_s",
        "labStructName_s",
        "instStructName_s",
        "publisherStructName_s",
        "journalTitle_s",
        "journalPublisher_s",
        "bookTitle_s",
        "conferenceTitle_s",
        "volume_s",
        "issue_s",
        "language_s",
        "licence_s",
        "citationRef_s",
    ))

    def __init__(self, db_conn, delay=0.5, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, accept=None, referer=None, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept or 'application/json,text/html;q=0.9,*/*;q=0.8'}",
            "-H", "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

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
        raw = self._curl(url, accept="application/json", referer=self._START_URL)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] invalid JSON from {label}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Text / HTML helpers
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
        seen: set[str] = set()
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
        raw_items = value if isinstance(value, list) else [value]
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
    # HAL API
    # ------------------------------------------------------------------

    @classmethod
    def _api_url(cls, params):
        return cls._SEARCH_API + "?" + urlencode(params, doseq=True)

    def _fetch_page(self, start, rows):
        params = [
            ("q", "*"),
            ("rows", str(rows)),
            ("start", str(start)),
            ("fq", "instStructAcronym_s:INSERM"),
            ("fq", "docType_s:REPORT"),
            ("fl", self._FIELDS),
            ("wt", "json"),
            ("sort", "docid asc"),
        ]
        return self._fetch_json(self._api_url(params), label=f"page start={start}")

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
        return self._first_value(doc, "citationRef_s")

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

    def _paper_from_doc(self, doc):
        docid = self._first_value(doc, "docid")
        hal_id = self._first_value(doc, "halId_s")
        version = self._first_value(doc, "version_i")
        external_id = hal_id or docid
        if not external_id:
            print(f"[{self.site_id}] skipping item without halId or docid")
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
        publishers = self._values(doc, "publisherStructName_s")
        publisher_str = "; ".join(publishers) if publishers else "; ".join(
            self._dedupe(labs + institutions)
        )

        uri = self._first_value(doc, "uri_s")
        url = uri or (f"{self.base_url}/{hal_id}" if hal_id else self.base_url)
        pdf_url = self._first_value(doc, "fileMain_s")
        doi = self._first_value(doc, "doiId_s")
        doc_type = self._first_value(doc, "docType_s")
        journal = self._first_value(doc, "journalTitle_s")
        volume = self._first_value(doc, "volume_s")
        issue = self._first_value(doc, "issue_s")

        original_filename = f"{hal_id}.pdf" if hal_id else None
        if not original_filename and pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if "." in tail and len(tail) <= 200:
                original_filename = tail

        submitted_raw = self._first_value(doc, "submittedDate_tdate")
        produced_raw = self._first_value(doc, "producedDate_tdate")

        published_date = ""
        for raw in (produced_raw, submitted_raw):
            parsed = self._parse_date(raw)
            if parsed:
                published_date = parsed
                break

        listed_date = self._parse_date(submitted_raw)

        metadata = {
            "posted_date": submitted_raw,
            "hal_id": hal_id,
            "version": version,
            "docid": docid,
            "doc_type": doc_type,
            "primary_domain": self._first_value(doc, "primaryDomain_s"),
            "domains": self._values(doc, "domain_s"),
            "journal_raw": journal,
            "volume": volume,
            "issue": issue,
            "book_title": self._first_value(doc, "bookTitle_s"),
            "conference_title": self._first_value(doc, "conferenceTitle_s"),
            "language": self._values(doc, "language_s"),
            "licence": self._first_value(doc, "licence_s"),
            "citation": self._first_value(doc, "citationRef_s"),
            "submitted_date": submitted_raw,
            "produced_date": produced_raw,
            "laboratories": labs,
            "institutions": institutions,
            "originalFilename": original_filename,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": docid or None,
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": doc_type,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "url": url,
            "pdf_url": pdf_url or None,
            "doi": doi,
            "journal": journal,
            "publisher": publisher_str,
            "department": "; ".join(self._dedupe(labs + institutions)),
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        start_offset = (self.delivery_cursor or {}).get("offset", 0)
        start = start_offset
        page = 0
        seen_ids: set[str] = set()
        total = None
        t_start = time.time()
        limit_or_inf: int | str = limit if limit is not None else "∞"

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
                data = self._fetch_page(start, self._PAGE_SIZE)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] list fetch error at start={start}: {exc}")
                break

            if data is None:
                print(f"[{self.site_id}] list fetch failed at start={start}; stopping")
                break

            response = data.get("response") or {}
            items = response.get("docs") or []

            if total is None:
                total = response.get("numFound")
                if total is not None:
                    print(f"[{self.site_id}] API records: {total}")

            if not items:
                print(f"[{self.site_id}] no more list records at start={start}")
                self._mark_exhausted()
                break

            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            items_not_deduped = 0
            for offset, doc in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                item_no = start + offset
                try:
                    docid = self._first_value(doc, "docid")
                    hal_id = self._first_value(doc, "halId_s")
                    dedup_key = docid or hal_id
                    if dedup_key and dedup_key in seen_ids:
                        continue
                    if dedup_key:
                        seen_ids.add(dedup_key)

                    items_not_deduped += 1

                    paper = self._paper_from_doc(doc)
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
            self._advance_cursor({"offset": start}, items_done=len(items))

            if items_not_deduped == 0 and items:
                print(f"[{self.site_id}] all items on page {page} were duplicates; stopping")
                break

            if len(items) < self._PAGE_SIZE:
                print(f"[{self.site_id}] last page (got {len(items)} < {self._PAGE_SIZE}); done")
                self._mark_exhausted()
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
