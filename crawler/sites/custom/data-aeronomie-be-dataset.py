# -*- coding: utf-8 -*-
"""data.aeronomie.be CKAN PDF dataset crawler (BIRA-IASB).

Target: https://data.aeronomie.be/dataset/?res_format=PDF
API:    https://data.aeronomie.be/api/3/action/package_search?fq=res_format:PDF
Total:  ~32 datasets (CKAN 2.11.4)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class DataAeronomieBeDatasetCrawler(BaseCrawler):
    site_id = "data-aeronomie-be-dataset"
    site_name = "Custom: data-aeronomie-be-dataset"
    base_url = "https://data.aeronomie.be"

    LIST_API = "https://data.aeronomie.be/api/3/action/package_search"
    FILTER_QUERY = "res_format:PDF"
    DETAIL_PAGE = "https://data.aeronomie.be/dataset/{name}"
    PAGE_SIZE = 20
    MAX_PAGES = 200
    MIN_ABSTRACT_CHARS = 50
    _NETWORK_FAILED = object()

    def crawl(self, limit=None):
        saved = 0
        start = 0
        page = 1
        seen_urls = set()
        t0 = time.time()
        limit_label = str(limit) if limit is not None else "inf"

        while True:
            if time.time() - t0 > 25 * 60:
                print(f"[{self.site_id}] 25-minute budget reached; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page > self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

            data = self._fetch_list(start)
            if data is self._NETWORK_FAILED or data is None:
                print(f"[{self.site_id}] list API failed at page {page}; stopping")
                break

            result = data.get("result") or {}
            records = result.get("results") or []
            total = result.get("count") or 0

            if not records:
                print(f"[{self.site_id}] no records at page {page}; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            for record in records:
                if limit is not None and saved >= limit:
                    break

                try:
                    pkg_id = (record.get("id") or record.get("name") or "").strip()
                    if not pkg_id:
                        continue

                    name = (record.get("name") or pkg_id).strip()
                    detail_url = self.DETAIL_PAGE.format(name=name)
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    paper = self._parse_record(record)
                    if paper is None:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping {pkg_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_label}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {record.get('id', '?')} failed: {exc}")
                    continue

                time.sleep(self._delay)

            start += len(records)
            page += 1

            if total and start >= int(total):
                break

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def _parse_record(self, item: dict) -> dict | None:
        pkg_id = (item.get("id") or item.get("name") or "").strip()
        if not pkg_id:
            return None

        name = (item.get("name") or pkg_id).strip()
        title = self._clean_text(item.get("title") or name)
        if not title:
            return None

        abstract = self._build_abstract(item)

        resources = item.get("resources") if isinstance(item.get("resources"), list) else []
        pdf_resources = [
            r for r in resources
            if isinstance(r, dict) and (
                (r.get("format") or "").strip().upper() == "PDF"
                or (r.get("mimetype") or "").lower() == "application/pdf"
                or ".pdf" in (r.get("url") or "").lower()
            )
        ]
        pdf_url = (pdf_resources[0].get("url") or "") if pdf_resources else ""
        original_filename = self._filename_from_url(pdf_url)

        authors = self._extract_authors(item)

        org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        publisher = self._clean_text(
            org.get("title") or org.get("name") or item.get("author_entity") or ""
        )

        groups = item.get("groups") if isinstance(item.get("groups"), list) else []
        dept_parts = [
            self._clean_text(g.get("display_name") or g.get("title") or g.get("name") or "")
            for g in groups if isinstance(g, dict)
        ]
        department = "; ".join(p for p in dept_parts if p) or None

        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        keywords_list = [
            self._clean_text(t.get("display_name") or t.get("name") or "")
            for t in tags if isinstance(t, dict)
        ]
        keywords = ", ".join(k for k in keywords_list if k) or None

        published_date = (
            self._normalize_date(item.get("metadata_modified"))
            or self._normalize_date(item.get("metadata_created"))
        )
        listed_date = self._normalize_date(item.get("metadata_created"))

        references = item.get("reference") if isinstance(item.get("reference"), list) else []
        doi = None
        for ref in references:
            if isinstance(ref, str) and "doi.org" in ref.lower():
                doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", ref.strip())
                break

        category_parts = dept_parts or [self._clean_text(item.get("type") or "dataset")]
        category = "; ".join(p for p in category_parts if p) or "dataset"

        detail_url = self.DETAIL_PAGE.format(name=name)

        metadata = {
            "package_id": pkg_id,
            "name": name,
            "state": item.get("state"),
            "type": item.get("type"),
            "license_id": item.get("license_id"),
            "license_title": item.get("license_title"),
            "license_url": item.get("license_url"),
            "metadata_created": item.get("metadata_created"),
            "metadata_modified": item.get("metadata_modified"),
            "temporal_start": item.get("temporal_start"),
            "temporal_end": item.get("temporal_end"),
            "temporal_resolution": item.get("temporal_resolution"),
            "spatial": item.get("spatial"),
            "version": item.get("version"),
            "frequency": item.get("frequency"),
            "funding_detail": item.get("funding_detail"),
            "reference": item.get("reference"),
            "maintainer": item.get("maintainer"),
            "maintainer_email": item.get("maintainer_email"),
            "author_entity": item.get("author_entity"),
            "conforms_to": item.get("conforms_to"),
            "owner_org": item.get("owner_org"),
            "groups": dept_parts,
            "resources": [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "description": r.get("description"),
                    "format": r.get("format"),
                    "url": r.get("url"),
                    "created": r.get("created"),
                    "last_modified": r.get("last_modified"),
                    "size": r.get("size"),
                }
                for r in pdf_resources
            ],
            "posted_date": listed_date,
            "originalFilename": original_filename,
        }

        return {
            "site_id": self.site_id,
            "external_id": pkg_id,
            "post_number": pkg_id,
            "title": title,
            "abstract": abstract,
            "url": detail_url,
            "pdf_url": pdf_url,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "keywords": keywords,
            "published_date": published_date,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "doi": doi,
            "category": category,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _extract_authors(self, item: dict) -> list:
        authors = []
        firstname = self._clean_text(item.get("author_firstname") or "")
        lastname = self._clean_text(item.get("author") or "")
        if lastname:
            full = f"{firstname} {lastname}".strip() if firstname else lastname
            authors.append(full)

        for oc in (item.get("other_creator") or []):
            if not isinstance(oc, dict):
                continue
            fn = self._clean_text(oc.get("firstname") or "")
            ln = self._clean_text(oc.get("lastname") or "")
            if ln:
                full = f"{fn} {ln}".strip() if fn else ln
                if full not in authors:
                    authors.append(full)
        return authors

    def _build_abstract(self, item: dict) -> str:
        parts = []

        notes = self._clean_text(item.get("notes") or "")
        if notes:
            parts.append(notes)

        for r in (item.get("resources") or []):
            if not isinstance(r, dict):
                continue
            desc = self._clean_text(r.get("description") or "")
            if desc and desc not in parts:
                parts.append(desc)

        joined = "\n\n".join(parts)
        if len(joined) < 100:
            org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
            org_desc = self._clean_text(org.get("description") or "")
            if org_desc and org_desc not in parts:
                parts.append(org_desc)

        if len("\n\n".join(parts)) < 100:
            for g in (item.get("groups") or []):
                if not isinstance(g, dict):
                    continue
                gd = self._clean_text(g.get("description") or "")
                if gd and gd not in parts:
                    parts.append(gd)

        if len("\n\n".join(parts)) < 100:
            for ref in (item.get("reference") or []):
                if isinstance(ref, str) and ref.strip():
                    parts.append(f"Reference: {ref.strip()}")

        return "\n\n".join(parts).strip()

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch_list(self, start: int):
        params = {
            "fq": self.FILTER_QUERY,
            "rows": str(self.PAGE_SIZE),
            "start": str(start),
        }
        url = f"{self.LIST_API}?{urlencode(params)}"
        data = self._curl_json(url, context=f"list start={start}")
        if data is self._NETWORK_FAILED:
            return self._NETWORK_FAILED
        if not data or not data.get("success"):
            return None
        return data

    def _curl_json(self, url: str, context: str = "request"):
        raw = self._curl_get(url, context=context)
        if not raw:
            return self._NETWORK_FAILED
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--fail", "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = err or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"
            except Exception as exc:
                last_error = str(exc)

            print(f"[{self.site_id}] {context} curl attempt {attempt + 1}/3 failed: {last_error}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts")
        return None

    def _make_soup(self, raw: str):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_date(value) -> str:
        if not value:
            return ""
        m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", str(value))
        if not m:
            return ""
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        if "." in tail and len(tail) <= 200:
            return tail
        return None

    @staticmethod
    def _clean_text(value) -> str:
        if value is None:
            return ""
        text = str(value)
        text = unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#>]+", " ", text)
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
