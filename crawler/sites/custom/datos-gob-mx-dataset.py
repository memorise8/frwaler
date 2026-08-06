# -*- coding: utf-8 -*-
"""datos.gob.mx dataset crawler.

The public dataset listing at
``https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=territorio``
is a CKAN 2.11 portal. Records behind that view are exposed through CKAN's
``package_search`` / ``package_show`` JSON APIs, which is what we use here.

Pagination uses CKAN's ``rows`` + ``start`` offsets. We walk the search
results until the listed ``count`` is exhausted, the requested ``limit`` is
reached, a safety cap is hit, or the wall-clock budget expires.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class DatosGobMxDatasetCrawler(BaseCrawler):
    site_id = "datos-gob-mx-dataset"
    site_name = "Custom: datos-gob-mx-dataset"
    base_url = "https://datos.gob.mx"

    START_URL = (
        "https://datos.gob.mx/dataset/"
        "?_groups_limit=0&_organization_limit=0&groups=presupuesto"
    )
    LIST_API_URL = "https://datos.gob.mx/api/3/action/package_search"
    DETAIL_API_URL = "https://datos.gob.mx/api/3/action/package_show"
    DETAIL_PAGE_URL = "https://datos.gob.mx/dataset/{name}"
    GROUP_FILTER = "groups:presupuesto"
    PAGE_SIZE = 25
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100
    PAGE_SAFETY_CAP = 200
    WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _NETWORK_FAILED = object()

    def crawl(self, limit=None):
        saved = 0
        start_offset = 0
        page = 1
        seen_urls = set()
        deadline = time.monotonic() + self.WALL_CLOCK_BUDGET_SEC
        limit_label = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self.PAGE_SAFETY_CAP:
                print(
                    f"[{self.site_id}] safety cap reached at page {page}; stopping"
                )
                break
            if time.monotonic() > deadline:
                print(
                    f"[{self.site_id}] wall-clock budget exhausted; stopping at page {page}"
                )
                break

            list_data = self._fetch_list(start_offset)
            if not list_data:
                print(f"[{self.site_id}] list API failed at page {page}; stopping")
                break

            result = list_data.get("result") or {}
            records = result.get("results") or []
            total = result.get("count")
            if not records:
                print(f"[{self.site_id}] no records returned at page {page}; stopping")
                break

            print(
                f"[{self.site_id}] page {page}: discovered {len(records)} records "
                f"(start={start_offset}, count={total})"
            )

            new_in_page = 0
            for idx, record in enumerate(records, start=start_offset + 1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() > deadline:
                    print(f"[{self.site_id}] wall-clock budget exhausted mid-page")
                    break

                try:
                    package_key = self._package_key(record)
                    if not package_key:
                        raise RuntimeError("record has no package id or name")

                    name = (record.get("name") or package_key).strip()
                    detail_url = self.DETAIL_PAGE_URL.format(name=quote(name, safe=""))
                    if detail_url in seen_urls:
                        print(
                            f"[{self.site_id}] item {idx} skipped: already seen "
                            f"({detail_url})"
                        )
                        continue
                    seen_urls.add(detail_url)
                    new_in_page += 1

                    # package_search already returns full package data; no need
                    # for a separate package_show call.
                    parsed = self._parse_package(record, source_format="json")

                    abstract = parsed["abstract"]
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {idx} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(
                    f"[{self.site_id}] page {page}: saved {saved}/{limit_label}"
                )

            if new_in_page == 0:
                print(
                    f"[{self.site_id}] page {page}: no new records (all duplicates); stopping"
                )
                break

            start_offset += len(records)
            page += 1
            if total is not None and start_offset >= int(total):
                break

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch_list(self, start):
        params = {
            "fq": self.GROUP_FILTER,
            "rows": str(self.PAGE_SIZE),
            "start": str(start),
        }
        url = f"{self.LIST_API_URL}?{urlencode(params)}"
        data = self._curl_json(url, context=f"list start={start}")
        if data is self._NETWORK_FAILED or not data or not data.get("success"):
            return None
        return data

    def _fetch_detail(self, package_key):
        params = {"id": package_key}
        url = f"{self.DETAIL_API_URL}?{urlencode(params)}"
        data = self._curl_json(url, context=f"item {package_key} package_show")
        if data is self._NETWORK_FAILED:
            return self._NETWORK_FAILED
        if not data or not data.get("success"):
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def _fetch_detail_html(self, package_key):
        path_key = quote(str(package_key).strip(), safe="")
        return self._curl_get(
            self.DETAIL_PAGE_URL.format(name=path_key),
            context=f"item {package_key} html detail",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )

    def _curl_json(self, url, context="request"):
        raw = self._curl_get(
            url,
            context=context,
            accept="application/json,text/javascript,*/*;q=0.8",
        )
        if not raw:
            return self._NETWORK_FAILED
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] {context} JSON root is not an object")
            return None
        return data

    def _curl_get(self, url, context="request", accept=None):
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
            "Accept-Language: es-MX,es;q=0.9,en;q=0.7",
            url,
        ]

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)

            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt + 1}/3): {last_error}"
            )
            if attempt < 2:
                wait = self.BACKOFF_SECONDS[attempt]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_package(self, item, source_format):
        package_id = (item.get("id") or item.get("name") or "").strip()
        name = (item.get("name") or package_id).strip()
        if not package_id:
            raise RuntimeError("detail record has no package id")

        title = self._clean_text(item.get("title") or name)
        if not title:
            raise RuntimeError(f"{package_id} has no title")

        resources = item.get("resources") if isinstance(item.get("resources"), list) else []
        abstract = self._build_abstract(item, resources)
        keywords = self._dedupe(self._names_from_dicts(item.get("tags")))
        groups = self._names_from_dicts(item.get("groups"), keys=("display_name", "title", "name"))
        category = "; ".join(groups) if groups else self._clean_text(item.get("type") or "dataset")

        organization = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        department = self._clean_text(organization.get("title") or organization.get("name") or "")
        authors = [department] if department else []

        primary_resource = resources[0] if resources else {}
        pdf_resources = self._pdf_resources(resources)
        pdf_url = ""
        if pdf_resources:
            pdf_url = pdf_resources[0].get("url") or ""
        elif primary_resource:
            res_url = str(primary_resource.get("url") or "")
            if res_url.lower().endswith(".pdf"):
                pdf_url = res_url

        published_date = (
            self._normalize_date(item.get("metadata_modified"))
            or self._normalize_date(item.get("metadata_created"))
            or self._normalize_date((primary_resource or {}).get("last_modified"))
            or self._normalize_date((primary_resource or {}).get("created"))
        )
        url = self.DETAIL_PAGE_URL.format(name=quote(name, safe=""))

        metadata = {
            "source_format": source_format,
            "start_url": self.START_URL,
            "list_api": self.LIST_API_URL,
            "detail_api": self.DETAIL_API_URL,
            "package_id": package_id,
            "name": name,
            "state": item.get("state"),
            "type": item.get("type"),
            "license_id": item.get("license_id"),
            "license_title": item.get("license_title"),
            "license_url": item.get("license_url"),
            "maintainer": item.get("maintainer"),
            "maintainer_email": item.get("maintainer_email"),
            "metadata_created": item.get("metadata_created"),
            "metadata_modified": item.get("metadata_modified"),
            "owner_org": item.get("owner_org"),
            "extras": self._extras_dict(item.get("extras")),
            "groups": groups,
            "resources": [self._resource_summary(resource) for resource in resources],
        }

        return {
            "external_id": package_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "metadata": metadata,
        }

    def _parse_detail_html(self, soup, record, package_key):
        title = (
            self._meta_content(soup, "og:title")
            or self._meta_content(soup, "dcterms:title")
            or self._clean_text(soup.find("h1"))
            or self._clean_text(record.get("title"))
        )
        title = re.sub(r"\s+-\s+datos\.gob\.mx\s*$", "", title or "").strip()
        abstract = self._meta_content(soup, "og:description") or self._clean_text(record.get("notes"))
        pdf_url = ""
        for link in soup.find_all("a", href=True):
            href = link.get("href", "").strip()
            if ".pdf" in href.lower():
                pdf_url = urljoin(self.base_url, href)
                break

        package_id = (record.get("id") or str(package_key)).strip()
        name = (record.get("name") or str(package_key)).strip()
        metadata = {
            "source_format": "html",
            "start_url": self.START_URL,
            "detail_page": self.DETAIL_PAGE_URL.format(name=quote(name, safe="")),
            "package_id": package_id,
            "name": name,
        }

        return {
            "external_id": package_id,
            "title": title,
            "authors": [],
            "abstract": self._clean_text(abstract),
            "category": self._clean_text(record.get("type") or "dataset"),
            "keywords": [],
            "published_date": self._normalize_date(record.get("metadata_modified")),
            "url": self.DETAIL_PAGE_URL.format(name=quote(name, safe="")),
            "pdf_url": pdf_url,
            "doi": "",
            "department": "",
            "metadata": metadata,
        }

    def _build_abstract(self, item, resources):
        parts = []
        self._add_part(parts, item.get("notes"))
        for resource in resources:
            self._add_part(parts, resource.get("description"))
            self._add_part(parts, resource.get("name"))
        if len(" ".join(parts)) < 100:
            org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
            self._add_part(parts, org.get("description"))
        if len(" ".join(parts)) < 100:
            groups = item.get("groups") if isinstance(item.get("groups"), list) else []
            for group in groups:
                if isinstance(group, dict):
                    self._add_part(parts, group.get("description"))
        if len(" ".join(parts)) < 100:
            extras = self._extras_dict(item.get("extras"))
            for key, value in extras.items():
                self._add_part(parts, f"{key}: {value}")
        return "\n\n".join(parts).strip()

    def _add_part(self, parts, value):
        text = self._clean_text(value)
        if text and text not in parts and text.lower() not in {
            "no informado",
            "sin descripcion",
            "sin descripción",
        }:
            parts.append(text)

    def _pdf_resources(self, resources):
        out = []
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            fmt = self._clean_text(resource.get("format")).lower()
            mimetype = self._clean_text(resource.get("mimetype")).lower()
            url = str(resource.get("url") or "")
            if fmt == "pdf" or mimetype == "application/pdf" or url.lower().endswith(".pdf"):
                out.append(resource)
        return out

    def _resource_summary(self, resource):
        if not isinstance(resource, dict):
            return {}
        return {
            "id": resource.get("id"),
            "name": self._clean_text(resource.get("name")),
            "description": self._clean_text(resource.get("description")),
            "format": resource.get("format"),
            "mimetype": resource.get("mimetype"),
            "url": resource.get("url"),
            "created": resource.get("created"),
            "last_modified": resource.get("last_modified"),
            "metadata_modified": resource.get("metadata_modified"),
            "size": resource.get("size"),
        }

    @staticmethod
    def _package_key(record):
        if not isinstance(record, dict):
            return ""
        return (record.get("id") or record.get("name") or "").strip()

    def _extras_dict(self, value):
        extras = {}
        if not isinstance(value, list):
            return extras
        for item in value:
            if not isinstance(item, dict):
                continue
            key = self._clean_text(item.get("key"))
            val = self._clean_text(item.get("value"))
            if key and val:
                extras[key] = val
        return extras

    def _names_from_dicts(self, value, keys=("display_name", "name", "title")):
        names = []
        if not isinstance(value, list):
            return names
        for item in value:
            if not isinstance(item, dict):
                continue
            for key in keys:
                text = self._clean_text(item.get(key))
                if text:
                    names.append(text)
                    break
        return self._dedupe(names)

    @staticmethod
    def _normalize_date(value):
        if not value:
            return ""
        match = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", str(value))
        if not match:
            return ""
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    def _meta_content(self, soup, name):
        tag = (
            soup.find("meta", attrs={"property": name})
            or soup.find("meta", attrs={"name": name})
        )
        if tag and tag.get("content"):
            return self._clean_text(tag.get("content"))
        return ""

    @staticmethod
    def _dedupe(items):
        seen = set()
        out = []
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = str(value)
        text = unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#>]+", " ", text)
        text = re.sub(r"\r\n|\r|\n|\t", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


# ---------------------------------------------------------------------------
# Per-portal variants of the datos.gob.mx CKAN crawler.
#
# datos.gob.mx exposes every ministry / thematic group through the same CKAN
# ``package_search`` API; only the ``fq`` facet filter differs. Each site_id
# below is one of the "Mexico Ministries" portals that previously had no
# crawler (the D group). We reuse the fully-tested base implementation and only
# override the facet filter + start URL. Because the custom-crawler loader
# registers *every* class in this module that carries a ``site_id`` + ``crawl``,
# defining the subclasses here is enough to register all of them.
#
# ``GROUP_FILTER`` accepts either ``groups:<slug>`` or ``organization:<slug>``
# — CKAN treats both as ordinary facet queries.
# ---------------------------------------------------------------------------


class _DatosGobMxVariant(DatosGobMxDatasetCrawler):
    """Base for the per-portal variants; subclasses set site_id + GROUP_FILTER."""


def _variant(site_id, group_filter, start_url):
    return type(
        "".join(part.capitalize() for part in site_id.split("-")) + "Crawler",
        (_DatosGobMxVariant,),
        {
            "site_id": site_id,
            "site_name": f"Custom: {site_id}",
            "START_URL": start_url,
            "GROUP_FILTER": group_filter,
        },
    )


DatosGobMxAgriculturaCrawler = _variant(
    "datos-gob-mx-agricultura", "groups:agricultura",
    "https://datos.gob.mx/dataset/?groups=agricultura")
DatosGobMxCulturaCrawler = _variant(
    "datos-gob-mx-cultura", "groups:cultura",
    "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=cultura")
DatosGobMxPresupuestoCrawler = _variant(
    "datos-gob-mx-presupuesto", "groups:presupuesto",
    "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=presupuesto")
DatosGobMxSecretariaSaludCrawler = _variant(
    "datos-gob-mx-secretaria-salud", "organization:secretaria_salud",
    "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&organization=secretaria_salud")
DatosGobMxSecretariaTrabajoCrawler = _variant(
    "datos-gob-mx-secretaria-trabajo", "organization:secretaria_trabajo",
    "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&organization=secretaria_trabajo")
DatosGobMxSeguridadCrawler = _variant(
    "datos-gob-mx-seguridad", "groups:seguridad",
    "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=seguridad")
DatosGobMxTerritorioCrawler = _variant(
    "datos-gob-mx-territorio", "groups:territorio",
    "https://datos.gob.mx/dataset/?_groups_limit=0&_organization_limit=0&groups=territorio")
DatosGobMxTurismoCrawler = _variant(
    "datos-gob-mx-turismo", "groups:turismo",
    "https://datos.gob.mx/dataset/?_groups_limit=0&groups=turismo")
