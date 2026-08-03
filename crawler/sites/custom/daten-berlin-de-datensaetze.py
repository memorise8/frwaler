# -*- coding: utf-8 -*-
"""Crawler for daten.berlin.de datasets filtered to PDF resources.

The public Drupal page at /datensaetze is backed by the CKAN registry at
https://datenregister.berlin.de. The list endpoint is package_search and the
detail endpoint is package_show.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from email.parser import Parser
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

# Absolute import: this file is loaded via spec_from_file_location.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


class DatenBerlinDeDatensaetzeCrawler(BaseCrawler):
    site_id = "daten-berlin-de-datensaetze"
    site_name = "Custom: daten-berlin-de-datensaetze"
    base_url = "https://daten.berlin.de"

    START_URL = (
        "https://daten.berlin.de/datensaetze"
        "?res_format=PDF&sort=score+desc%2C+metadata_modified+desc"
    )
    REGISTRY_BASE_URL = "https://datenregister.berlin.de"
    LIST_API_URL = f"{REGISTRY_BASE_URL}/api/3/action/package_search"
    DETAIL_API_URL = f"{REGISTRY_BASE_URL}/api/3/action/package_show"
    PAGE_SIZE = 50
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    WALL_BUDGET_MARGIN_SECONDS = 15
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        saved = 0
        page = 1
        start_offset = 0
        started_at = time.time()
        seen_urls = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            if self._budget_nearly_exhausted(started_at):
                print(
                    f"[{self.site_id}] approaching 25-minute wall-clock budget; "
                    "exiting cleanly"
                )
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_data = self._fetch_list(start_offset)
            if not list_data:
                print(f"[{self.site_id}] list API failed at page {page}; stopping")
                break

            result = list_data.get("result") or {}
            records = result.get("results") or []
            total = result.get("count")
            if not records:
                print(f"[{self.site_id}] page {page}: no records returned; stopping")
                break

            new_urls_on_page = 0
            for index, record in enumerate(records, start=start_offset + 1):
                if limit is not None and saved >= limit:
                    break
                if self._budget_nearly_exhausted(started_at):
                    print(
                        f"[{self.site_id}] approaching 25-minute wall-clock budget; "
                        "exiting cleanly"
                    )
                    return saved

                item_label = self._item_label(record, index)
                try:
                    detail_url = self._detail_page_url(record)
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_urls_on_page += 1

                    if self._delay:
                        time.sleep(float(self._delay))

                    detail = self._fetch_detail(record)
                    if detail:
                        parsed = self._parse_package(detail, record)
                    else:
                        html = self._fetch_detail_html(record)
                        if not html:
                            raise RuntimeError("detail API and HTML fallback failed")
                        soup = self._make_soup(html)
                        if soup is None:
                            raise RuntimeError("detail HTML could not be parsed")
                        parsed = self._parse_detail_html(soup, record)

                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(self._paper_from_parsed(parsed))
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                        f"{parsed.get('title', '')[:80]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(
                    f"[{self.site_id}] page {page}: no new URLs; "
                    "stopping to avoid pagination loop"
                )
                break

            start_offset += len(records)
            if total is not None:
                try:
                    if start_offset >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
            if len(records) < self.PAGE_SIZE:
                break

            page += 1
        else:
            print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def _fetch_list(self, start_offset):
        params = {
            "fq": "res_format:PDF",
            "sort": "score desc, metadata_modified desc",
            "rows": str(self.PAGE_SIZE),
            "start": str(start_offset),
        }
        url = f"{self.LIST_API_URL}?{urlencode(params)}"
        data = self._curl_json(url, context=f"list start={start_offset}")
        if not data or not data.get("success"):
            if data:
                print(f"[{self.site_id}] list API success=false: {data.get('error')}")
            return None
        return data

    def _fetch_detail(self, record):
        package_id = (record.get("id") or record.get("name") or "").strip()
        if not package_id:
            return None
        url = f"{self.DETAIL_API_URL}?{urlencode({'id': package_id})}"
        data = self._curl_json(url, context=f"item {package_id} package_show")
        if not data or not data.get("success"):
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def _fetch_detail_html(self, record):
        url = self._detail_page_url(record)
        if not url:
            return None
        return self._curl_get(
            url,
            context=f"item {record.get('name') or record.get('id') or url} detail html",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )

    def _curl_json(self, url, context="request"):
        raw = self._curl_get(url, context=context, accept="application/json,*/*;q=0.8")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] {context} JSON root is not an object")
            return None
        return data

    def _curl_get(self, url, context="request", accept=None, method="GET"):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--fail",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'application/json,text/html,*/*;q=0.8'}",
            "-H",
            "Accept-Language: de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if method == "HEAD":
            cmd.append("-I")
        cmd.append(url)

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt}/3): {last_error}"
            )
            if attempt < len(self.BACKOFF_SECONDS):
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
        return None

    def _parse_package(self, package, list_record=None):
        if list_record is None:
            list_record = {}

        dataset_id = self._clean(package.get("id") or list_record.get("id"))
        name = self._clean(package.get("name") or list_record.get("name") or dataset_id)
        title = self._clean(package.get("title") or list_record.get("title") or name)
        if not title:
            raise RuntimeError("package has no title")

        detail_url = self._detail_page_url(package) or self._detail_page_url(list_record)
        if not detail_url:
            raise RuntimeError("package has no detail URL")

        resources = package.get("resources") or []
        pdf_resource = self._first_pdf_resource(resources)
        pdf_url = self._normalise_resource_url(pdf_resource.get("url")) if pdf_resource else None
        original_filename = self._original_filename(pdf_url, pdf_resource)

        published_raw = (
            package.get("date_released")
            or package.get("issued")
            or package.get("metadata_created")
            or ""
        )
        listed_raw = (
            package.get("date_updated")
            or package.get("metadata_modified")
            or list_record.get("date_updated")
            or list_record.get("metadata_modified")
            or package.get("metadata_created")
            or ""
        )
        published_date = self._date_only(published_raw)
        listed_date = self._date_only(listed_raw)

        tags = self._tag_names(package.get("tags") or list_record.get("tags") or [])
        groups = self._group_names(package.get("groups") or list_record.get("groups") or [])
        category = ", ".join(groups) if groups else None
        org = package.get("organization") if isinstance(package.get("organization"), dict) else {}
        publisher = self._clean(org.get("title") or org.get("name"))
        author = self._clean(package.get("author") or list_record.get("author"))
        maintainer = self._clean(package.get("maintainer") or list_record.get("maintainer"))
        authors = self._join_unique([author, maintainer])
        department = self._clean(package.get("attribution_text")) or maintainer or None

        extras = self._extras_dict(package)
        journal = self._first_value(
            package,
            extras,
            ("journal", "journal_raw", "journalName", "publication", "venue"),
        )
        doi = self._first_value(package, extras, ("doi", "DOI"))

        abstract = self._build_abstract(package, tags, groups, publisher, resources)
        post_number = self._post_number(name, dataset_id)

        metadata = self._metadata_for_package(
            package=package,
            list_record=list_record,
            detail_url=detail_url,
            pdf_resource=pdf_resource,
            pdf_url=pdf_url,
            original_filename=original_filename,
            posted_raw=listed_raw,
            post_number=post_number,
            extras=extras,
        )

        return {
            "external_id": dataset_id or name,
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
            "keywords": ", ".join(tags) if tags else None,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _parse_detail_html(self, soup, list_record):
        detail_url = self._detail_page_url(list_record)
        title = self._text(soup.select_one("h1.title, h1")) or self._clean(
            list_record.get("title") or list_record.get("name")
        )
        desc_node = soup.select_one("section.modul-text_bild .text")
        abstract = self._text(desc_node) or self._clean(list_record.get("notes"))
        table = self._detail_table(soup)

        published_date = self._date_only(table.get("Ver\u00f6ffentlicht"))
        listed_raw = (
            table.get("Aktualisiert")
            or table.get("Metadaten Letzte \u00c4nderung")
            or list_record.get("metadata_modified")
            or ""
        )
        listed_date = self._date_only(listed_raw)
        publisher = table.get("Ver\u00f6ffentlichende Stelle")
        department = table.get("Kontaktperson")
        category = table.get("Kategorie")
        keywords = table.get("Schlagworte")
        resources = self._html_resources(soup)
        pdf_resource = self._first_pdf_resource(resources)
        pdf_url = self._normalise_resource_url(pdf_resource.get("url")) if pdf_resource else None
        original_filename = self._original_filename(pdf_url, pdf_resource)
        name = self._clean(list_record.get("name")) or self._slug_from_url(detail_url)
        dataset_id = self._clean(list_record.get("id")) or name
        post_number = self._post_number(name, dataset_id)

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "ckan_id": dataset_id,
            "ckan_name": name,
            "post_number": post_number,
            "detail_url": detail_url,
            "source": "detail_html_fallback",
            "html_table": table,
            "resources": resources,
            "raw_list_record": list_record,
        }

        return {
            "external_id": dataset_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": publisher,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _paper_from_parsed(self, parsed):
        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": parsed.get("external_id"),
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("posted_date"),
            "authors": parsed.get("authors"),
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department"),
            "journal": parsed.get("journal"),
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(parsed.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
        }

    def _metadata_for_package(
        self,
        package,
        list_record,
        detail_url,
        pdf_resource,
        pdf_url,
        original_filename,
        posted_raw,
        post_number,
        extras,
    ):
        resource_summaries = []
        for resource in package.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            resource_summaries.append(
                {
                    "id": resource.get("id"),
                    "name": resource.get("name"),
                    "format": resource.get("format"),
                    "mimetype": resource.get("mimetype"),
                    "url": resource.get("url"),
                    "description": resource.get("description"),
                    "created": resource.get("created"),
                    "last_modified": resource.get("last_modified"),
                    "metadata_modified": resource.get("metadata_modified"),
                    "position": resource.get("position"),
                    "size": resource.get("size"),
                }
            )

        metadata = {
            "posted_date": posted_raw,
            "originalFilename": original_filename,
            "journal_raw": self._first_value(
                package,
                extras,
                ("journal_raw", "journal", "journalName", "publication", "venue"),
            ),
            "series": self._first_value(package, extras, ("series", "Series")),
            "volume": self._first_value(package, extras, ("volume", "Volume")),
            "issue": self._first_value(package, extras, ("issue", "Issue")),
            "node_id": None,
            "ckan_id": package.get("id"),
            "ckan_name": package.get("name"),
            "post_number": post_number,
            "package_show_url": f"{self.DETAIL_API_URL}?{urlencode({'id': package.get('id') or package.get('name') or ''})}",
            "package_search_url": self.START_URL,
            "detail_url": detail_url,
            "source_url": package.get("url"),
            "pdf_resource_id": pdf_resource.get("id") if pdf_resource else None,
            "pdf_resource_name": pdf_resource.get("name") if pdf_resource else None,
            "pdf_url": pdf_url,
            "license_id": package.get("license_id"),
            "license_title": package.get("license_title"),
            "license_url": package.get("license_url"),
            "berlin_source": package.get("berlin_source"),
            "berlin_type": package.get("berlin_type"),
            "author_email": package.get("author_email"),
            "maintainer": package.get("maintainer"),
            "maintainer_email": package.get("maintainer_email"),
            "attribution_text": package.get("attribution_text"),
            "date_released": package.get("date_released"),
            "date_updated": package.get("date_updated"),
            "metadata_created": package.get("metadata_created"),
            "metadata_modified": package.get("metadata_modified"),
            "temporal_coverage_from": package.get("temporal_coverage_from"),
            "temporal_coverage_to": package.get("temporal_coverage_to"),
            "temporal_granularity": package.get("temporal_granularity"),
            "geographical_coverage": package.get("geographical_coverage"),
            "geographical_granularity": package.get("geographical_granularity"),
            "hvd_category": package.get("hvd_category"),
            "resources": resource_summaries,
            "tags": self._tag_names(package.get("tags") or []),
            "groups": self._group_names(package.get("groups") or []),
            "organization": package.get("organization"),
            "extras": extras,
            "raw_list_record": list_record,
            "raw_package": package,
        }
        return metadata

    def _build_abstract(self, package, tags, groups, publisher, resources):
        parts = []
        notes = self._clean_multiline(package.get("notes"))
        if notes:
            parts.append(notes)

        combined = "\n\n".join(parts)
        if len(combined) >= 100:
            return combined

        for resource in resources[:5]:
            if not isinstance(resource, dict):
                continue
            desc = self._clean_multiline(resource.get("description"))
            if desc and len(desc) > 20 and desc not in parts:
                parts.append(desc)
                combined = "\n\n".join(parts)
                if len(combined) >= 100:
                    return combined

        for label, value in (
            ("Publisher", publisher),
            ("Category", ", ".join(groups) if groups else None),
            ("Keywords", ", ".join(tags) if tags else None),
            ("License", self._clean(package.get("license_title"))),
            ("Source", self._clean(package.get("url"))),
        ):
            if value:
                parts.append(f"{label}: {value}")
                combined = "\n\n".join(parts)
                if len(combined) >= 100:
                    return combined

        return "\n\n".join(parts)

    def _first_pdf_resource(self, resources):
        for resource in resources or []:
            if not isinstance(resource, dict):
                continue
            url = self._clean(resource.get("url"))
            fmt = self._clean(resource.get("format")).upper()
            mimetype = self._clean(resource.get("mimetype")).lower()
            name = self._clean(resource.get("name"))
            if url and (
                fmt == "PDF"
                or "PDF" in fmt
                or mimetype == "application/pdf"
                or urlparse(url).path.lower().endswith(".pdf")
                or name.lower().endswith(".pdf")
            ):
                return resource
        return None

    def _normalise_resource_url(self, raw_url):
        raw_url = self._clean(raw_url)
        if not raw_url:
            return None
        return urljoin(self.base_url, raw_url)

    def _original_filename(self, pdf_url, pdf_resource=None):
        filename = self._filename_from_url(pdf_url)
        if filename:
            return filename

        if pdf_url:
            filename = self._curl_head_filename(pdf_url)
            if filename:
                return filename

        if isinstance(pdf_resource, dict):
            name = self._clean(pdf_resource.get("name"))
            if name:
                return name
        return None

    def _filename_from_url(self, url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if tail and "." in tail and len(tail) <= 240:
            return tail
        return None

    def _curl_head_filename(self, url):
        headers = self._curl_get(
            url,
            context=f"HEAD filename {url}",
            accept="application/pdf,*/*;q=0.8",
            method="HEAD",
        )
        if not headers:
            return None
        try:
            msg = Parser().parsestr(headers)
        except Exception:
            return None
        content_disposition = msg.get("Content-Disposition") or ""
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)', content_disposition)
        if not match:
            return None
        return unquote(match.group(1).strip())

    def _detail_page_url(self, record):
        name = self._clean(record.get("name")) if isinstance(record, dict) else ""
        if name:
            return f"{self.base_url}/datensaetze/{quote(name, safe='')}"
        dataset_id = self._clean(record.get("id")) if isinstance(record, dict) else ""
        if dataset_id:
            return f"{self.base_url}/datensaetze/{quote(dataset_id, safe='')}"
        return None

    def _post_number(self, name, dataset_id):
        name = self._clean(name)
        dataset_id = self._clean(dataset_id)
        for candidate in (name, dataset_id):
            if not candidate:
                continue
            match = re.search(r"(\d{4,})(?!.*\d)", candidate)
            if match:
                return match.group(1)
        return name or dataset_id or None

    def _date_only(self, value):
        value = self._clean(value)
        if not value:
            return None
        iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if iso:
            return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
        german = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
        if german:
            return (
                f"{german.group(3)}-"
                f"{int(german.group(2)):02d}-"
                f"{int(german.group(1)):02d}"
            )
        year = re.search(r"\b(\d{4})\b", value)
        if year:
            return f"{year.group(1)}-01-01"
        return value[:10] if value else None

    def _extras_dict(self, package):
        extras = package.get("extras") if isinstance(package, dict) else None
        if isinstance(extras, dict):
            return dict(extras)
        out = {}
        if isinstance(extras, list):
            for item in extras:
                if isinstance(item, dict) and item.get("key"):
                    out[str(item.get("key"))] = item.get("value")
        return out

    def _first_value(self, package, extras, keys):
        for key in keys:
            value = package.get(key) if isinstance(package, dict) else None
            if value not in (None, "", [], {}):
                return self._clean(value)
            value = extras.get(key) if isinstance(extras, dict) else None
            if value not in (None, "", [], {}):
                return self._clean(value)
        return None

    def _tag_names(self, tags):
        names = []
        for tag in tags:
            if isinstance(tag, dict):
                value = self._clean(tag.get("display_name") or tag.get("name"))
            else:
                value = self._clean(tag)
            if value and value not in names:
                names.append(value)
        return names

    def _group_names(self, groups):
        names = []
        for group in groups:
            if isinstance(group, dict):
                value = self._clean(group.get("display_name") or group.get("title") or group.get("name"))
            else:
                value = self._clean(group)
            if value and value not in names:
                names.append(value)
        return names

    def _detail_table(self, soup):
        result = {}
        for row in soup.select("ul.list--tablelist li"):
            label = self._text(row.select_one(".text--strong")).rstrip(":")
            if not label:
                continue
            cells = row.select(".cell")
            value_node = cells[1] if len(cells) > 1 else row
            value = self._text(value_node)
            if value:
                result[label] = value
        return result

    def _html_resources(self, soup):
        resources = []
        for block in soup.select(".modul-download"):
            title = self._text(block.select_one(".dp-resource-header .title"))
            fmt = self._text(block.select_one(".dp-resource-icon-label"))
            desc = self._text(block.select_one("p.text"))
            link = block.select_one("a[href]")
            url = urljoin(self.base_url, link.get("href")) if link else None
            resources.append(
                {
                    "name": title,
                    "format": fmt,
                    "description": desc,
                    "url": url,
                }
            )
        return resources

    def _item_label(self, record, index):
        if isinstance(record, dict):
            return record.get("name") or record.get("id") or str(index)
        return str(index)

    def _slug_from_url(self, url):
        if not url:
            return None
        return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or None

    def _budget_nearly_exhausted(self, started_at):
        return (time.time() - started_at) >= (
            self.WALL_BUDGET_SECONDS - self.WALL_BUDGET_MARGIN_SECONDS
        )

    def _join_unique(self, values):
        parts = []
        for value in values:
            value = self._clean(value)
            if value and value not in parts:
                parts.append(value)
        return "; ".join(parts) if parts else None

    def _clean(self, value):
        if value is None:
            return ""
        if isinstance(value, str):
            return re.sub(r"\s+", " ", value).strip()
        return re.sub(r"\s+", " ", str(value)).strip()

    def _clean_multiline(self, value):
        value = "" if value is None else str(value)
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _text(self, node):
        if node is None:
            return ""
        return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
