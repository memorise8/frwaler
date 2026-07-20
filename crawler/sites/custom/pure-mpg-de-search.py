# -*- coding: utf-8 -*-
"""Crawler for MPG.Pure institutional repository (pure.mpg.de/search).

API: POST /rest/items/search  (Elasticsearch query, JSON response)
     193,956+ items have full abstracts (metadata.abstracts field).
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://pure.mpg.de"
_SEARCH_API = f"{_BASE_URL}/rest/items/search"
_PAGE_SIZE = 25
_MAX_PAGES = 200
_WALL_BUDGET_SECS = 25 * 60   # 25 minutes
_ABSTRACT_MIN_CHARS = 100      # skip items with shorter abstracts


class PureMpgDeSearchCrawler(BaseCrawler):
    site_id = "pure-mpg-de-search"
    site_name = "Custom: pure-mpg-de-search"
    base_url = "https://pure.mpg.de"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _post_json(self, url: str, payload: dict, *, timeout: int = 30):
        """POST JSON with retry (1 s / 3 s / 9 s backoff). Returns dict or None."""
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                resp = self._session.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                print(
                    f"[{self.site_id}] POST {url} failed"
                    f" (attempt {attempt + 1}/3): {exc}"
                )
                if attempt < 2:
                    time.sleep(waits[attempt])
        return None

    # ------------------------------------------------------------------
    # Field extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_abstract(meta: dict) -> str:
        """Return abstract text. Prefer English; fall back to first entry."""
        abstracts = meta.get("abstracts") or []
        if not isinstance(abstracts, list):
            return ""
        # Two passes: English first, then any language
        for lang_prefix in ("en", None):
            for a in abstracts:
                if not isinstance(a, dict):
                    continue
                if lang_prefix is not None:
                    if not (a.get("language") or "").lower().startswith(lang_prefix):
                        continue
                val = (a.get("value") or "").strip()
                if val:
                    return val
        return ""

    @staticmethod
    def _extract_authors(meta: dict) -> str:
        """Return '; '-joined author names (AUTHOR role only)."""
        names = []
        for c in (meta.get("creators") or []):
            if not isinstance(c, dict) or c.get("role") != "AUTHOR":
                continue
            person = c.get("person") or {}
            given = (person.get("givenName") or "").strip()
            family = (person.get("familyName") or "").strip()
            name = f"{given} {family}".strip()
            if name and name not in names:
                names.append(name)
        return "; ".join(names)

    @staticmethod
    def _extract_publisher(meta: dict) -> str:
        """Return '; '-joined publishers from sources[*].publishingInfo."""
        pubs: list[str] = []
        for s in (meta.get("sources") or []):
            if not isinstance(s, dict):
                continue
            pi = s.get("publishingInfo") or {}
            pub = (pi.get("publisher") or "").strip()
            if pub and pub not in pubs:
                pubs.append(pub)
        return "; ".join(pubs)

    @staticmethod
    def _extract_journal(meta: dict) -> str:
        for s in (meta.get("sources") or []):
            if isinstance(s, dict) and s.get("genre") == "JOURNAL":
                return (s.get("title") or "").strip()
        return ""

    @staticmethod
    def _extract_doi(meta: dict) -> str:
        for ident in (meta.get("identifiers") or []):
            if isinstance(ident, dict) and ident.get("type") == "DOI":
                return (ident.get("id") or "").strip()
        return ""

    @staticmethod
    def _extract_keywords(meta: dict) -> str:
        kw = meta.get("freeKeywords") or ""
        return kw.strip() if isinstance(kw, str) else ""

    @staticmethod
    def _parse_date(raw) -> str:
        """Convert any date-like string to YYYY-MM-DD or YYYY-01-01."""
        if not raw:
            return ""
        raw = str(raw).strip()
        m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if m:
            return m.group(0)
        m = re.search(r"\b(\d{4})\b", raw)
        if m:
            return f"{m.group(1)}-01-01"
        return ""

    @staticmethod
    def _extract_pdf_info(item: dict) -> tuple[str, str]:
        """Return (pdf_url, original_filename).

        Priority:
          1. Internal managed, PDF, PUBLIC visibility
          2. Any file whose mimeType is application/pdf
          3. Any file whose content URL ends in .pdf
        """
        files = item.get("files") or []

        for f in files:
            if not isinstance(f, dict):
                continue
            if (f.get("mimeType") == "application/pdf"
                    and f.get("storage") == "INTERNAL_MANAGED"
                    and f.get("visibility") == "PUBLIC"):
                content = f.get("content", "")
                if content.startswith("/rest/"):
                    return urljoin(_BASE_URL, content), f.get("name", "")

        for f in files:
            if not isinstance(f, dict):
                continue
            content = f.get("content") or ""
            name = f.get("name") or ""
            if f.get("mimeType") == "application/pdf" and content:
                url = urljoin(_BASE_URL, content) if content.startswith("/rest/") else content
                return url, name
            if re.search(r"\.pdf(?:[?#]|$)", content, re.I):
                url = urljoin(_BASE_URL, content) if content.startswith("/rest/") else content
                return url, name

        return "", ""

    @staticmethod
    def _numeric_post_number(object_id: str):
        m = re.search(r"item_(\d+)", object_id)
        return m.group(1) if m else None

    @staticmethod
    def _source_metadata(meta: dict) -> dict:
        """Extract journal/series/volume/issue for the metadata blob."""
        result: dict = {}
        for s in (meta.get("sources") or []):
            if not isinstance(s, dict):
                continue
            genre = s.get("genre", "")
            if genre == "JOURNAL" and "journal_raw" not in result:
                result["journal_raw"] = s.get("title", "")
                for k in ("volume", "issue", "startPage", "endPage"):
                    if s.get(k):
                        result[k] = s[k]
            elif genre == "SERIES" and "series" not in result:
                result["series"] = s.get("title", "")
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_ids: set[str] = set()
        start_time = time.time()

        base_query: dict = {
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "metadata.abstracts"}}]
                }
            },
            "sortingKeys": ["lastModificationDate"],
            "sortOrder": "DESCENDING",
            "size": _PAGE_SIZE,
        }

        while True:
            # ---- budget / termination guards ----
            if time.time() - start_time > _WALL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            if page % 10 == 0:
                limit_label = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            # ---- fetch page ----
            payload = {**base_query, "offset": page * _PAGE_SIZE}
            data = self._post_json(_SEARCH_API, payload)
            if data is None:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break

            records = data.get("records") or []
            if not records:
                print(f"[{self.site_id}] page {page}: no records returned; stopping")
                break

            # ---- process records ----
            new_on_page = 0
            for rec in records:
                if limit is not None and saved >= limit:
                    break

                try:
                    item = rec.get("data") or rec
                    object_id = (item.get("objectId") or "").strip()

                    if not object_id or object_id in seen_ids:
                        continue
                    seen_ids.add(object_id)
                    new_on_page += 1

                    meta = item.get("metadata") or {}
                    version = item.get("versionNumber") or 1

                    title = (meta.get("title") or "").strip()
                    if not title:
                        print(f"[{self.site_id}] item {object_id} skipped: no title")
                        continue

                    abstract = self._extract_abstract(meta)
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{self.site_id}] item {object_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    authors = self._extract_authors(meta)
                    publisher = self._extract_publisher(meta)
                    journal = self._extract_journal(meta)
                    doi = self._extract_doi(meta)
                    keywords = self._extract_keywords(meta)
                    category = (meta.get("genre") or "").strip()

                    published_date = self._parse_date(
                        meta.get("datePublishedInPrint")
                        or meta.get("datePublishedOnline")
                        or meta.get("dateCreated")
                    )
                    listed_date = self._parse_date(item.get("creationDate"))

                    detail_url = f"{_BASE_URL}/pubman/item/{object_id}_{version}"
                    pdf_url, original_filename = self._extract_pdf_info(item)
                    post_number = self._numeric_post_number(object_id)

                    src_meta = self._source_metadata(meta)
                    metadata_blob: dict = {
                        "objectId": object_id,
                        "versionNumber": version,
                        "genre": category,
                        "publicState": item.get("publicState", ""),
                        "context": (item.get("context") or {}).get("name", ""),
                        "posted_date": listed_date,
                        "modificationDate": item.get("modificationDate", ""),
                        **src_meta,
                    }
                    if doi:
                        metadata_blob["doi"] = doi
                    if original_filename:
                        metadata_blob["originalFilename"] = original_filename
                    if post_number:
                        metadata_blob["nativeNumericId"] = post_number

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": object_id,
                        "title": title,
                        "abstract": abstract,
                        "authors": authors,
                        "publisher": publisher or None,
                        "journal": journal or None,
                        "published_date": published_date or None,
                        "posted_date": listed_date or None,
                        "url": detail_url,
                        "pdf_url": pdf_url or None,
                        "doi": doi or None,
                        "keywords": keywords or None,
                        "category": category or None,
                        "original_filename": original_filename or None,
                        "metadata": json.dumps(metadata_blob, ensure_ascii=False),
                    })
                    saved += 1
                    limit_label = str(limit) if limit is not None else "∞"
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_label}: "
                        f"{title[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    oid = "?"
                    try:
                        oid = (rec.get("data") or {}).get("objectId", "?")
                    except Exception:
                        pass
                    print(f"[{self.site_id}] item {oid} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                break

            if len(records) < _PAGE_SIZE:
                print(f"[{self.site_id}] page {page}: fewer than {_PAGE_SIZE} records; likely last page")
                if limit is None or saved >= limit:
                    break

            time.sleep(self._delay)
            page += 1

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
