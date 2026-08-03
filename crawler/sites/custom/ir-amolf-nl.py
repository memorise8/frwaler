# -*- coding: utf-8 -*-
"""AMOLF Institutional Repository (artudis) crawler.

Starting URL: https://ir.amolf.nl/#facet=type:article|dissertation;facet=open_access:T

Discovery notes:
  - The site runs "artudis" repository software (www.artudis.com). The
    search UI is a Backbone.js single-page app whose state lives in the
    URL hash (``#facet=...;filter=...``) and is submitted to a JSON API.
  - Real list endpoint: ``POST https://ir.amolf.nl/search/query`` with a
    JSON body shaped like the page's embedded ``#initial-query`` script
    (``{"query": {"filters": {...}, "facets": [...], "sort": "auto",
    "from": N}}``). Sending only the two facets we care about causes a
    503 — the server appears to require the full facet-definition list
    that the browser normally echoes back, so we keep the full template
    and just toggle ``filters`` on the ``type``/``open_access`` entries
    and bump ``from`` by the fixed page size (10) each page.
  - Detail pages are plain HTML at ``https://ir.amolf.nl/pub/{id}/`` and
    carry Highwire/Google-Scholar style ``citation_*`` <meta> tags plus
    an "Additional Metadata" table (publisher, journal, organisation,
    keywords, funder, promotor/degree grantor for theses, etc).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

# Absolute import - spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_START_URL = "https://ir.amolf.nl/#facet=type:article|dissertation;facet=open_access:T"
_SEARCH_API = "https://ir.amolf.nl/search/query"
_PAGE_SIZE = 10
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_WAITS = (1, 3, 9)

# Full facet-definition template as embedded in the page's #initial-query
# script. The server 503s on a trimmed-down payload, so we keep every
# field and only mutate "filters" on the facets we care about.
_QUERY_TEMPLATE = {
    "filters": {
        "options": [
            {"field_id": "all", "title": "All Fields"},
            {"field_id": "title", "title": "Title"},
            {"field_id": "author", "title": "Author"},
            {"field_id": "promotor", "title": "Promotor"},
            {"field_id": "editor", "title": "Editor"},
            {"field_id": "affiliation", "title": "Affiliation"},
            {"field_id": "series", "title": "Series"},
            {"field_id": "journal", "title": "Journal"},
            {"field_id": "project", "title": "Project"},
        ],
        "values": [{"field_id": "all", "query": ""}],
    },
    "facets": [
        {"title": "Type", "field_id": "type", "api_only": False, "users_only": False,
         "type": "default", "partOf_start": 0, "max_terms": 100, "max_display_terms": 5,
         "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "Visible", "field_id": "public", "api_only": False, "users_only": False,
         "type": "boolean", "partOf_start": 0, "max_terms": 100, "max_display_terms": 5,
         "sort_by": "term", "sort_order": "descending", "filters": []},
        {"title": "Open Access", "field_id": "open_access", "api_only": False, "users_only": False,
         "type": "boolean", "partOf_start": 0, "max_terms": 100, "max_display_terms": 5,
         "sort_by": "term", "sort_order": "descending", "filters": []},
        {"title": "PDF Attached", "field_id": "has_pdf", "api_only": False, "users_only": True,
         "type": "boolean", "partOf_start": 0, "max_terms": 10, "max_display_terms": 10,
         "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "Year", "field_id": "issued_date", "api_only": False, "users_only": False,
         "type": "year", "partOf_start": 0, "max_terms": 100, "max_display_terms": 25,
         "sort_by": "term", "sort_order": "descending", "filters": []},
        {"title": "Research Group", "field_id": "affiliation_label_partOf", "api_only": False,
         "users_only": False, "type": "partOf", "partOf_start": 0, "max_terms": 100,
         "max_display_terms": 6, "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "Theme", "field_id": "subject_theme_label_partOf", "api_only": False,
         "users_only": False, "type": "default", "partOf_start": 0, "max_terms": 100,
         "max_display_terms": 10, "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "Journals", "field_id": "host_journal_label", "api_only": False, "users_only": True,
         "type": "default", "partOf_start": 0, "max_terms": 100, "max_display_terms": 5,
         "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "Series", "field_id": "host_series_label", "api_only": False, "users_only": False,
         "type": "default", "partOf_start": 0, "max_terms": 100, "max_display_terms": 5,
         "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "Project", "field_id": "host_project_label", "api_only": False, "users_only": False,
         "type": "default", "partOf_start": 0, "max_terms": 1000, "max_display_terms": 10,
         "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "Keyword", "field_id": "subject_keyword_label", "api_only": False, "users_only": False,
         "type": "default", "partOf_start": 0, "max_terms": 100, "max_display_terms": 10,
         "sort_by": "count", "sort_order": "descending", "filters": []},
        {"title": "MSC", "field_id": "subject_msc_label_partOf", "api_only": False, "users_only": False,
         "type": "partOf", "partOf_start": 0, "max_terms": 100, "max_display_terms": 5,
         "sort_by": "count", "sort_order": "descending", "filters": []},
    ],
    "sortings": [
        {"title": "Year (descending)", "id": "year_desc",
         "field": [{"field_id": "issued_date", "order": "descending"}]},
        {"title": "Year (ascending)", "id": "year_asc",
         "field": [{"field_id": "issued_date", "order": "ascending"}]},
        {"title": "Title", "id": "title",
         "field": [{"field_id": "title_label", "order": "ascending"}]},
        {"title": "Score / Year", "id": "score",
         "field": [{"field_id": "_score", "order": "descending"},
                   {"field_id": "issued_date", "order": "descending"}]},
    ],
    "sort": "auto",
}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;")


def _make_soup(raw: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[ir-amolf-nl] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _parse_citation_date(raw: str | None) -> str | None:
    value = _clean_text(raw)
    if not value:
        return None
    match = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", value)
    if match:
        return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
    match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if match:
        return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
    return None


def _dedupe_join(values: list[str], sep: str) -> str | None:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return sep.join(out) if out else None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").split("/")[-1]
    return tail[:240] if tail and "." in tail else None


class IrAmolfNlCrawler(BaseCrawler):
    site_id = "ir-amolf-nl"
    site_name = "Custom: ir-amolf-nl"
    base_url = "https://ir.amolf.nl"

    def _curl(self, url: str, *, json_body: dict | None = None) -> str | None:
        headers = [
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
        ]
        cmd = ["curl", "--tls-max", "1.3", "-skL", "--compressed", "--max-time", "45", *headers]
        if json_body is not None:
            cmd.extend(["-X", "POST", "-H", "Content-Type: application/json", "--data", json.dumps(json_body)])
        cmd.append(url)

        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                if result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"[ir-amolf-nl] empty response attempt {attempt}/3 for {url}: {err}")
            except Exception as exc:
                print(f"[ir-amolf-nl] curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < 3:
                time.sleep(wait)

        print(f"[ir-amolf-nl] fetch failed after 3 attempts: {url}")
        return None

    @staticmethod
    def _build_query_payload(from_offset: int) -> dict:
        payload = json.loads(json.dumps(_QUERY_TEMPLATE))  # deep copy
        for facet in payload["facets"]:
            if facet["field_id"] == "type":
                facet["filters"] = [{"term": "article"}, {"term": "dissertation"}]
            elif facet["field_id"] == "open_access":
                facet["filters"] = [{"term": "T"}]
            else:
                facet["filters"] = []
        payload["from"] = from_offset
        return {"query": payload}

    def _fetch_list_page(self, from_offset: int) -> tuple[list[dict], int | None]:
        raw = self._curl(_SEARCH_API, json_body=self._build_query_payload(from_offset))
        if not raw:
            return [], None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            print(f"[ir-amolf-nl] list page from={from_offset} JSON parse failed: {exc}")
            return [], None

        hits = data.get("hits") or []
        total = data.get("query", {}).get("total")

        items: list[dict] = []
        for hit in hits:
            hit_id = hit.get("id")
            if not hit_id:
                continue
            detail_url = f"{self.base_url}/pub/{hit_id}/"
            items.append(
                {
                    "id": str(hit_id),
                    "detail_url": detail_url,
                    "title": _clean_text(hit.get("title")),
                    "category": hit.get("type"),
                    "issued": hit.get("issued"),
                    "affiliation": hit.get("affiliation"),
                }
            )
        return items, total

    @staticmethod
    def _parse_metadata_table(soup) -> dict[str, list[str]]:
        fields: dict[str, list[str]] = {}
        table = soup.select_one("#publication-metadata table") or soup.select_one(".table")
        if not table:
            return fields
        for row in table.find_all("tr"):
            tds = row.find_all("td", recursive=False)
            if len(tds) < 2:
                continue
            key_cell, value_cell = tds[0], tds[1]
            key = _clean_text(key_cell.get_text(" ", strip=True))
            if not key or key == "Citation":
                continue
            links = value_cell.find_all("a")
            if links:
                values = [_clean_text(a.get_text(" ", strip=True)) for a in links]
            else:
                spans = value_cell.select("span.publication-metadata-value")
                if spans:
                    values = [_clean_text(s.get_text(" ", strip=True)) for s in spans]
                else:
                    values = [_clean_text(value_cell.get_text(" ", strip=True))]
            values = [v for v in values if v]
            if values:
                fields[key] = values
        return fields

    def _parse_detail(self, url: str, list_item: dict) -> dict | None:
        raw = self._curl(url)
        if not raw:
            return None
        soup = _make_soup(raw)
        if soup is None:
            return None

        def meta_all(name: str) -> list[str]:
            return [
                _clean_text(tag.get("content"))
                for tag in soup.find_all("meta", attrs={"name": name})
                if _clean_text(tag.get("content"))
            ]

        def meta_one(name: str) -> str | None:
            values = meta_all(name)
            return values[0] if values else None

        title = meta_one("citation_title") or list_item.get("title")
        if not title:
            print(f"[ir-amolf-nl] item {url} skipped: no title")
            return None

        abstract = meta_one("citation_abstract")
        if not abstract:
            p_abstract = soup.select_one("p.abstract")
            abstract = _clean_text(p_abstract.get_text(" ", strip=True)) if p_abstract else ""
        if len(abstract) < 50:
            print(f"[ir-amolf-nl] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return None

        pub_id = list_item.get("id") or url.rstrip("/").rsplit("/", 1)[-1]
        external_id = str(pub_id)
        post_number = external_id if external_id.isdigit() else None

        date_raw = meta_one("citation_date") or meta_one("citation_publication_date")
        published_date = _parse_citation_date(date_raw) or _parse_citation_date(list_item.get("issued"))
        listed_date = _parse_citation_date(list_item.get("issued")) or published_date

        authors = _dedupe_join(meta_all("citation_author"), "; ")
        doi = meta_one("citation_doi")
        pdf_url = meta_one("citation_pdf_url")
        if not pdf_url:
            dl_link = soup.select_one(".publication-downloads legend a[href]")
            if dl_link:
                pdf_url = dl_link.get("href")
        original_filename = _filename_from_url(pdf_url)

        journal = meta_one("citation_journal_title")
        volume = meta_one("citation_volume")
        issue = meta_one("citation_issue")
        issn = meta_one("citation_issn")

        meta_fields = self._parse_metadata_table(soup)
        publisher = "; ".join(meta_fields.get("Publisher", [])) or None
        degree_grantor = "; ".join(meta_fields.get("Degree Grantor", [])) or None
        if not publisher:
            publisher = degree_grantor
        department = "; ".join(meta_fields.get("Organisation", [])) or list_item.get("affiliation")
        if not journal:
            journal = "; ".join(meta_fields.get("Journal", [])) or None
        keywords = _dedupe_join(meta_fields.get("Keywords", []), ", ")

        category = list_item.get("category") or "article"

        metadata = {
            "posted_date": list_item.get("issued"),
            "originalFilename": original_filename,
            "journal_raw": journal,
            "series": "; ".join(meta_fields.get("Series", [])) or None,
            "volume": volume,
            "issue": issue,
            "issn": issn,
            "pub_id": pub_id,
            "funder": "; ".join(meta_fields.get("Funder", [])) or None,
            "promotor": "; ".join(meta_fields.get("Promotor", [])) or None,
            "editor": "; ".join(meta_fields.get("Editor", [])) or None,
            "degree_grantor": degree_grantor,
            "rights": "; ".join(meta_fields.get("Rights", [])) or None,
            "publisher_raw": "; ".join(meta_fields.get("Publisher", [])) or None,
            "affiliation": list_item.get("affiliation"),
            "citation_online_date": meta_one("citation_online_date"),
            "citation_firstpage": meta_one("citation_firstpage"),
            "citation_lastpage": meta_one("citation_lastpage"),
            "source_list_endpoint": "/search/query",
            "source_detail_endpoint": "/pub/{id}/",
        }

        return {
            "id": None,
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
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        from_offset = 0
        page = 1
        seen_urls: set[str] = set()
        started = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= _PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                print(f"[ir-amolf-nl] wall-clock budget nearly reached; stopping cleanly.")
                break
            if page == _PAGE_CAP:
                print(f"[ir-amolf-nl] safety cap of {_PAGE_CAP} pages reached.")

            items, total = self._fetch_list_page(from_offset)
            if not items:
                print(f"[ir-amolf-nl] page {page}: no records found. Done.")
                break

            new_items = []
            for item in items:
                detail_url = item.get("detail_url")
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_items.append(item)

            if not new_items:
                print(f"[ir-amolf-nl] page {page}: all records already seen. Done.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                    print(f"[ir-amolf-nl] wall-clock budget nearly reached; stopping cleanly.")
                    return saved

                detail_url = item.get("detail_url")
                try:
                    time.sleep(self._delay)
                    paper = self._parse_detail(detail_url, item)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[ir-amolf-nl] Saved {saved}/{limit_or_inf}: {paper.get('title', '')[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ir-amolf-nl] item {detail_url} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[ir-amolf-nl] page {page}: saved {saved}/{limit_or_inf}")

            if len(items) < _PAGE_SIZE:
                print(f"[ir-amolf-nl] page {page}: short page ({len(items)} < {_PAGE_SIZE}). Done.")
                break
            if total is not None and from_offset + _PAGE_SIZE >= total:
                print(f"[ir-amolf-nl] page {page}: reached reported total ({total}). Done.")
                break

            from_offset += _PAGE_SIZE
            page += 1

        print(f"[ir-amolf-nl] Done. Total saved: {saved}")
        return saved
