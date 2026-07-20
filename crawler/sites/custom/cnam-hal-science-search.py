# -*- coding: utf-8 -*-
"""Crawler for CNAM HAL Science search (OTHER/REPORT doc types).

cnam.hal.science is protected by Anubis bot-detection (PoW challenge); all
data is fetched via the official HAL Solr API at api.archives-ouvertes.fr,
filtered to the CNAM collection with open-access files and document types
OTHER and REPORT.

Total corpus: ~642 records (as of 2026-05).
Starting URL: https://cnam.hal.science/search/index/?q=%2A&rows=30&submitType_s=file&docType_s=OTHER+OR+REPORT
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urlencode, urlparse

# Absolute import: spec_from_file_location has no package context, so we walk
# up from __file__ until we find the project root (where 'crawler' package
# lives) and inject it into sys.path.
_here = os.path.dirname(os.path.abspath(__file__))
_root = _here
for _ in range(6):
    if os.path.isdir(os.path.join(_root, "crawler")) and os.path.isfile(
        os.path.join(_root, "crawler", "__init__.py")
    ):
        break
    _parent = os.path.dirname(_root)
    if _parent == _root:
        break
    _root = _parent
if _root not in sys.path:
    sys.path.insert(0, _root)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_API_BASE = "https://api.archives-ouvertes.fr/search/"
_PAGE_SIZE = 30
_MIN_ABSTRACT = 100
_RETRY_WAITS = (1, 3, 9)
_MAX_PAGES = 200
_WALL_BUDGET = 25 * 60  # seconds

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
    "files_s",
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
    "label_s",
    "serie_s",
    "volume_s",
    "issue_s",
    "publisher_s",
    "reportNumber_s",
    "openAccess_bool",
))


class CnamHalScienceSearchCrawler(BaseCrawler):
    """Crawls CNAM HAL Science (OTHER/REPORT) via api.archives-ouvertes.fr."""

    site_id = "cnam-hal-science-search"
    site_name = "Custom: cnam-hal-science-search"
    base_url = "https://cnam.hal.science"

    _START_URL = (
        "https://cnam.hal.science/search/index/?q=%2A&rows=30"
        "&submitType_s=file&docType_s=OTHER+OR+REPORT"
    )

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json,text/html;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
            "-H", f"Referer: {self._START_URL}",
            url,
        ]
        last_err = "unknown"
        for attempt in range(1, 4):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_err = str(exc)
            else:
                stdout = r.stdout or b""
                stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
                if r.returncode == 0 and stdout:
                    return stdout.decode("utf-8", errors="replace")
                last_err = f"exit={r.returncode} stderr={stderr}"

            wait = _RETRY_WAITS[attempt - 1]
            if attempt < 3:
                print(
                    f"[{self.site_id}] curl failed {attempt}/3: {last_err}; "
                    f"retry in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts: {last_err}")
        return None

    def _fetch_json(self, url, label=""):
        raw = self._curl(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] invalid JSON from {label or url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(text):
        if not text or "<" not in str(text):
            return str(text or "")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(str(text), parser).get_text(" ", strip=True)
            except Exception:
                continue
        return re.sub(r"<[^>]+>", " ", str(text))

    @classmethod
    def _clean(cls, value):
        text = cls._strip_html(value)
        text = unescape(str(text))
        text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _vals(cls, doc, key):
        v = doc.get(key)
        if v in (None, ""):
            return []
        items = v if isinstance(v, list) else [v]
        seen_lc: set[str] = set()
        result = []
        for item in items:
            s = cls._clean(item)
            if s and s.lower() not in seen_lc:
                seen_lc.add(s.lower())
                result.append(s)
        return result

    @classmethod
    def _first(cls, doc, *keys):
        for k in keys:
            vs = cls._vals(doc, k)
            if vs:
                return vs[0]
        return ""

    @staticmethod
    def _parse_date(raw):
        s = str(raw or "")
        m = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", s)
        if m:
            return m.group(0)
        m = re.search(r"\b(19|20)\d{2}-\d{2}\b", s)
        if m:
            return m.group(0) + "-01"
        m = re.search(r"\b(19|20)\d{2}\b", s)
        return (m.group(0) + "-01-01") if m else ""

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        try:
            seg = unquote(urlparse(url).path.rstrip("/").split("/")[-1].split("?")[0])
            if seg and "." in seg and len(seg) <= 200:
                return seg
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # API URL builder
    # ------------------------------------------------------------------

    def _list_url(self, start):
        params = [
            ("q", "*"),
            ("rows", str(_PAGE_SIZE)),
            ("start", str(start)),
            ("fq", "submitType_s:file"),
            ("fq", "docType_s:(OTHER OR REPORT)"),
            ("fq", "collCode_s:CNAM"),
            ("fl", _FIELDS),
            ("wt", "json"),
            ("sort", "submittedDate_tdate desc"),
        ]
        return _API_BASE + "?" + urlencode(params)

    # ------------------------------------------------------------------
    # Record normalization
    # ------------------------------------------------------------------

    def _paper_from_doc(self, doc):
        docid = self._first(doc, "docid")
        hal_id = self._first(doc, "halId_s")
        version = self._first(doc, "version_i")

        external_id = hal_id or (f"docid-{docid}" if docid else None)
        if not external_id:
            print(f"[{self.site_id}] skipping item: no HAL id or docid")
            return None

        # post_number: numeric docid for incremental max(post_number) tracking
        post_number = docid if docid else None

        title_vals = (
            self._vals(doc, "title_s")
            or self._vals(doc, "en_title_s")
            or self._vals(doc, "fr_title_s")
        )
        title = " / ".join(title_vals) if title_vals else self._first(doc, "label_s")
        if not title:
            print(f"[{self.site_id}] skipping {external_id}: no title")
            return None

        # Collect all language variants, keep the longest as primary
        abs_parts: list[str] = []
        seen_abs: set[str] = set()
        for key in ("abstract_s", "en_abstract_s", "fr_abstract_s"):
            for v in self._vals(doc, key):
                if v.lower() not in seen_abs:
                    seen_abs.add(v.lower())
                    abs_parts.append(v)
        abstract = max(abs_parts, key=len) if abs_parts else ""

        if len(abstract) < 50:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None
        if len(abstract) < _MIN_ABSTRACT:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract below save threshold ({len(abstract)} chars)"
            )
            return None

        published_date = ""
        for dk in ("producedDate_s", "publicationDate_s", "releasedDate_s", "submittedDate_s"):
            published_date = self._parse_date(self._first(doc, dk))
            if published_date:
                break
        listed_date = self._parse_date(self._first(doc, "submittedDate_s"))

        authors = self._vals(doc, "authFullName_s")

        keywords: list[str] = []
        seen_kw: set[str] = set()
        for kk in ("keyword_s", "en_keyword_s", "fr_keyword_s"):
            for kw in self._vals(doc, kk):
                if kw.lower() not in seen_kw:
                    seen_kw.add(kw.lower())
                    keywords.append(kw)

        labs = self._vals(doc, "labStructName_s")
        insts = self._vals(doc, "instStructName_s")
        publisher_vals = self._vals(doc, "publisher_s")
        # Publisher: institutions/labs; fall back to publisher field
        departments = list(dict.fromkeys(labs + insts))

        url = self._first(doc, "uri_s") or (
            f"{self.base_url}/{hal_id}" if hal_id else self.base_url
        )

        # Prefer a direct .pdf URL from files_s, fall back to fileMain_s
        file_urls = self._vals(doc, "files_s")
        pdf_url = next((u for u in file_urls if u.lower().endswith(".pdf")), None)
        if not pdf_url:
            pdf_url = self._first(doc, "fileMain_s") or None

        doi = self._first(doc, "doiId_s") or None
        journal = self._first(doc, "journalTitle_s") or None
        doc_type = self._first(doc, "docType_s") or None
        original_filename = self._filename_from_url(pdf_url)

        metadata: dict = {
            "posted_date": self._first(doc, "submittedDate_s"),
            "originalFilename": original_filename,
            "halId": hal_id,
            "docid": docid,
            "version": version,
            "docType": doc_type,
            "openAccess": doc.get("openAccess_bool"),
            "collCodes": doc.get("collCode_s") or [],
            "primaryDomain": self._first(doc, "primaryDomain_s"),
            "domains": self._vals(doc, "domain_s"),
            "language": self._vals(doc, "language_s"),
            "licence": self._first(doc, "licence_s"),
            "laboratories": labs,
            "institutions": insts,
            "submittedDate": self._first(doc, "submittedDate_s"),
            "releasedDate": self._first(doc, "releasedDate_s"),
            "producedDate": self._first(doc, "producedDate_s"),
            "reportNumber": self._first(doc, "reportNumber_s"),
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
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "authors": "; ".join(authors),
            "publisher": (
                "; ".join(departments) if departments
                else ("; ".join(publisher_vals) if publisher_vals else None)
            ),
            "journal": journal,
            "keywords": ", ".join(keywords),
            "category": doc_type,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl CNAM HAL Science (OTHER/REPORT) via api.archives-ouvertes.fr.

        Paginates the HAL Solr API (collCode_s:CNAM, submitType_s:file,
        docType_s:(OTHER OR REPORT)), saves each record via _save_paper.
        Items with abstracts shorter than 100 chars are skipped.
        """
        saved = 0
        start = 0
        page = 0
        seen: set[str] = set()
        total_on_server: int | None = None
        t0 = time.time()
        lim_str = str(limit) if limit is not None else "∞"

        while True:
            # Limit guard
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget
            if time.time() - t0 >= _WALL_BUDGET:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping.")
                break

            # Safety page cap
            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Rate-limit (skip on first page)
            if page > 0:
                time.sleep(self._delay)

            url = self._list_url(start)
            data = self._fetch_json(url, label=f"page {page + 1} start={start}")
            if data is None:
                print(f"[{self.site_id}] Failed to fetch page {page + 1}. Stopping.")
                break

            try:
                response = data["response"]
                docs = response["docs"]
                if total_on_server is None:
                    total_on_server = response.get("numFound")
                    if total_on_server is not None:
                        print(f"[{self.site_id}] Total records on server: {total_on_server}")
            except (KeyError, TypeError) as exc:
                print(
                    f"[{self.site_id}] Unexpected API structure at page {page + 1}: {exc}. "
                    "Stopping."
                )
                break

            if not docs:
                print(f"[{self.site_id}] Page {page + 1} returned 0 docs. Done.")
                break

            # Progress every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page + 1}: saved {saved}/{lim_str}")

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                try:
                    hal_id = str(doc.get("halId_s") or "")
                    uri = str(doc.get("uri_s") or "")
                    dedup_key = uri or hal_id
                    if dedup_key and dedup_key in seen:
                        continue
                    if dedup_key:
                        seen.add(dedup_key)

                    paper = self._paper_from_doc(doc)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{lim_str}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    ident = doc.get("halId_s") or doc.get("docid") or "?"
                    print(f"[{self.site_id}] item {ident} failed: {exc}")
                    continue

            start += len(docs)
            page += 1

            if len(docs) < _PAGE_SIZE:
                print(f"[{self.site_id}] Last page (got {len(docs)} < {_PAGE_SIZE}). Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
