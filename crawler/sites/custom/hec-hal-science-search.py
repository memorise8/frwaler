# -*- coding: utf-8 -*-
"""HEC HAL Science search crawler.

The public search UI at hec.hal.science/search/index/ can return robot
protection pages to non-browser clients. The real data is exposed through
HAL's public Solr API:

List API:
  https://api.archives-ouvertes.fr/search/hec/

Detail API:
  https://api.archives-ouvertes.fr/search/hec/?q=docid:<docid>&rows=1

Starting URL reference:
  https://hec.hal.science/search/index/?q=%2A&rows=30&submitType_s=file&docType_s=THESE+OR+ART+OR+COMM
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from html import unescape
from urllib.parse import unquote, urlencode, urlparse

# Absolute import: spec_from_file_location has no package context.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
for _ in range(6):
    if os.path.isdir(os.path.join(_ROOT, "crawler")) and os.path.isfile(
        os.path.join(_ROOT, "crawler", "__init__.py")
    ):
        break
    _PARENT = os.path.dirname(_ROOT)
    if _PARENT == _ROOT:
        break
    _ROOT = _PARENT
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_API_BASE = "https://api.archives-ouvertes.fr/search/hec/"
_START_URL = (
    "https://hec.hal.science/search/index/?q=%2A&rows=30"
    "&submitType_s=file&docType_s=THESE+OR+ART+OR+COMM"
)
_PAGE_SIZE = 30
_MAX_PAGES = 200
_WALL_BUDGET_SECONDS = 25 * 60
_RETRY_WAITS = (1, 3, 9)
_MIN_ABSTRACT_LEN = 100

_FIELDS = ",".join(
    (
        "docid",
        "halId_s",
        "version_i",
        "uri_s",
        "label_s",
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
        "linkExtUrl_s",
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
        "collCode_s",
        "serie_s",
        "volume_s",
        "issue_s",
        "publisher_s",
        "reportNumber_s",
        "openAccess_bool",
    )
)


class HecHalScienceSearchCrawler(BaseCrawler):
    site_id = "hec-hal-science-search"
    site_name = "Custom: hec-hal-science-search"
    base_url = "https://hec.hal.science"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
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
            "Accept: application/json,text/html;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
            "-H",
            f"Referer: {_START_URL}",
            url,
        ]

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
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout:
                    return stdout.decode("utf-8", errors="replace")
                last_error = f"exit={result.returncode} stderr={stderr}"

            wait = _RETRY_WAITS[attempt - 1]
            if attempt < 3:
                print(
                    f"[{self.site_id}] curl failed {attempt}/3: {last_error}; "
                    f"retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts: {last_error}")
        return None

    def _fetch_json(self, url: str, *, label: str = "") -> dict | None:
        raw = self._curl(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] invalid JSON from {label or url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # API builders
    # ------------------------------------------------------------------

    def _list_url(self, start: int) -> str:
        params = [
            ("q", "*"),
            ("rows", str(_PAGE_SIZE)),
            ("start", str(start)),
            ("fq", "submitType_s:file"),
            ("fq", "docType_s:(THESE OR ART OR COMM)"),
            ("fl", "docid,halId_s,uri_s,submittedDate_s"),
            ("wt", "json"),
            ("sort", "submittedDate_tdate desc"),
        ]
        return _API_BASE + "?" + urlencode(params)

    def _detail_url(self, list_doc: dict) -> str | None:
        docid = self._first(list_doc, "docid")
        if docid:
            query = f"docid:{docid}"
        else:
            hal_id = self._first(list_doc, "halId_s")
            if not hal_id:
                return None
            query = f'halId_s:"{hal_id}"'

        params = [
            ("q", query),
            ("rows", "1"),
            ("start", "0"),
            ("fq", "submitType_s:file"),
            ("fq", "docType_s:(THESE OR ART OR COMM)"),
            ("fl", _FIELDS),
            ("wt", "json"),
        ]
        return _API_BASE + "?" + urlencode(params)

    def _fetch_detail_doc(self, list_doc: dict) -> dict | None:
        url = self._detail_url(list_doc)
        if not url:
            return None
        data = self._fetch_json(url, label=f"detail {self._first(list_doc, 'docid')}")
        docs = (data or {}).get("response", {}).get("docs", [])
        if not docs:
            return None
        return docs[0]

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(value) -> str:
        text = str(value or "")
        if "<" not in text and "&" not in text:
            return text

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup

                return BeautifulSoup(text, parser).get_text(" ", strip=True)
            except Exception:
                continue

        return re.sub(r"<[^>]+>", " ", text)

    @classmethod
    def _clean(cls, value) -> str:
        text = unescape(cls._strip_html(value))
        text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _vals(cls, doc: dict, key: str) -> list[str]:
        value = doc.get(key)
        if value in (None, ""):
            return []
        raw_items = value if isinstance(value, list) else [value]

        seen: set[str] = set()
        items: list[str] = []
        for item in raw_items:
            cleaned = cls._clean(item)
            marker = cleaned.lower()
            if cleaned and marker not in seen:
                seen.add(marker)
                items.append(cleaned)
        return items

    @classmethod
    def _first(cls, doc: dict, *keys: str) -> str:
        for key in keys:
            values = cls._vals(doc, key)
            if values:
                return values[0]
        return ""

    @staticmethod
    def _parse_date(raw) -> str | None:
        text = str(raw or "")
        match = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", text)
        if match:
            return match.group(0)
        match = re.search(r"\b(19|20)\d{2}-\d{2}\b", text)
        if match:
            return match.group(0) + "-01"
        match = re.search(r"\b(19|20)\d{2}\b", text)
        if match:
            return match.group(0) + "-01-01"
        return None

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        try:
            path = urlparse(url).path
            segment = unquote(path.rstrip("/").split("/")[-1].split("?")[0])
            if segment and "." in segment and len(segment) <= 200:
                return segment
        except Exception:
            return None
        return None

    @staticmethod
    def _dedupe_extend(target: list[str], values: list[str], seen: set[str]) -> None:
        for value in values:
            marker = value.lower()
            if marker not in seen:
                seen.add(marker)
                target.append(value)

    # ------------------------------------------------------------------
    # Record normalization
    # ------------------------------------------------------------------

    def _paper_from_doc(self, doc: dict) -> dict | None:
        docid = self._first(doc, "docid")
        hal_id = self._first(doc, "halId_s")
        external_id = docid or hal_id
        if not external_id:
            print(f"[{self.site_id}] skipping item: no docid or HAL id")
            return None

        title_values = (
            self._vals(doc, "title_s")
            or self._vals(doc, "en_title_s")
            or self._vals(doc, "fr_title_s")
        )
        title = " / ".join(title_values) if title_values else self._first(doc, "label_s")
        if not title:
            print(f"[{self.site_id}] skipping {external_id}: no title")
            return None

        abstract_values: list[str] = []
        seen_abstracts: set[str] = set()
        for key in ("abstract_s", "en_abstract_s", "fr_abstract_s"):
            self._dedupe_extend(abstract_values, self._vals(doc, key), seen_abstracts)
        abstract = max(abstract_values, key=len) if abstract_values else ""
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None
        if len(abstract) < _MIN_ABSTRACT_LEN:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract below save threshold ({len(abstract)} chars)"
            )
            return None

        published_date = None
        for date_key in ("producedDate_s", "publicationDate_s", "releasedDate_s"):
            published_date = self._parse_date(self._first(doc, date_key))
            if published_date:
                break

        submitted_raw = self._first(doc, "submittedDate_s")
        listed_date = self._parse_date(submitted_raw)

        authors = self._vals(doc, "authFullName_s")
        labs = self._vals(doc, "labStructName_s")
        institutions = self._vals(doc, "instStructName_s")
        publishers = self._vals(doc, "publisher_s")

        keywords: list[str] = []
        seen_keywords: set[str] = set()
        for key in ("keyword_s", "en_keyword_s", "fr_keyword_s"):
            self._dedupe_extend(keywords, self._vals(doc, key), seen_keywords)

        file_urls = self._vals(doc, "files_s")
        pdf_url = next((u for u in file_urls if u.lower().endswith(".pdf")), None)
        if not pdf_url:
            pdf_url = self._first(doc, "fileMain_s") or None
        original_filename = self._filename_from_url(pdf_url)

        journal = self._first(doc, "journalTitle_s") or None
        doc_type = self._first(doc, "docType_s") or None
        doi = self._first(doc, "doiId_s") or None
        version = self._first(doc, "version_i") or None
        url = self._first(doc, "uri_s") or (
            f"{self.base_url}/{hal_id}" if hal_id else self.base_url
        )

        publisher_values = institutions or publishers
        metadata = {
            "posted_date": submitted_raw,
            "originalFilename": original_filename,
            "docid": docid,
            "halId": hal_id,
            "version": version,
            "docType": doc_type,
            "openAccess": doc.get("openAccess_bool"),
            "collCodes": doc.get("collCode_s") or [],
            "primaryDomain": self._first(doc, "primaryDomain_s"),
            "domains": self._vals(doc, "domain_s"),
            "language": self._vals(doc, "language_s"),
            "licence": self._first(doc, "licence_s"),
            "laboratories": labs,
            "institutions": institutions,
            "submittedDate": submitted_raw,
            "releasedDate": self._first(doc, "releasedDate_s"),
            "modifiedDate": self._first(doc, "modifiedDate_s"),
            "producedDate": self._first(doc, "producedDate_s"),
            "publicationDate": self._first(doc, "publicationDate_s"),
            "reportNumber": self._first(doc, "reportNumber_s"),
            "fileMain_s": self._first(doc, "fileMain_s"),
            "files_s": file_urls,
            "linkExtUrl": self._first(doc, "linkExtUrl_s"),
            "raw": doc,
        }
        if journal:
            metadata["journal_raw"] = journal
        if self._first(doc, "bookTitle_s"):
            metadata["bookTitle"] = self._first(doc, "bookTitle_s")
        if self._first(doc, "conferenceTitle_s"):
            metadata["conferenceTitle"] = self._first(doc, "conferenceTitle_s")
        if self._first(doc, "serie_s"):
            metadata["series"] = self._first(doc, "serie_s")
        if self._first(doc, "volume_s"):
            metadata["volume"] = self._first(doc, "volume_s")
        if self._first(doc, "issue_s"):
            metadata["issue"] = self._first(doc, "issue_s")
        if self._first(doc, "citationRef_s"):
            metadata["citationRef"] = self._first(doc, "citationRef_s")
        if doi:
            metadata["doi"] = doi

        return {
            "id": str(uuid.uuid4()),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": docid or None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": "; ".join(publisher_values) if publisher_values else None,
            "department": "; ".join(labs) if labs else None,
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords) if keywords else None,
            "category": doc_type,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        start = 0
        seen_urls: set[str] = set()
        started_at = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break

            if time.time() - started_at >= _WALL_BUDGET_SECONDS - 5:
                print(f"[{self.site_id}] approaching 25-minute budget; exiting cleanly")
                break

            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")
                break

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            data = self._fetch_json(self._list_url(start), label=f"page {page} start={start}")
            if data is None:
                print(f"[{self.site_id}] failed to fetch page {page}; stopping")
                break

            response = data.get("response") or {}
            docs = response.get("docs") or []
            if not docs:
                print(f"[{self.site_id}] page {page} returned 0 records; done")
                break

            if page == 0:
                total = response.get("numFound")
                print(f"[{self.site_id}] total records on server: {total}")

            new_unique_on_page = 0
            for list_doc in docs:
                if limit is not None and saved >= limit:
                    break

                if time.time() - started_at >= _WALL_BUDGET_SECONDS - 5:
                    print(f"[{self.site_id}] approaching 25-minute budget; exiting cleanly")
                    return saved

                item_id = (
                    self._first(list_doc, "docid")
                    or self._first(list_doc, "halId_s")
                    or "?"
                )

                try:
                    url_key = self._first(list_doc, "uri_s") or item_id
                    if url_key in seen_urls:
                        continue
                    seen_urls.add(url_key)
                    new_unique_on_page += 1

                    if self._delay:
                        time.sleep(self._delay)

                    detail_doc = self._fetch_detail_doc(list_doc)
                    if detail_doc is None:
                        print(f"[{self.site_id}] item {item_id} failed: no detail API response")
                        continue

                    paper = self._paper_from_doc(detail_doc)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if new_unique_on_page == 0:
                print(f"[{self.site_id}] page {page} had no new URLs; stopping")
                break

            page += 1
            start += len(docs)

            total_found = response.get("numFound")
            if isinstance(total_found, int) and start >= total_found:
                print(f"[{self.site_id}] reached server total ({total_found}); done")
                break
            if len(docs) < _PAGE_SIZE:
                print(f"[{self.site_id}] last page (got {len(docs)} < {_PAGE_SIZE}); done")
                break

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
