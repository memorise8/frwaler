# -*- coding: utf-8 -*-
"""Crawler for IMT Lucca's institutional repository.

The old EPrints instance (eprints.imtlucca.it) is dead — the hostname no
longer resolves (NXDOMAIN) — IMT Lucca migrated its institutional
repository to CINECA's IRIS (DSpace-CRIS) platform at ``iris.imtlucca.it``
(confirmed via https://library.imtlucca.it/en/open-access). Same platform
as ``iris-unitn-it.py`` in this package: the HTML UI / DSpace REST API
sit behind Cloudflare (403), but the OAI-PMH endpoint (``/oai/request``)
is not challenged and serves the full catalogue.

Metadata prefix ``didl`` (MPEG-21 DIDL) is used because it embeds both the
Dublin-Core block (title/creator/description/date/type/identifier/rights)
and the ORIGINAL-bundle bitstream URL as a ``<Resource
mimeType="application/pdf" ref="...">`` — title, abstract and PDF link all
come from a single harvest pass. Pagination is via OAI ``resumptionToken``
(100 records/page).

Only records that are ``info:eu-repo/semantics/openAccess`` AND carry a PDF
Resource are saved.

PDF DOWNLOAD CAVEAT: the ``/bitstream/...`` host is behind the same
Cloudflare WAF, so ``pdf_url`` is recorded but direct download returns 403
to plain curl / curl_cffi (verified) — same caveat as iris-unitn-it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_NS_OAI = "{http://www.openarchives.org/OAI/2.0/}"
_NS_DC = "{http://purl.org/dc/elements/1.1/}"
_NS_DIDL = "{urn:mpeg:mpeg21:2002:02-DIDL-NS}"


class EprintsImtLuccaItCgiCrawler(BaseCrawler):
    """Crawler for IMT Lucca's IRIS institutional repository (OAI-PMH)."""

    site_id = "eprints-imtlucca-it-cgi"
    site_name = "Custom: eprints-imtlucca-it-cgi"
    base_url = "https://iris.imtlucca.it"
    PUBLISHER = "IMT School for Advanced Studies Lucca"

    OAI_ENDPOINT = base_url + "/oai/request"
    METADATA_PREFIX = "didl"
    MAX_PAGES = 400
    MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    STOP_MARGIN_SECONDS = 120
    BACKOFF_SECONDS = (2, 5, 12)
    MIN_SAVE_ABSTRACT_CHARS = 100
    REQUIRE_OPEN_ACCESS = True
    REQUIRE_PDF = True

    # ------------------------------------------------------------------
    # Network — OAI is Cloudflare-exempt, but curl_cffi impersonation is used
    # for robustness (matches the sibling iris-unitn-it crawler).
    # ------------------------------------------------------------------

    def _fetch(self, url, *, context="request"):
        from curl_cffi import requests as _creq
        last_error = "unknown error"
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                resp = _creq.get(
                    url, impersonate="chrome131", timeout=60, allow_redirects=True,
                    headers={"Accept": "application/xml,text/xml,*/*;q=0.8",
                             "Accept-Language": "en-US,en;q=0.9,it;q=0.8"},
                )
                if resp.status_code == 200 and resp.content:
                    return resp.text
                last_error = f"HTTP {resp.status_code}; {len(resp.content)} bytes"
            except Exception as exc:
                last_error = str(exc)
            if attempt < len(self.BACKOFF_SECONDS):
                print(f"[{self.site_id}] {context} failed attempt "
                      f"{attempt}/{len(self.BACKOFF_SECONDS)}: {last_error}; retrying in {wait}s")
                time.sleep(wait)
        print(f"[{self.site_id}] {context} failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_page(self, resumption_token=None, context="page"):
        if resumption_token:
            from urllib.parse import quote
            url = f"{self.OAI_ENDPOINT}?verb=ListRecords&resumptionToken={quote(resumption_token)}"
        else:
            url = f"{self.OAI_ENDPOINT}?verb=ListRecords&metadataPrefix={self.METADATA_PREFIX}"
        raw = self._fetch(url, context=context)
        if not raw:
            return None, None
        try:
            root = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError as exc:
            print(f"[{self.site_id}] {context} XML parse error: {exc}")
            return None, None
        err = root.find(_NS_OAI + "error")
        if err is not None:
            print(f"[{self.site_id}] {context} OAI error "
                  f"[{err.get('code')}]: {(err.text or '').strip()}")
            return [], None
        lr = root.find(_NS_OAI + "ListRecords")
        if lr is None:
            return [], None
        records = lr.findall(_NS_OAI + "record")
        token_el = lr.find(_NS_OAI + "resumptionToken")
        token = (token_el.text or "").strip() if token_el is not None else ""
        return records, (token or None)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(value):
        if not value:
            return ""
        text = unescape(str(value))
        text = text.replace("\r", "\n")
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    @staticmethod
    def _parse_date(raw):
        if not raw:
            return None
        text = str(raw).strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.match(r"^(\d{4})-(\d{2})$", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"
        m = re.match(r"^(\d{4})$", text)
        if m:
            return f"{m.group(1)}-01-01"
        return None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        from urllib.parse import unquote
        path = urlparse(url).path.rstrip("/")
        tail = unquote(path.rsplit("/", 1)[-1])
        return tail if "." in tail and len(tail) <= 200 else None

    def _dc_values(self, md, tag):
        return [self._clean_text(e.text) for e in md.iter(_NS_DC + tag)
                if e.text and self._clean_text(e.text)]

    def _select_pdf(self, md):
        """Return (pdf_url, filename) for the first application/pdf Resource."""
        for res in md.iter(_NS_DIDL + "Resource"):
            ref = res.get("ref")
            if not ref:
                continue
            mime = (res.get("mimeType") or "").lower()
            if "application/pdf" in mime or ref.lower().split("?")[0].endswith(".pdf"):
                return ref, self._filename_from_url(ref)
        return None, None

    def _record_to_paper(self, record):
        header = record.find(_NS_OAI + "header")
        if header is None or (header.get("status") == "deleted"):
            return None
        oai_id = header.findtext(_NS_OAI + "identifier") or ""
        datestamp = header.findtext(_NS_OAI + "datestamp") or ""
        md = record.find(_NS_OAI + "metadata")
        if md is None:
            return None

        titles = self._dc_values(md, "title")
        title = titles[0] if titles else ""
        if not title:
            return None

        descriptions = self._dc_values(md, "description")
        abstract = "\n\n".join(descriptions)

        rights = self._dc_values(md, "rights")
        rights_blob = " ".join(rights).lower()
        is_open = "openaccess" in rights_blob or "open access" in rights_blob
        if self.REQUIRE_OPEN_ACCESS and not is_open:
            return None

        pdf_url, original_filename = self._select_pdf(md)
        if self.REQUIRE_PDF and not pdf_url:
            return None

        creators = self._dc_values(md, "creator")
        contributors = self._dc_values(md, "contributor")
        authors = "; ".join(dict.fromkeys(creators or contributors)) or None

        dates = self._dc_values(md, "date")
        published_date = self._parse_date(dates[0]) if dates else None
        listed_date = self._parse_date(datestamp)

        types = self._dc_values(md, "type")
        category = None
        for t in types:
            if t.startswith("info:eu-repo/semantics/"):
                category = t.rsplit("/", 1)[-1]
                break
        if category is None and types:
            category = types[0]

        identifiers = self._dc_values(md, "identifier")
        handle_url = None
        doi = None
        for ident in identifiers:
            if "hdl.handle.net" in ident or "/handle/" in ident:
                handle_url = ident
            elif ident.startswith("10.") or "doi.org/" in ident:
                doi = ident.rsplit("doi.org/", 1)[-1] if "doi.org/" in ident else ident
        detail_url = handle_url or (f"{self.base_url}/handle/{oai_id.split(':')[-1]}"
                                    if ":" in oai_id else self.base_url)
        post_number = oai_id.split(":")[-1] if ":" in oai_id else None

        relations = self._dc_values(md, "relation")
        journal = None
        for rel in relations:
            if rel.lower().startswith("journal:"):
                journal = rel.split(":", 1)[1].strip()
                break

        keywords = ", ".join(self._dc_values(md, "subject")) or None

        metadata = {
            "oai_identifier": oai_id,
            "datestamp": datestamp,
            "handle": handle_url,
            "types": types,
            "rights": rights,
            "relations": relations,
            "originalFilename": original_filename,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": post_number,
            "site_id": self.site_id,
            "external_id": post_number or oai_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": self.PUBLISHER,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def _near_time_budget(self, start_time):
        return time.time() - start_time > self.MAX_SECONDS - self.STOP_MARGIN_SECONDS

    def crawl(self, limit=None):
        saved = 0
        seen = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"
        token = None

        for page in range(self.MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if self._near_time_budget(start_time):
                print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                break

            if (page + 1) % 10 == 0:
                print(f"[{self.site_id}] page {page + 1}: saved {saved}/{limit_or_inf}")

            records, token = self._fetch_page(token, context=f"list page {page + 1}")
            if records is None:
                print(f"[{self.site_id}] failed to fetch list page {page + 1}; stopping")
                break
            if not records:
                print(f"[{self.site_id}] page {page + 1}: no records; stopping")
                break

            for record in records:
                if limit is not None and saved >= limit:
                    break
                if self._near_time_budget(start_time):
                    print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                    return saved
                try:
                    paper = self._record_to_paper(record)
                    if paper is None:
                        continue
                    if paper["url"] in seen:
                        continue
                    seen.add(paper["url"])
                    abstract_len = len(paper.get("abstract") or "")
                    if abstract_len < self.MIN_SAVE_ABSTRACT_CHARS:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:90]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] record failed: {exc}")
                    continue

            if not token:
                print(f"[{self.site_id}] no resumptionToken; harvest complete")
                break
        else:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
