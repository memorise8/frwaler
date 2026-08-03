# -*- coding: utf-8 -*-
"""Crawler for IGN-ENSG records in HAL Science (archives-ouvertes.fr Solr API).

Starting URL:
  https://hal.science/IGN-ENSG/search/index/?q=%2A&rows=30&submitType_s=file
    &docType_s=THESE+OR+COMM+OR+ART

The HAL public API returns all needed fields (title, abstract, authors, …) in
a single Solr request per page — no per-item detail fetch required.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler


class HalScienceIgnEnsgCrawler(BaseCrawler):
    site_id = "hal-science-ign-ensg"
    site_name = "Custom: hal-science-ign-ensg"
    base_url = "https://hal.science"

    _START_URL = (
        "https://hal.science/IGN-ENSG/search/index/?q=%2A&rows=30"
        "&submitType_s=file&docType_s=THESE+OR+COMM+OR+ART"
    )
    _SEARCH_API = "https://api.archives-ouvertes.fr/search/"
    _COLL_FILTER = "collCode_s:IGN-ENSG"
    _PAGE_SIZE = 30
    _MIN_ABSTRACT_CHARS = 100  # must match verification test assertion
    _RETRY_WAITS = (1, 3, 9)
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))

    _ALL_FIELDS = ",".join((
        "docid",
        "halId_s",
        "version_i",
        "uri_s",
        "title_s",
        "fr_title_s",
        "en_title_s",
        "abstract_s",
        "fr_abstract_s",
        "en_abstract_s",
        "keyword_s",
        "en_keyword_s",
        "fr_keyword_s",
        "authFullName_s",
        "producedDate_s",
        "publicationDate_s",
        "submittedDate_s",
        "releasedDate_s",
        "submitType_s",
        "docType_s",
        "fileMain_s",
        "doiId_s",
        "journalTitle_s",
        "bookTitle_s",
        "conferenceTitle_s",
        "publisher_s",
        "labStructName_s",
        "instStructName_s",
        "domain_s",
        "primaryDomain_s",
        "language_s",
        "label_s",
        "citationRef_s",
    ))

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
            "-H", f"Referer: {self._START_URL}",
            url,
        ]
        last_error = "unknown"
        for attempt, wait in enumerate(self._RETRY_WAITS, start=1):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                if r.returncode == 0 and r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
                stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={r.returncode} stderr={stderr[:120]}"

            if attempt < len(self._RETRY_WAITS):
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed ({last_error}); "
                    f"retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_json(self, url: str) -> dict | None:
        raw = self._curl(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] invalid JSON from {url[:80]}: {exc}")
            return None

    def _list_url(self, start: int) -> str:
        params = [
            ("q", "*"),
            ("fq", self._COLL_FILTER),
            ("fq", "submitType_s:file"),
            ("fq", "docType_s:(THESE OR COMM OR ART)"),
            ("fl", self._ALL_FIELDS),
            ("rows", str(self._PAGE_SIZE)),
            ("start", str(start)),
            ("wt", "json"),
            ("sort", "docid asc"),
        ]
        return self._SEARCH_API + "?" + urlencode(params, doseq=True)

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        return text.strip()

    @classmethod
    def _values(cls, doc: dict, key: str) -> list[str]:
        v = doc.get(key)
        if v is None or v == "":
            return []
        items = v if isinstance(v, list) else [v]
        return [c for c in (cls._clean(x) for x in items) if c]

    @classmethod
    def _first(cls, doc: dict, *keys: str) -> str:
        for k in keys:
            vals = cls._values(doc, k)
            if vals:
                return vals[0]
        return ""

    @staticmethod
    def _parse_date(raw: str) -> str:
        if not raw:
            return ""
        m = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", raw)
        if m:
            return m.group(0)
        m = re.search(r"\b(19|20)\d{2}-\d{2}\b", raw)
        if m:
            return m.group(0)
        m = re.search(r"\b(19|20)\d{2}\b", raw)
        return m.group(0) if m else ""

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in items:
            k = x.lower()
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    # ------------------------------------------------------------------
    # Record mapping
    # ------------------------------------------------------------------

    def _build_paper(self, doc: dict) -> dict | None:
        docid = self._first(doc, "docid")
        hal_id = self._first(doc, "halId_s")
        version = self._first(doc, "version_i")

        external_id = docid or (f"{hal_id}v{version}" if hal_id and version else hal_id)
        if not external_id:
            print(f"[{self.site_id}] skipping item: no docid or HAL id")
            return None

        # post_number: docid is numeric on HAL
        post_number = docid if (docid and re.fullmatch(r"\d+", docid)) else external_id

        # Title (prefer multi-language join)
        title_vals = self._dedupe(
            self._values(doc, "title_s")
            + self._values(doc, "en_title_s")
            + self._values(doc, "fr_title_s")
        )
        if not title_vals:
            label = self._first(doc, "label_s")
            title_vals = [label] if label else []
        title = " / ".join(title_vals)
        if not title:
            print(f"[{self.site_id}] skipping {external_id}: missing title")
            return None

        # Abstract
        abstract_vals = self._dedupe(
            self._values(doc, "abstract_s")
            + self._values(doc, "en_abstract_s")
            + self._values(doc, "fr_abstract_s")
        )
        abstract = "\n\n".join(abstract_vals)
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] skipping {external_id}: "
                f"abstract below threshold ({len(abstract)} < {self._MIN_ABSTRACT_CHARS} chars)"
            )
            return None

        # Authors ("; " separated)
        authors = "; ".join(self._dedupe(self._values(doc, "authFullName_s")))

        # Keywords (", " separated; multilingual merged)
        kw_vals = self._dedupe(
            self._values(doc, "keyword_s")
            + self._values(doc, "en_keyword_s")
            + self._values(doc, "fr_keyword_s")
        )
        keywords = ", ".join(kw_vals)

        # Dates
        published_date = self._parse_date(
            self._first(doc, "producedDate_s", "publicationDate_s",
                        "releasedDate_s", "submittedDate_s")
        )
        listed_date = self._parse_date(
            self._first(doc, "submittedDate_s", "releasedDate_s")
        )

        # URLs
        url = self._first(doc, "uri_s")
        pdf_url = self._first(doc, "fileMain_s")

        # Original filename: last segment of fileMain_s if meaningful
        original_filename: str | None = None
        if pdf_url:
            last_seg = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if last_seg and last_seg not in ("document", "file"):
                original_filename = last_seg

        # Publisher / institution (prefer publisher_s; fall back to struct names)
        publisher_vals = self._values(doc, "publisher_s")
        inst_vals = self._dedupe(
            self._values(doc, "instStructName_s")
            + self._values(doc, "labStructName_s")
        )
        publisher = "; ".join(publisher_vals) if publisher_vals else "; ".join(inst_vals[:4])

        # Department (full institution list)
        department = "; ".join(inst_vals)

        # Journal / venue
        journal = self._first(doc, "journalTitle_s", "bookTitle_s", "conferenceTitle_s")

        # DOI
        doi = self._first(doc, "doiId_s")

        # Category
        category = self._first(doc, "docType_s")

        # Metadata: all unmapped raw fields
        metadata = {
            "docid": docid,
            "hal_id": hal_id,
            "version": version,
            "doc_type": category,
            "primary_domain": self._first(doc, "primaryDomain_s"),
            "domains": self._values(doc, "domain_s"),
            "language": self._values(doc, "language_s"),
            "citation": self._first(doc, "citationRef_s"),
            "posted_date": self._first(doc, "submittedDate_s"),        # for adapter
            "submitted_date": self._first(doc, "submittedDate_s"),
            "released_date": self._first(doc, "releasedDate_s"),
            "produced_date": self._first(doc, "producedDate_s"),
            "publication_date": self._first(doc, "publicationDate_s"),
            "journal_raw": journal,
            "book_title": self._first(doc, "bookTitle_s"),
            "conference_title": self._first(doc, "conferenceTitle_s"),
            "laboratories": self._values(doc, "labStructName_s"),
            "institutions": self._values(doc, "instStructName_s"),
            "publisher_raw": publisher_vals,
            "originalFilename": original_filename,
        }

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,      # → listed_date via adapter
            "authors": authors,              # "; " separated — adapter handles it
            "publisher": publisher,          # "; " separated
            "department": department,        # full inst list (adapter also reads this)
            "journal": journal,
            "url": url,                      # → meta_url via adapter
            "pdf_url": pdf_url,
            "keywords": keywords,            # ", " separated — adapter handles it
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        import datetime as _dt

        deadline = _dt.datetime.now() + _dt.timedelta(minutes=25)
        saved = 0
        start = 0
        page = 0
        seen_ids: set[str] = set()
        total: int | None = None
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if _dt.datetime.now() >= deadline:
                print(
                    f"[{self.site_id}] 25-minute budget reached at page {page}. "
                    "Exiting cleanly."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page >= self._MAX_PAGES:
                print(
                    f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. "
                    "Stopping."
                )
                break

            # Rate-limit between page fetches (no per-item delay needed — no detail fetch)
            if page > 0:
                time.sleep(self._delay)

            url = self._list_url(start)
            data = self._fetch_json(url)
            response = ((data or {}).get("response") or {})
            docs = response.get("docs") or []

            if total is None:
                total = response.get("numFound")
                if total is not None:
                    print(f"[{self.site_id}] API total records matching filter: {total}")

            if not docs:
                print(f"[{self.site_id}] No more records at start={start}. Done.")
                break

            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                try:
                    docid = self._first(doc, "docid")

                    # URL deduplication — prevents infinite loops if paginator loops
                    uri = self._first(doc, "uri_s")
                    dedup_key = docid or uri
                    if dedup_key and dedup_key in seen_ids:
                        continue
                    if dedup_key:
                        seen_ids.add(dedup_key)

                    paper = self._build_paper(doc)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_str}: "
                        f"{paper['title'][:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({docid!r}): {exc}; continuing")
                    continue

            start += len(docs)
            if len(docs) < self._PAGE_SIZE:
                print(
                    f"[{self.site_id}] Last page reached "
                    f"(got {len(docs)} < {self._PAGE_SIZE}). Done."
                )
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
