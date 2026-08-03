# -*- coding: utf-8 -*-
"""Observatorio Chileno de Ciencia, Tecnologia y Conocimiento (observa.minciencia.gob.cl) crawler.

Starting URL: https://observa.minciencia.gob.cl/

The site is a Vue SPA backed by a Django API (api.observa.minciencia.gob.cl)
that mirrors a DSpace 7 repository. The "Estudios y publicaciones" section is
served by two endpoints on that API:

- ``GET /api/estudios/``          -> flat JSON array of every study (curated
  list, no pagination; ~239 records at time of writing) with title/abstract/
  dates/etc under ``datos.metadata`` (Dublin Core fields).
- ``GET /api/estudios/{slug}/``   -> per-item detail, adds the DSpace
  ``handle``, ``uuid`` and ``archivos`` (attached bitstreams: uuid + name),
  which are required to build a working PDF download URL:
  ``https://api.observa.minciencia.gob.cl/api/datosabiertos/download/?uuid=...&filename=...``

The list endpoint is fetched once; detail endpoints are fetched per-item
(rate limited) only for items whose list-level abstract is long enough to be
worth saving, since almost half of the entries have no abstract at all.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


def _strip_html(raw) -> str:
    """Strip HTML tags/entities from a Dublin-Core style value and collapse whitespace."""
    if raw is None:
        return ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    text = str(raw)
    if not text.strip():
        return ""
    if _BS is not None and "<" in text:
        try:
            soup = _make_soup(text)
            text = soup.get_text(" ")
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_DATE_YMD = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_YM = re.compile(r"^(\d{4})-(\d{2})$")
_DATE_Y = re.compile(r"^(\d{4})$")


def _normalize_date(raw):
    if raw is None:
        return None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    m = _DATE_YMD.match(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DATE_YM.match(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = _DATE_Y.match(text)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _first_str(raw):
    if raw is None:
        return None
    if isinstance(raw, list):
        return str(raw[0]) if raw else None
    return str(raw)


def _split_terms(raw):
    """Split a Dublin-Core value (str or list, possibly ';'/',' delimited) into terms."""
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    terms = []
    for v in values:
        if not v:
            continue
        for part in re.split(r"[;,]", str(v)):
            part = part.strip()
            if part and part not in terms:
                terms.append(part)
    return terms


class ObservaMincienciaGobClCrawler(BaseCrawler):
    """Crawler for observa.minciencia.gob.cl (Estudios y publicaciones)."""

    site_id = "observa-minciencia-gob-cl"
    site_name = "Custom: observa-minciencia-gob-cl"
    base_url = "https://observa.minciencia.gob.cl"

    _API_BASE = "https://api.observa.minciencia.gob.cl"
    _LIST_URL = f"{_API_BASE}/api/estudios/"
    _DETAIL_URL_FMT = f"{_API_BASE}/api/estudios/{{slug}}/"
    _DOWNLOAD_URL_FMT = f"{_API_BASE}/api/datosabiertos/download/?uuid={{uuid}}&filename={{filename}}"

    _MIN_ABSTRACT = 100
    _PAGE_SIZE = 20
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get_json(self, url: str):
        """GET ``url`` via curl with exponential-backoff retries; returns parsed JSON or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    try:
                        data = json.loads(text)
                        if data is not None:
                            return data
                        print(f"[{self.site_id}] null JSON body for {url}")
                    except (ValueError, TypeError) as exc:
                        print(f"[{self.site_id}] JSON parse error for {url}: {exc}")
                else:
                    print(f"[{self.site_id}] empty response for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error ({exc}) for {url}")
            if attempt < 2:
                wait = waits[attempt]
                print(f"[{self.site_id}] retry {attempt + 1}/3 in {wait}s: {url}")
                time.sleep(wait)
        print(f"[{self.site_id}] giving up after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # Item processing
    # ------------------------------------------------------------------

    def _pick_pdf(self, archivos):
        if not archivos:
            return None, None
        chosen = None
        for a in archivos:
            if "pdf" in (a.get("mimeType") or "").lower():
                chosen = a
                break
        if chosen is None:
            chosen = archivos[0]
        name = chosen.get("name")
        uuid_ = chosen.get("uuid")
        if not name or not uuid_:
            return None, name
        pdf_url = self._DOWNLOAD_URL_FMT.format(
            uuid=urllib.parse.quote(uuid_, safe=""),
            filename=urllib.parse.quote(name, safe=""),
        )
        return pdf_url, name

    def _process_item(self, item, url, slug):
        meta = (item.get("datos") or {}).get("metadata") or {}
        datos = item.get("datos") or {}

        abstract = _strip_html(meta.get("dc.description.abstract"))
        if not abstract:
            abstract = _strip_html(meta.get("dc.description"))
        if len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] skip (abstract too short, {len(abstract)} chars): {slug}")
            return False

        title = _strip_html(meta.get("dc.title")) or _strip_html(item.get("title")) or "(untitled)"

        detail = self._curl_get_json(self._DETAIL_URL_FMT.format(slug=urllib.parse.quote(slug, safe="-")))
        time.sleep(self._delay)

        handle = detail.get("handle") if detail else None
        dspace_uuid = detail.get("uuid") if detail else None
        archivos = detail.get("archivos") if detail else None
        cms_id = item.get("id")

        if handle and "/" in handle:
            post_number = handle.rsplit("/", 1)[-1]
        elif cms_id is not None:
            post_number = str(cms_id)
        else:
            post_number = dspace_uuid

        # NOTE: _save_paper()/paper_to_document() only reads paper["external_id"]
        # (post_number is derived from it downstream) so both must carry the
        # same value for incremental MAX(post_number) tracking to work.
        external_id = post_number

        pdf_url, original_filename = self._pick_pdf(archivos)

        listed_date_raw = meta.get("dc.date.updated") or meta.get("dc.date.available")
        published_date_raw = meta.get("dc.date.issued") or meta.get("dc.date.available") or meta.get("dc.coverage.temporal")

        authors_terms = _split_terms(meta.get("dc.contributor.author") or meta.get("dc.creator"))
        publisher_terms = _split_terms(meta.get("dc.publisher"))
        keyword_terms = _split_terms(meta.get("dc.subject"))

        doi = None
        for k, v in meta.items():
            if "doi" in k.lower():
                doi = _first_str(v)
                break

        metadata = {
            "posted_date": listed_date_raw,
            "originalFilename": original_filename,
            "cms_id": cms_id,
            "handle": handle,
            "dspace_uuid": dspace_uuid,
            "dc_type_raw": meta.get("dc.type"),
            "categorias_nombres": datos.get("categorias_nombres"),
            "tipo_documento": datos.get("tipo_documento"),
            "ano_referencia": datos.get("ano_referencia"),
            "collection": detail.get("collection") if detail else None,
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}

        listed_date = _normalize_date(listed_date_raw)

        paper = {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": _normalize_date(published_date_raw),
            "listed_date": listed_date,
            # paper_to_document() only reads paper["posted_date"] (falling back to
            # metadata["posted_date"] otherwise, which is the *raw* string) to
            # derive documents.listed_date — so the normalized ISO date must be
            # duplicated here too, or the DB ends up with the unparsed raw value.
            "posted_date": listed_date,
            "authors": ";".join(authors_terms) if authors_terms else None,
            "publisher": ";".join(publisher_terms) if publisher_terms else None,
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": ",".join(keyword_terms) if keyword_terms else None,
            "category": _first_str(meta.get("dc.type")),
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        limit_display = limit if limit is not None else "inf"

        all_items = self._curl_get_json(self._LIST_URL)
        if not isinstance(all_items, list) or not all_items:
            print(f"[{self.site_id}] failed to fetch estudios list, aborting")
            return 0

        saved = 0
        seen_urls = set()
        total_pages = max(1, (len(all_items) + self._PAGE_SIZE - 1) // self._PAGE_SIZE)

        try:
            for page in range(total_pages):
                if page >= self._MAX_PAGES:
                    print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages, stopping")
                    break

                elapsed = time.time() - start_time
                if elapsed > self._MAX_WALL:
                    print(f"[{self.site_id}] approaching wall-clock budget ({elapsed:.0f}s elapsed), stopping cleanly")
                    break

                if limit is not None and saved >= limit:
                    break

                chunk = all_items[page * self._PAGE_SIZE:(page + 1) * self._PAGE_SIZE]
                if not chunk:
                    break

                new_count = 0
                for item in chunk:
                    slug = item.get("nombre")
                    if not slug:
                        continue
                    url = f"{self.base_url}/estudios/{slug}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_count += 1

                    try:
                        if self._process_item(item, url, slug):
                            saved += 1
                    except Exception as exc:
                        print(f"[{self.site_id}] item {slug} failed: {exc}")
                        continue

                    if limit is not None and saved >= limit:
                        break

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

                if new_count == 0:
                    break
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done: saved {saved} items")
        return saved
