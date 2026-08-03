# -*- coding: utf-8 -*-
"""Crawler for DSpace at Griffith College title browse.

Starting URL:
    https://go.griffith.ie/browse/title

The Angular browse page is backed by DSpace 7 REST:
    /server/api/discover/browses/title/items
PDFs are exposed through each item's ORIGINAL bundle bitstreams.
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


class GoGriffithIeBrowseCrawler(BaseCrawler):
    site_id = "go-griffith-ie-browse"
    site_name = "Custom: go-griffith-ie-browse"
    base_url = "https://go.griffith.ie"

    _API_BASE = "https://go.griffith.ie/server/api"
    _LIST_API = f"{_API_BASE}/discover/browses/title/items"
    _PAGE_SIZE = 20
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 50
    _BACKOFFS = (1, 3, 9)

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        if limit is not None and limit <= 0:
            return 0

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                break
            if self._time_budget_near(start_time):
                print(f"[{self.site_id}] 25-minute budget approaching; exiting cleanly")
                break

            display_page = page + 1
            if display_page == 1 or display_page % 10 == 0:
                print(f"[{self.site_id}] page {display_page}: saved {saved}/{limit_or_inf}")

            list_url = f"{self._LIST_API}?size={self._PAGE_SIZE}&page={page}"
            data = self._curl_json(list_url, context=f"list page {display_page}")
            if data is None:
                print(f"[{self.site_id}] list page {display_page} failed; stopping")
                break

            items = (((data.get("_embedded") or {}).get("items")) or [])
            if not items:
                print(f"[{self.site_id}] page {display_page}: no records; done")
                break

            new_urls_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_near(start_time):
                    print(f"[{self.site_id}] 25-minute budget approaching; exiting cleanly")
                    return saved

                item_id = _clean(item.get("uuid") or item.get("id") or item.get("handle") or "?")
                try:
                    detail_url = self._detail_url(item)
                    if not detail_url:
                        print(f"[{self.site_id}] item {item_id} skipped: missing detail URL")
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_urls_on_page += 1

                    ok = self._process_item(item, detail_url, display_page)
                    if ok:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {display_page}: 0 new records; done")
                break

            links = data.get("_links") or {}
            page_info = data.get("page") or {}
            has_next = bool((links.get("next") or {}).get("href"))
            if not has_next:
                try:
                    has_next = int(page_info.get("number", page)) + 1 < int(page_info.get("totalPages", 0))
                except Exception:
                    has_next = False
            if not has_next:
                print(f"[{self.site_id}] no next page after page {display_page}; done")
                break

            page += 1

        print(f"[{self.site_id}] done. total saved={saved}")
        return saved

    def _process_item(self, item, detail_url, list_page):
        metadata = item.get("metadata") or {}
        uuid_val = _clean(item.get("uuid") or item.get("id"))
        if not uuid_val:
            return False

        title = _meta_first(metadata, "dc.title") or _clean(item.get("name"))
        if not title:
            return False

        abstract = _meta_first(
            metadata,
            "dc.description.abstract",
            "dcterms.abstract",
            "dc.description",
        )
        abstract = _normalize_ws(abstract)
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skip {uuid_val}: abstract {len(abstract)} chars < {self._MIN_ABSTRACT_CHARS}")
            return False

        time.sleep(self._delay)
        bundles_url = f"{self._API_BASE}/core/items/{uuid_val}/bundles?embed=bitstreams"
        bundles_data = self._curl_json(bundles_url, context=f"item {uuid_val} bundles")
        if bundles_data is None:
            raise RuntimeError("bundle detail fetch failed after retries")

        pdf_url, original_filename, pdf_bitstream = self._extract_pdf(bundles_data)
        original_filename = original_filename or _filename_from_url(pdf_url)

        handle = _clean(item.get("handle"))
        post_number = _post_number(handle, uuid_val)
        external_id = uuid_val

        published_raw = _meta_first(
            metadata,
            "dc.date.issued",
            "dc.date.created",
            "dcterms.issued",
        )
        listed_raw = _meta_first(
            metadata,
            "dc.date.accessioned",
            "dc.date.available",
            "dcterms.available",
        ) or _clean(item.get("lastModified"))
        published_date = _iso_date(published_raw)
        listed_date = _iso_date(listed_raw)

        authors = _join_unique(
            _meta_values(metadata, "dc.contributor.author")
            + _meta_values(metadata, "dc.creator"),
            "; ",
        )
        publisher = _join_unique(
            _meta_values(metadata, "dc.publisher")
            + _meta_values(metadata, "dc.contributor.institution"),
            "; ",
        )
        department = _join_unique(
            _meta_values(metadata, "dc.contributor.department")
            + _meta_values(metadata, "dc.description.department")
            + _meta_values(metadata, "dc.subject.department"),
            "; ",
        )
        keywords = _join_unique(
            _meta_values(metadata, "dc.subject")
            + _meta_values(metadata, "dc.subject.other")
            + _meta_values(metadata, "dc.subject.keyword"),
            ", ",
        )
        category = _join_unique(_meta_values(metadata, "dc.type"), ", ")
        doi = _meta_first(metadata, "dc.identifier.doi", "dc.identifier.uri.doi")
        journal_raw = _meta_first(
            metadata,
            "dc.source",
            "dc.relation.ispartof",
            "dc.relation.ispartofjournal",
            "citation_journal_title",
        )
        series = _meta_first(metadata, "dc.relation.ispartofseries", "dc.description.series")
        volume = _meta_first(metadata, "dc.citation.volume", "prism.volume")
        issue = _meta_first(metadata, "dc.citation.issue", "prism.issue")
        journal = journal_raw or series

        raw_metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "node_id": uuid_val,
            "uuid": uuid_val,
            "handle": handle,
            "post_number": post_number,
            "source_start_url": f"{self.base_url}/browse/title",
            "list_api": self._LIST_API,
            "list_page": list_page,
            "detail_api": bundles_url,
            "published_date_raw": published_raw,
            "listed_date_raw": listed_raw,
            "lastModified": item.get("lastModified"),
            "inArchive": item.get("inArchive"),
            "discoverable": item.get("discoverable"),
            "withdrawn": item.get("withdrawn"),
            "entityType": item.get("entityType"),
            "handle_url": f"{self.base_url}/handle/{handle}" if handle else None,
            "legacy_identifier_uri": _meta_first(metadata, "dc.identifier.uri"),
            "metadata": metadata,
            "pdf_bitstream": pdf_bitstream,
        }

        paper = {
            "id": uuid_val,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(raw_metadata, ensure_ascii=False, sort_keys=True),
        }
        saved_id = self._save_paper(paper)
        self._patch_libertree_post_number(saved_id, post_number)
        return True

    def _curl_json(self, url, context="request", timeout=45):
        raw = self._curl(url, accept="application/json", context=context, timeout=timeout)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            text = raw.decode("utf-8", errors="replace")
            soup = _make_soup(text)
            title = ""
            if soup is not None and soup.find("title"):
                title = _clean(soup.find("title").get_text(" "))
            print(f"[{self.site_id}] {context} JSON parse failed: {exc}; html_title={title!r}")
            return None

    def _curl(self, url, accept="*/*", context="request", timeout=45):
        last_error = None
        for attempt, wait_seconds in enumerate(self._BACKOFFS, start=1):
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-skL",
                        "--fail",
                        "--max-time",
                        str(timeout),
                        "-A",
                        self.USER_AGENT,
                        "-H",
                        f"Accept: {accept}",
                        url,
                    ],
                    capture_output=True,
                    timeout=timeout + 10,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self._BACKOFFS):
                print(f"[{self.site_id}] {context} fetch attempt {attempt}/3 failed: {last_error}; retrying in {wait_seconds}s")
                time.sleep(wait_seconds)

        print(f"[{self.site_id}] {context} fetch failed after 3 attempts: {last_error}")
        return None

    def _extract_pdf(self, bundles_data):
        bundles = (((bundles_data or {}).get("_embedded") or {}).get("bundles") or [])
        candidates = []
        for bundle in bundles:
            bundle_name = _clean(bundle.get("name")).upper()
            embedded = bundle.get("_embedded") or {}
            bitstreams = (
                (((embedded.get("bitstreams") or {}).get("_embedded") or {}).get("bitstreams"))
                or []
            )
            for bitstream in bitstreams:
                name = _clean(bitstream.get("name"))
                content_url = (((bitstream.get("_links") or {}).get("content") or {}).get("href"))
                uuid_val = _clean(bitstream.get("uuid") or bitstream.get("id"))
                if not content_url and uuid_val:
                    content_url = f"{self._API_BASE}/core/bitstreams/{uuid_val}/content"
                source_name = _meta_first(bitstream.get("metadata") or {}, "dc.source", "dc.title")
                filename = name or source_name or _filename_from_url(content_url)
                is_pdf = (
                    bundle_name == "ORIGINAL"
                    and content_url
                    and (
                        filename.lower().endswith(".pdf")
                        or "pdf" in _clean(bitstream.get("bundleName")).lower()
                    )
                )
                if is_pdf:
                    candidates.append((content_url, filename, bitstream))
        if not candidates:
            return None, None, None
        return candidates[0]

    def _detail_url(self, item):
        uuid_val = _clean(item.get("uuid") or item.get("id"))
        if uuid_val:
            return f"{self.base_url}/items/{uuid_val}"
        handle = _clean(item.get("handle"))
        if handle:
            return f"{self.base_url}/handle/{handle}"
        return None

    def _time_budget_near(self, start_time):
        return time.time() - start_time >= self._MAX_SECONDS - 5

    def _patch_libertree_post_number(self, saved_id, post_number):
        if not saved_id or not post_number:
            return
        try:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(documents)").fetchall()}
            if "seq_id" not in columns or "post_number" not in columns:
                return
            self._conn.execute(
                "UPDATE documents SET post_number = ? WHERE seq_id = ?",
                (post_number, saved_id),
            )
            self._conn.commit()
        except Exception as exc:
            print(f"[{self.site_id}] post_number patch failed for {saved_id}: {exc}")


def _meta_values(metadata, *keys):
    values = []
    for key in keys:
        for entry in metadata.get(key, []) or []:
            value = entry.get("value") if isinstance(entry, dict) else entry
            value = _normalize_ws(value)
            if value:
                values.append(value)
    return values


def _meta_first(metadata, *keys):
    values = _meta_values(metadata, *keys)
    return values[0] if values else None


def _join_unique(values, delimiter):
    seen = set()
    out = []
    for value in values:
        value = _normalize_ws(value)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return delimiter.join(out) if out else None


def _post_number(handle, fallback):
    if handle:
        tail = handle.rstrip("/").split("/")[-1]
        if tail:
            return tail if tail.isdigit() else tail
    return fallback or None


def _iso_date(raw):
    raw = _clean(raw)
    if not raw:
        return None
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        return "-".join(match.groups())
    match = re.search(r"(\d{4})-(\d{2})", raw)
    if match:
        year, month = match.groups()
        return f"{year}-{month}-01"
    match = re.search(r"(\d{4})", raw)
    if match:
        return f"{match.group(1)}-01-01"
    return None


def _filename_from_url(url):
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").split("/")[-1])
    if "." in tail and len(tail) <= 240:
        return tail
    return None


def _normalize_ws(value):
    value = _clean(value)
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _make_soup(raw_html):
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw_html or "", parser)
        except Exception:
            continue
    return None
