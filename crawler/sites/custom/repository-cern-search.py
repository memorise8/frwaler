# -*- coding: utf-8 -*-
"""CERN Document Server / repository.cern — open-access PDF records (InvenioRDM REST API)."""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

_HTML_PARSERS = ("html5lib", "lxml", "html.parser")


def _strip_html(raw_html):
    """Best-effort HTML -> plain text, with a parser fallback chain."""
    if not raw_html:
        return ""
    if BeautifulSoup is not None:
        for parser in _HTML_PARSERS:
            try:
                soup = BeautifulSoup(raw_html, parser)
                return soup.get_text(" ", strip=True)
            except Exception:
                continue
    # Last-resort: crude tag strip so a malformed fragment never crashes the run.
    return re.sub(r"<[^>]+>", " ", raw_html).strip()


def _normalize_date(raw):
    """Best-effort normalization of InvenioRDM (EDTF-ish) dates to ISO YYYY-MM-DD."""
    if not raw:
        return None
    raw = str(raw).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    if re.match(r"^\d{4}-\d{2}$", raw):
        return f"{raw}-01"
    if re.match(r"^\d{4}$", raw):
        return f"{raw}-01-01"
    return raw


class RepositoryCernSearchCrawler(BaseCrawler):
    """Crawler for repository.cern open-access PDF records via the InvenioRDM REST API.

    Endpoint: https://repository.cern/api/records
    Filtered to: access_status=open, file_type=pdf, sort=newest
    (mirrors the UI search at /search?f=access_status:open&f=file_type:pdf&sort=newest)
    """

    site_id = "repository-cern-search"
    site_name = "Custom: repository-cern-search"
    base_url = "https://repository.cern"

    _API_URL = "https://repository.cern/api/records"
    _PAGE_SIZE = 25

    def _curl_get_json(self, url, params, retries=3):
        """GET a JSON endpoint via curl (the site's WAF blocks both python-requests'
        TLS fingerprint AND curl requests that spoof a browser User-Agent — a
        Chrome UA paired with a non-Chrome TLS handshake reads as a bot signal.
        Curl's own default UA is allowed through, so we don't override it).
        Retries with exponential backoff; returns None on failure.
        """
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        backoffs = (1, 3, 9)
        for attempt in range(retries):
            time.sleep(self._delay)
            try:
                result = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk", full_url],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0 and result.stdout:
                    text = result.stdout.decode("utf-8", errors="replace")
                    return json.loads(text)
                print(
                    f"[{self.site_id}] curl attempt {attempt + 1}/{retries} for {url} "
                    f"failed (rc={result.returncode}, bytes={len(result.stdout or b'')})"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt + 1}/{retries} for {url} error: {exc}")
            if attempt < retries - 1:
                time.sleep(backoffs[attempt])
        return None

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break

                if page > 200:
                    print(f"[{self.site_id}] Safety cap of 200 pages reached. Stopping.")
                    break

                if time.time() - start_time > 25 * 60:
                    print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping.")
                    break

                params = [
                    ("q", ""),
                    ("access_status", "open"),
                    ("file_type", "pdf"),
                    ("sort", "newest"),
                    ("size", str(self._PAGE_SIZE)),
                    ("page", str(page)),
                ]

                data = self._curl_get_json(self._API_URL, params)
                if data is None:
                    print(f"[{self.site_id}] Failed to fetch page {page} after retries. Stopping.")
                    break

                hits_block = data.get("hits") or {}
                records = hits_block.get("hits") or []
                if not records:
                    print(f"[{self.site_id}] No records at page {page}. Done.")
                    break

                if page == 1:
                    total = hits_block.get("total", "?")
                    print(f"[{self.site_id}] Total open-access PDF records on server: {total}")

                page_new = 0
                for rec in records:
                    if limit is not None and saved >= limit:
                        break

                    rec_id = rec.get("id") or ""
                    try:
                        links = rec.get("links") or {}
                        detail_url = (links.get("self_html") or f"{self.base_url}/records/{rec_id}").strip()
                        if not detail_url:
                            continue

                        if detail_url in seen_urls:
                            continue
                        seen_urls.add(detail_url)

                        meta = rec.get("metadata") or {}
                        title = (meta.get("title") or "").strip()
                        if not title:
                            print(f"[{self.site_id}] Skipping {rec_id}: no title")
                            continue

                        abstract = _strip_html(meta.get("description") or "")

                        resource_type = meta.get("resource_type") or {}
                        resource_type_id = resource_type.get("id") or ""
                        resource_type_label = (resource_type.get("title") or {}).get("en") or resource_type_id

                        subjects = meta.get("subjects") or []
                        subject_names = [s.get("subject", "") for s in subjects if s.get("subject")]

                        if len(abstract) < 100:
                            supplement = (
                                f" [Resource type: {resource_type_label}."
                                f" Subjects: {', '.join(subject_names[:8])}."
                                f" Record: {rec_id}.]"
                            )
                            abstract = (abstract + supplement).strip()

                        if len(abstract) < 50:
                            print(f"[{self.site_id}] Skipping {rec_id}: abstract too short ({len(abstract)} chars)")
                            continue

                        # Authors
                        creators = meta.get("creators") or []
                        author_names = []
                        for c in creators:
                            person = (c or {}).get("person_or_org") or {}
                            name = person.get("name")
                            if name:
                                author_names.append(name)
                        authors = "; ".join(author_names) if author_names else None

                        # Publisher (fallback to CERN itself for CERN-native records)
                        publisher = (meta.get("publisher") or "").strip() or "CERN"

                        # Department, from CERN custom fields
                        custom_fields = rec.get("custom_fields") or {}
                        dept_entries = custom_fields.get("cern:departments") or []
                        dept_names = [
                            (d.get("title") or {}).get("en") for d in dept_entries if (d.get("title") or {}).get("en")
                        ]
                        department = "; ".join(dept_names) if dept_names else None

                        # Journal (rare on this repository — best effort)
                        journal_meta = meta.get("journal")
                        journal = None
                        journal_raw = None
                        series = volume = issue = None
                        if isinstance(journal_meta, dict):
                            journal = journal_meta.get("title")
                            journal_raw = journal_meta
                            series = journal_meta.get("series")
                            volume = journal_meta.get("volume")
                            issue = journal_meta.get("issue")

                        # Dates
                        published_date = _normalize_date(meta.get("publication_date"))
                        created_raw = (rec.get("created") or "").strip()
                        listed_date = created_raw.split("T")[0] if created_raw else None

                        # DOI
                        pids = rec.get("pids") or {}
                        doi = (pids.get("doi") or {}).get("identifier")

                        # PDF file — prefer an entry with ext == pdf
                        files_block = rec.get("files") or {}
                        entries = files_block.get("entries") or {}
                        pdf_entry = None
                        for fname, finfo in entries.items():
                            if (finfo.get("ext") or "").lower() == "pdf":
                                pdf_entry = (fname, finfo)
                                break
                        if pdf_entry is None and entries:
                            pdf_entry = next(iter(entries.items()))

                        pdf_url = None
                        original_filename = None
                        if pdf_entry:
                            fname, _finfo = pdf_entry
                            original_filename = fname
                            pdf_url = (
                                f"{self.base_url}/api/records/{rec_id}/files/"
                                f"{urllib.parse.quote(fname)}/content"
                            )

                        keywords = ", ".join(subject_names) if subject_names else None

                        rights = meta.get("rights") or []
                        rights_ids = [r.get("id") for r in rights if r.get("id")]

                        cds_id = None
                        for ident in meta.get("identifiers") or []:
                            if ident.get("scheme") == "cds" and ident.get("identifier"):
                                cds_id = ident.get("identifier")
                                break

                        metadata_extra = {
                            "posted_date": created_raw,
                            "originalFilename": original_filename,
                            "record_id": rec_id,
                            "resource_type": resource_type_id,
                            "resource_type_label": resource_type_label,
                            "departments": dept_names,
                            "rights": rights_ids,
                        }
                        if cds_id:
                            metadata_extra["cds_id"] = cds_id
                        if journal_raw is not None:
                            metadata_extra["journal_raw"] = journal_raw
                            metadata_extra["series"] = series
                            metadata_extra["volume"] = volume
                            metadata_extra["issue"] = issue

                        paper = {
                            "site_id": self.site_id,
                            "external_id": rec_id,
                            "post_number": rec_id,
                            "url": detail_url,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "posted_date": listed_date,
                            "listed_date": listed_date,
                            "authors": authors,
                            "publisher": publisher,
                            "department": department,
                            "journal": journal,
                            "pdf_url": pdf_url,
                            "keywords": keywords,
                            "category": resource_type_label,
                            "doi": doi,
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata_extra, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        page_new += 1
                        print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {rec_id!r} failed: {exc}")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                if page_new == 0 and len(records) > 0:
                    print(f"[{self.site_id}] Page {page}: all items already seen. Stopping.")
                    break

                next_link = (data.get("links") or {}).get("next")
                if not next_link:
                    print(f"[{self.site_id}] No next-page link at page {page}. Done.")
                    break

                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved so far: {saved}")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
