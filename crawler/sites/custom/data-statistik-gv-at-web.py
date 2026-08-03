# -*- coding: utf-8 -*-
"""Crawler for Statistik Austria Open.data catalog.

Start page: https://data.statistik.gv.at/web/catalog.jsp#

The catalog is server-rendered HTML. Each row links to:
  - /web/meta.jsp?dataset=<dataset-id>      (human-readable metadata page)
  - /ogd/json?dataset=<dataset-id>          (machine-readable detail API)

Absolute import is required because tests load this file via
spec_from_file_location without package context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_SITE_ID = "data-statistik-gv-at-web"
_BASE_URL = "https://data.statistik.gv.at"
_START_URL = f"{_BASE_URL}/web/catalog.jsp#"
_JSON_API = f"{_BASE_URL}/ogd/json"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_SHORT_ABSTRACT_MIN = 50
_RETRY_WAITS = (1, 3, 9)


class DataStatistikGvAtWebCrawler(BaseCrawler):
    site_id = "data-statistik-gv-at-web"
    site_name = "Custom: data-statistik-gv-at-web"
    base_url = "https://data.statistik.gv.at"

    def crawl(self, limit=None):
        """Crawl Statistik Austria Open.data records and persist them."""
        if limit is not None:
            limit = int(limit)
            if limit <= 0:
                return 0

        limit_or_inf = str(limit) if limit is not None else "inf"
        started_at = time.time()
        saved = 0
        page = 1
        page_url = _START_URL
        seen_urls = set()
        seen_page_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at >= _WALL_BUDGET_SECONDS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Exiting cleanly.")
                break
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            canonical_page_url = page_url.split("#", 1)[0]
            if canonical_page_url in seen_page_urls:
                print(f"[{_SITE_ID}] page {page}: paginator loop detected. Stopping.")
                break
            seen_page_urls.add(canonical_page_url)

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            raw = self._curl_get(page_url, accept="text/html,application/xhtml+xml,*/*;q=0.8")
            if not raw:
                print(f"[{_SITE_ID}] page {page}: empty or failed catalog response. Stopping.")
                break

            items = self._parse_catalog_items(raw, page_url)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no catalog records found. Stopping.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - started_at >= _WALL_BUDGET_SECONDS:
                    print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Exiting cleanly.")
                    return saved

                item_url = item.get("url")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                item_label = item.get("dataset") or item_url
                try:
                    detail = self._fetch_detail(item)
                    if detail is None:
                        print(f"[{_SITE_ID}] item {item_label} failed: detail fetch failed after retries")
                        continue
                    record = self._build_record(item, detail)
                    if record is None:
                        continue

                    abstract = record.get("abstract") or ""
                    if len(abstract) < _SHORT_ABSTRACT_MIN:
                        print(f"[{_SITE_ID}] skip short abstract ({len(abstract)} chars): "
                              f"{record.get('title', '')[:80]}")
                        continue

                    self._save_paper(record)
                    saved += 1

                    if limit is None or saved < limit:
                        time.sleep(max(float(getattr(self, "_delay", 1.0)), 0.0))
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: 0 new records after URL deduplication. Stopping.")
                break

            next_url = self._find_next_page(raw, page_url)
            if not next_url:
                break

            page += 1
            page_url = next_url

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, accept="*/*", retries=3):
        """Fetch a URL with curl, TLS 1.3 cap, retries, and replacement decoding."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            "60",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept}",
            url,
        ]

        for attempt in range(retries):
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=70)
                text = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
                if proc.returncode == 0 and text.strip():
                    return text

                stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
                print(f"[{_SITE_ID}] curl failed (attempt {attempt + 1}/{retries}) "
                      f"for {url}: rc={proc.returncode} {stderr.strip()[:200]}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}) "
                      f"for {url}: {exc}")

            if attempt < retries - 1:
                wait = _RETRY_WAITS[attempt]
                print(f"[{_SITE_ID}] retrying in {wait}s...")
                time.sleep(wait)

        return None

    def _fetch_detail(self, item):
        """Fetch JSON detail API for a catalog item; fallback to metadata HTML."""
        dataset = item.get("dataset")
        if not dataset:
            return {}

        qs = urlencode({"dataset": dataset}, quote_via=quote)
        json_url = f"{_JSON_API}?{qs}"
        raw = self._curl_get(json_url, accept="application/json,text/plain,*/*;q=0.8")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    data["_detail_api_url"] = json_url
                    return data
            except (TypeError, ValueError) as exc:
                print(f"[{_SITE_ID}] JSON parse failed for {dataset}: {exc}; trying HTML detail.")

        html = self._curl_get(item.get("url"), accept="text/html,application/xhtml+xml,*/*;q=0.8")
        if html:
            return self._parse_html_detail(html, item.get("url"), dataset)
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(raw):
        """Create BeautifulSoup with a robust parser fallback chain."""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                continue
        print(f"[{_SITE_ID}] BeautifulSoup parser fallback failed: {last_exc}")
        return BeautifulSoup("", "html.parser")

    def _parse_catalog_items(self, raw, page_url):
        soup = self._make_soup(raw)
        items = []

        for link in soup.select('a[href*="meta.jsp?dataset="]'):
            href = link.get("href") or ""
            detail_url = urljoin(page_url, href)
            dataset = self._dataset_from_url(detail_url)
            if not dataset:
                continue

            row = link.find_parent("tr")
            title = self._clean_text(link.get_text(" "))
            description = None
            listed_date_raw = None
            data_url = None
            json_url = f"{_JSON_API}?{urlencode({'dataset': dataset})}"

            if row is not None:
                cells = row.find_all("td")
                if cells:
                    para = cells[0].find("p")
                    if para is not None:
                        description = self._clean_text(para.get_text(" "))
                    if len(cells) > 1:
                        listed_date_raw = self._clean_text(cells[1].get_text(" "))

                for row_link in row.find_all("a", href=True):
                    row_href = row_link.get("href") or ""
                    abs_url = urljoin(page_url, row_href)
                    lower = abs_url.lower()
                    if "/ogd/json" in lower:
                        json_url = abs_url
                    elif "/data/" in lower and not data_url:
                        data_url = abs_url

            panel = link.find_parent("div", class_=re.compile(r"\bpanel\b"))
            panel_title = None
            if panel is not None:
                heading = panel.find(class_=re.compile(r"\bpanel-title\b"))
                if heading is not None:
                    panel_title = self._clean_text(heading.get_text(" "))

            items.append({
                "dataset": dataset,
                "title": title,
                "description": description,
                "listed_date_raw": listed_date_raw,
                "url": detail_url,
                "json_url": json_url,
                "data_url": data_url,
                "panel_title": panel_title,
            })

        return items

    def _parse_html_detail(self, raw, detail_url, dataset):
        soup = self._make_soup(raw)
        fields = {}
        resources = []

        heading = soup.find("h1")
        if heading is not None:
            heading_text = self._clean_text(heading.get_text(" "))
            heading_text = re.sub(r"\bMetadaten\b", "", heading_text).strip()
            if heading_text:
                fields["title"] = heading_text

        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            label = self._clean_text(cells[0].get_text(" "))
            value = self._clean_text(cells[1].get_text(" "))
            if not label:
                continue
            fields[label] = value

            if "URL" in label:
                link = cells[1].find("a", href=True)
                if link is not None:
                    resource_url = urljoin(detail_url, link.get("href"))
                    resources.append({
                        "url": resource_url,
                        "format": self._filename_from_url(resource_url).rsplit(".", 1)[-1]
                        if self._filename_from_url(resource_url) else None,
                        "name": self._filename_from_url(resource_url),
                    })

        notes = fields.get("Inhaltliche Beschreibung des Datensatzes/Dienstes")
        tags = fields.get("Beschlagwortung des Datensatzes/Dienstes")
        publisher = fields.get("Für den Datensatz/Dienst zuständige Organisation")
        created = fields.get("Datum der erstmaligen Veröffentlichung des Datensatzes/Dienstes")
        modified = fields.get("Datum der letzten Aktualisierung des Datensatzes/Dienstes")

        return {
            "title": fields.get("title"),
            "notes": notes,
            "tags": [p.strip() for p in (tags or "").split(",") if p.strip()],
            "maintainer": publisher,
            "resources": resources,
            "extras": {
                "metadata_original_portal": detail_url,
                "metadata_modified": modified,
                "begin_datetime": created,
                "publisher": "Statistik Austria" if publisher else None,
            },
            "_html_fields": fields,
            "_detail_api_url": None,
            "state": None,
            "dataset": dataset,
        }

    def _find_next_page(self, raw, page_url):
        soup = self._make_soup(raw)
        current = page_url.split("#", 1)[0]

        candidates = []
        candidates.extend(soup.select('a[rel="next"], link[rel="next"]'))
        for link in soup.select("ul.pagination a, .pagination a, a"):
            text = self._clean_text(link.get_text(" ")).lower()
            if text in {"weiter", "next", ">", "»", "›"}:
                candidates.append(link)

        for link in candidates:
            href = link.get("href") if link is not None else None
            if not href or href.startswith("#"):
                continue
            absolute = urljoin(page_url, href).split("#", 1)[0]
            if absolute and absolute != current:
                return absolute
        return None

    # ------------------------------------------------------------------
    # Record construction
    # ------------------------------------------------------------------

    def _build_record(self, item, detail):
        dataset = item.get("dataset")
        if not dataset:
            return None

        extras = detail.get("extras") if isinstance(detail.get("extras"), dict) else {}
        resources = detail.get("resources") if isinstance(detail.get("resources"), list) else []
        title = self._clean_text(detail.get("title") or item.get("title"))
        if not title:
            return None

        detail_url = (
            extras.get("metadata_original_portal")
            or item.get("url")
            or f"{_BASE_URL}/web/meta.jsp?dataset={quote(dataset)}"
        )
        json_url = detail.get("_detail_api_url") or item.get("json_url")

        primary_resource = resources[0] if resources else {}
        primary_url = (primary_resource.get("url") or item.get("data_url") or "").strip() or None
        pdf_url, pdf_filename = self._first_pdf_resource(resources)
        primary_filename = self._filename_from_url(primary_url)
        original_filename = pdf_filename or primary_filename

        published_raw = (
            extras.get("begin_datetime")
            or primary_resource.get("created")
            or detail.get("metadata_created")
        )
        listed_raw = (
            item.get("listed_date_raw")
            or extras.get("metadata_modified")
            or primary_resource.get("last_modified")
        )
        published_date = self._iso_date(published_raw)
        listed_date = self._iso_date(listed_raw)

        tags = detail.get("tags") if isinstance(detail.get("tags"), list) else []
        keywords = ", ".join(self._clean_text(t) for t in tags if self._clean_text(t)) or None

        categories = extras.get("categorization") if isinstance(extras.get("categorization"), list) else []
        category = "; ".join(self._clean_text(c) for c in categories if self._clean_text(c)) or None

        publisher = (
            self._clean_text(extras.get("publisher"))
            or self._clean_text(detail.get("maintainer"))
            or "Statistik Austria"
        )
        department = self._clean_text(detail.get("maintainer")) or None
        abstract = self._build_abstract(item, detail, resources, category, keywords)

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "dataset": dataset,
            "dataset_id": dataset,
            "post_number": dataset,
            "detail_api_url": json_url,
            "metadata_identifier": extras.get("metadata_identifier"),
            "metadata_modified": extras.get("metadata_modified"),
            "metadata_original_portal": extras.get("metadata_original_portal") or detail_url,
            "metadata_linkage": extras.get("metadata_linkage"),
            "begin_datetime": extras.get("begin_datetime"),
            "end_datetime": extras.get("end_datetime"),
            "schema_name": extras.get("schema_name"),
            "schema_language": extras.get("schema_language"),
            "schema_characterset": extras.get("schema_characterset"),
            "attribute_description": extras.get("attribute_description"),
            "maintainer": detail.get("maintainer"),
            "maintainer_email": detail.get("maintainer_email"),
            "maintainer_link": extras.get("maintainer_link"),
            "publisher": extras.get("publisher"),
            "license": detail.get("license"),
            "license_citation": extras.get("license_citation"),
            "update_frequency": extras.get("update_frequency"),
            "geographic_toponym": extras.get("geographic_toponym"),
            "geographic_bbox": extras.get("geographic_bbox"),
            "state": detail.get("state"),
            "list_title": item.get("title"),
            "list_description": item.get("description"),
            "list_panel_title": item.get("panel_title"),
            "list_date_raw": item.get("listed_date_raw"),
            "primary_resource_url": primary_url,
            "resource_names": [
                r.get("name") for r in resources if isinstance(r, dict) and r.get("name")
            ],
            "resources": resources,
            "raw_detail": detail,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SITE_ID}:{dataset}")),
            "site_id": self.site_id,
            "external_id": dataset,
            "post_number": dataset,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    def _build_abstract(self, item, detail, resources, category, keywords):
        parts = []
        notes = self._clean_text(detail.get("notes") or item.get("description"))
        if notes:
            parts.append(f"Beschreibung: {notes}")

        extras = detail.get("extras") if isinstance(detail.get("extras"), dict) else {}
        en_desc = self._clean_text(extras.get("en_title_and_desc"))
        if en_desc and en_desc.lower() != notes.lower():
            parts.append(f"Englische Beschreibung: {en_desc}")

        if keywords:
            parts.append(f"Schlagworte: {keywords}")
        if category:
            parts.append(f"Kategorie: {category}")

        frequency = self._clean_text(extras.get("update_frequency"))
        if frequency:
            parts.append(f"Aktualisierungsfrequenz: {frequency}")

        resource_names = [
            self._clean_text(r.get("name"))
            for r in resources
            if isinstance(r, dict) and self._clean_text(r.get("name"))
        ]
        if resource_names:
            parts.append("Ressourcen: " + "; ".join(resource_names[:6]))

        attribute_description = self._clean_text(extras.get("attribute_description"))
        if attribute_description:
            if len(attribute_description) > 800:
                attribute_description = attribute_description[:797].rstrip() + "..."
            parts.append(f"Merkmale: {attribute_description}")

        return "\n".join(parts).strip()

    # ------------------------------------------------------------------
    # Scalar helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _dataset_from_url(url):
        parsed = urlparse(url)
        values = parse_qs(parsed.query).get("dataset")
        return values[0] if values else None

    @staticmethod
    def _iso_date(value):
        if not value:
            return None
        text = str(value).strip()
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            return m.group(1)
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if m:
            day, month, year = m.groups()
            try:
                return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
            except ValueError:
                return None
        return None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if "." in tail and len(tail) <= 255:
            return tail
        return None

    def _first_pdf_resource(self, resources):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            url = (resource.get("url") or "").strip()
            fmt = (resource.get("format") or "").strip().lower()
            if url and (fmt == "pdf" or urlparse(url).path.lower().endswith(".pdf")):
                return url, self._filename_from_url(url)
        return None, None
