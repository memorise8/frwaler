# -*- coding: utf-8 -*-
"""Crawler for INRIA report records in HAL Science search."""

from __future__ import annotations

import json
import re
import subprocess
import time
from email.message import Message
from html import unescape
from urllib.parse import unquote, urlencode, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class InriaHalScienceSearchCrawler(BaseCrawler):
    site_id = "inria-hal-science-search"
    site_name = "Custom: inria-hal-science-search"
    base_url = "https://inria.hal.science"

    _START_URL = (
        "https://inria.hal.science/search/index/?q=collCode_s%3AINRIA2"
        "&rows=30&sort=submittedDate_s+desc&submitType_s=file&docType_s=REPORT"
    )
    _SEARCH_API = "https://api.archives-ouvertes.fr/search/inria/"
    _QUERY = "collCode_s:INRIA2"
    _LIST_FILTERS = ("submitType_s:file", "docType_s:REPORT")
    _PAGE_SIZE = 30
    _MAX_PAGES = 200
    _WALL_CLOCK_BUDGET = 25 * 60
    _WALL_CLOCK_MARGIN = 30
    _MIN_ABSTRACT_CHARS = 100
    _RETRY_WAITS = (1, 3, 9)

    _LIST_FIELDS = ",".join((
        "docid",
        "halId_s",
        "version_i",
        "uri_s",
        "title_s",
        "submittedDate_s",
    ))
    _DETAIL_FIELDS = ",".join((
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
        "modifiedDate_s",
        "fileMain_s",
        "files_s",
        "fileType_s",
        "fileLicenses_s",
        "doiId_s",
        "docType_s",
        "domain_s",
        "primaryDomain_s",
        "en_domainAllCodeLabel_fs",
        "labStructName_s",
        "instStructName_s",
        "rteamStructName_s",
        "structName_s",
        "structAcronym_s",
        "authorityInstitution_s",
        "journalTitle_s",
        "journalPublisher_s",
        "journalVolume_s",
        "journalIssue_s",
        "serie_s",
        "series_s",
        "volume_s",
        "issue_s",
        "bookTitle_s",
        "conferenceTitle_s",
        "reportNumber_s",
        "language_s",
        "licence_s",
        "citationRef_s",
        "label_s",
        "collCode_s",
        "collName_s",
    ))

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, accept=None, referer=None, timeout=45):
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
            f"Accept: {accept or 'application/json,text/html;q=0.9,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = "unknown error"
        for attempt, wait in enumerate(self._RETRY_WAITS, start=1):
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
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout:
                    return stdout.decode("utf-8", errors="replace")
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(self._RETRY_WAITS):
                print(
                    f"[{self.site_id}] curl failed {attempt}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _curl_head_headers(self, url, *, timeout=20):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "-I",
            "--connect-timeout",
            "10",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        last_error = "unknown error"
        for attempt, wait in enumerate(self._RETRY_WAITS, start=1):
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
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout:
                    return stdout.decode("utf-8", errors="replace")
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(self._RETRY_WAITS):
                print(
                    f"[{self.site_id}] HEAD failed {attempt}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] HEAD failed after 3 attempts for {url}: {last_error}")
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
        match = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", text)
        if match:
            return match.group(0)
        match = re.search(r"\b(19|20)\d{2}-\d{2}\b", text)
        if match:
            return match.group(0)
        match = re.search(r"\b(19|20)\d{2}\b", text)
        return match.group(0) if match else ""

    @staticmethod
    def _api_url(params):
        return InriaHalScienceSearchCrawler._SEARCH_API + "?" + urlencode(params, doseq=True)

    # ------------------------------------------------------------------
    # HAL API
    # ------------------------------------------------------------------

    def _fetch_list(self, start):
        params = [
            ("q", self._QUERY),
            ("rows", str(self._PAGE_SIZE)),
            ("start", str(start)),
            ("sort", "submittedDate_s desc"),
            ("fq", self._LIST_FILTERS[0]),
            ("fq", self._LIST_FILTERS[1]),
            ("fl", self._LIST_FIELDS),
            ("wt", "json"),
        ]
        return self._fetch_json(self._api_url(params), label=f"list start={start}")

    def _fetch_detail(self, item):
        docid = self._first_value(item, "docid")
        hal_id = self._first_value(item, "halId_s")
        if docid:
            query = f"docid:{docid}"
            label = docid
        elif hal_id:
            query = f"halId_s:{hal_id}"
            label = hal_id
        else:
            return None

        params = [
            ("q", query),
            ("rows", "1"),
            ("fq", self._LIST_FILTERS[0]),
            ("fq", self._LIST_FILTERS[1]),
            ("fl", self._DETAIL_FIELDS),
            ("wt", "json"),
        ]
        data = self._fetch_json(self._api_url(params), label=f"detail {label}")
        docs = (((data or {}).get("response") or {}).get("docs") or [])
        if docs:
            return docs[0]
        return None

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

    def _listed_date(self, doc):
        return self._parse_date(self._first_value(doc, "submittedDate_s", "releasedDate_s"))

    def _pdf_url(self, doc):
        file_urls = self._values(doc, "files_s")
        for url in file_urls:
            if urlparse(url).path.lower().endswith(".pdf"):
                return url
        if file_urls:
            return file_urls[0]
        return self._first_value(doc, "fileMain_s")

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
        if "." in tail and len(tail) <= 240:
            return tail
        return None

    @staticmethod
    def _filename_from_content_disposition(headers_text):
        if not headers_text:
            return None
        blocks = [b for b in re.split(r"\r?\n\r?\n", headers_text) if b.strip()]
        if not blocks:
            return None
        message = Message()
        for line in blocks[-1].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            message[key.strip()] = value.strip()
        content_disposition = message.get("content-disposition", "")
        if not content_disposition:
            return None
        match = re.search(r'filename\*=UTF-8\'\'([^;]+)', content_disposition, re.I)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        match = re.search(r'filename="?([^";]+)"?', content_disposition, re.I)
        if match:
            return unquote(match.group(1).strip())
        return None

    def _original_filename(self, pdf_url):
        filename = self._filename_from_url(pdf_url)
        if filename:
            return filename
        if pdf_url:
            headers = self._curl_head_headers(pdf_url)
            filename = self._filename_from_content_disposition(headers)
            if filename:
                return filename
        return None

    def _external_id(self, doc):
        docid = self._first_value(doc, "docid")
        if docid:
            return docid
        hal_id = self._first_value(doc, "halId_s")
        version = self._first_value(doc, "version_i")
        if hal_id and version:
            return f"{hal_id}v{version}"
        return hal_id or self._first_value(doc, "uri_s") or None

    def _paper_from_doc(self, doc):
        docid = self._first_value(doc, "docid")
        hal_id = self._first_value(doc, "halId_s")
        version = self._first_value(doc, "version_i")
        external_id = self._external_id(doc)
        if not external_id:
            print(f"[{self.site_id}] skipping item without docid or HAL id")
            return None

        title = self._title(doc)
        if not title:
            print(f"[{self.site_id}] skipping {external_id}: missing title")
            return None

        abstract = self._abstract(doc)
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract below save threshold ({len(abstract)} chars)"
            )
            return None

        authors = self._values(doc, "authFullName_s")
        keywords = self._keywords(doc)
        authority = self._values(doc, "authorityInstitution_s")
        institutions = self._values(doc, "instStructName_s")
        labs = self._values(doc, "labStructName_s")
        teams = self._values(doc, "rteamStructName_s")
        structures = self._values(doc, "structName_s")
        publisher_values = self._dedupe(authority + institutions)
        department_values = self._dedupe(labs + teams)
        if not department_values:
            department_values = self._dedupe(structures)

        url = self._first_value(doc, "uri_s") or (
            f"{self.base_url}/{hal_id}v{version}" if hal_id and version else self.base_url
        )
        pdf_url = self._pdf_url(doc)
        original_filename = self._original_filename(pdf_url)
        published_date = self._published_date(doc)
        listed_date = self._listed_date(doc)
        submitted_raw = self._first_value(doc, "submittedDate_s")
        journal = self._first_value(doc, "journalTitle_s")
        doc_type = self._first_value(doc, "docType_s")
        category = doc_type or self._first_value(doc, "primaryDomain_s")

        metadata = {
            "source_start_url": self._START_URL,
            "list_api": self._SEARCH_API,
            "list_query": self._QUERY,
            "list_filters": list(self._LIST_FILTERS),
            "detail_api_query": f"docid:{docid}" if docid else f"halId_s:{hal_id}",
            "posted_date": submitted_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": {
                "journalTitle_s": self._first_value(doc, "journalTitle_s"),
                "journalPublisher_s": self._first_value(doc, "journalPublisher_s"),
            },
            "series": self._first_value(doc, "serie_s", "series_s"),
            "volume": self._first_value(doc, "journalVolume_s", "volume_s"),
            "issue": self._first_value(doc, "journalIssue_s", "issue_s"),
            "docid": docid,
            "hal_id": hal_id,
            "version": version,
            "post_number": docid or hal_id,
            "doc_type": doc_type,
            "primary_domain": self._first_value(doc, "primaryDomain_s"),
            "domains": self._values(doc, "domain_s"),
            "domain_labels": self._values(doc, "en_domainAllCodeLabel_fs"),
            "report_number": self._first_value(doc, "reportNumber_s"),
            "authority_institution": authority,
            "book_title": self._first_value(doc, "bookTitle_s"),
            "conference_title": self._first_value(doc, "conferenceTitle_s"),
            "language": self._values(doc, "language_s"),
            "licence": self._first_value(doc, "licence_s"),
            "file_licenses": self._values(doc, "fileLicenses_s"),
            "citation": self._first_value(doc, "citationRef_s"),
            "submitted_date": submitted_raw,
            "released_date": self._first_value(doc, "releasedDate_s"),
            "modified_date": self._first_value(doc, "modifiedDate_s"),
            "produced_date": self._first_value(doc, "producedDate_s"),
            "publication_date": self._first_value(doc, "publicationDate_s"),
            "collection_codes": self._values(doc, "collCode_s"),
            "collection_names": self._values(doc, "collName_s"),
            "laboratories": labs,
            "research_teams": teams,
            "institutions": institutions,
            "structures": structures,
            "structure_acronyms": self._values(doc, "structAcronym_s"),
            "raw_doc": doc,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": docid or hal_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors),
            "publisher": "; ".join(publisher_values),
            "department": "; ".join(department_values),
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords),
            "category": category,
            "doi": self._first_value(doc, "doiId_s"),
            "original_filename": original_filename,
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
        limit_or_inf = limit if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages; stopping")
                break

            elapsed = time.time() - t_start
            if elapsed >= self._WALL_CLOCK_BUDGET - self._WALL_CLOCK_MARGIN:
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

            new_records = 0
            for offset, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                elapsed = time.time() - t_start
                if elapsed >= self._WALL_CLOCK_BUDGET - self._WALL_CLOCK_MARGIN:
                    print(
                        f"[{self.site_id}] approaching 25-minute wall-clock budget "
                        f"({elapsed:.0f}s elapsed); stopping cleanly"
                    )
                    return saved

                item_no = start + offset
                try:
                    uri = self._first_value(item, "uri_s")
                    docid = self._first_value(item, "docid")
                    hal_id = self._first_value(item, "halId_s")
                    dedup_key = uri or docid or hal_id
                    if dedup_key and dedup_key in seen_urls:
                        continue
                    if dedup_key:
                        seen_urls.add(dedup_key)
                    new_records += 1

                    time.sleep(self.detail_delay)
                    detail = self._fetch_detail(item)
                    if not detail:
                        print(f"[{self.site_id}] item {item_no} failed: missing detail")
                        continue

                    paper = self._paper_from_doc(detail)
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

            if new_records == 0:
                print(f"[{self.site_id}] no new records at page {page}; stopping")
                break

            start += len(items)
            if len(items) < self._PAGE_SIZE:
                print(f"[{self.site_id}] last page (got {len(items)} < {self._PAGE_SIZE}); done")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
