# -*- coding: utf-8 -*-
"""Crawler for SODHA Dataverse datasets.

Discovered endpoints:
  - List:   /api/search?q=*&type=dataset&subtree=sodha&sort=dateSort&order=desc
  - Detail: /api/datasets/:persistentId/?persistentId=doi:...
  - File:   /api/access/datafile/{dataFile.id}

The file is loaded with spec_from_file_location, so imports must be absolute.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlencode, urlparse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "../../.."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


class SodhaBeDataverseCrawler(BaseCrawler):
    site_id = "sodha-be-dataverse"
    site_name = "Custom: sodha-be-dataverse"
    base_url = "https://www.sodha.be"

    _SEARCH_API = base_url + "/api/search"
    _DETAIL_API = base_url + "/api/datasets/:persistentId/"
    _PAGE_SIZE = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 100

    def crawl(self, limit=None):
        saved = 0
        start_time = time.time()
        seen_urls = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for p in range(1, self._MAX_PAGES + 1):
            if time.time() - start_time >= self._WALL_BUDGET:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if limit is not None and saved >= limit:
                break

            start = (p - 1) * self._PAGE_SIZE
            list_url = self._search_url(start=start)
            page_data = self._curl_json(list_url, context=f"page {p}")
            if page_data is None:
                print(f"[{self.site_id}] page {p}: fetch failed after retries; stopping")
                break

            items = ((page_data.get("data") or {}).get("items") or [])
            if not items:
                print(f"[{self.site_id}] page {p}: 0 items; done")
                break

            new_urls_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= self._WALL_BUDGET:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    return saved

                item_label = self._item_label(item)
                try:
                    persistent_id = self._persistent_id(item)
                    detail_url = self._dataset_url(persistent_id) if persistent_id else self._clean_url(item.get("url"))
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_urls_on_page += 1

                    paper = self._fetch_parse_item(item)
                    if paper is None:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skip {item_label}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    time.sleep(getattr(self, "_delay", 1.0))
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

            data = page_data.get("data") or {}
            count_in_response = data.get("count_in_response")
            total_count = data.get("total_count")
            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {p}: no new URLs; done")
                break
            if isinstance(count_in_response, int) and count_in_response < self._PAGE_SIZE:
                break
            if isinstance(total_count, int) and start + len(items) >= total_count:
                break
        else:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")

        return saved

    def _search_url(self, start):
        params = {
            "q": "*",
            "type": "dataset",
            "subtree": "sodha",
            "sort": "dateSort",
            "order": "desc",
            "per_page": str(self._PAGE_SIZE),
            "start": str(start),
        }
        return self._SEARCH_API + "?" + urlencode(params)

    def _fetch_parse_item(self, item):
        persistent_id = self._persistent_id(item)
        if not persistent_id:
            raise ValueError("missing persistent dataset ID")

        detail = None
        detail_api_url = self._DETAIL_API + "?" + urlencode({"persistentId": persistent_id})
        detail_json = self._curl_json(detail_api_url, context=f"detail {persistent_id}")
        if detail_json and detail_json.get("status") == "OK":
            detail = detail_json.get("data") or {}

        html_detail = None
        if detail is None:
            raw = self._curl_text(self._dataset_url(persistent_id), context=f"detail html {persistent_id}")
            if raw:
                html_detail = self._parse_detail_html(raw)
        if detail is None and html_detail is None:
            raise RuntimeError(f"detail fetch failed for {persistent_id}")

        return self._build_paper(item, detail or {}, html_detail or {})

    def _build_paper(self, item, detail, html_detail):
        version = detail.get("latestVersion") or {}
        fields = self._field_map(version)

        persistent_id = (
            detail.get("latestVersion", {}).get("datasetPersistentId")
            or self._persistent_id(item)
        )
        doi = self._doi_from_persistent_id(persistent_id)
        dataset_id = detail.get("id")
        version_id = version.get("id") or item.get("versionId")
        external_id = str(dataset_id or persistent_id)
        post_number = str(dataset_id) if dataset_id is not None else self._post_number_from_persistent_id(persistent_id)

        title = (
            self._text_field(fields, "title")
            or html_detail.get("title")
            or self._clean_text(item.get("name"))
            or "(untitled)"
        )
        abstract = self._abstract(fields) or html_detail.get("abstract") or self._clean_text(item.get("description"))

        listed_raw = item.get("published_at") or item.get("updatedAt") or version.get("releaseTime")
        listed_date = self._iso_date(listed_raw)
        published_raw = detail.get("publicationDate") or version.get("releaseTime") or item.get("published_at")
        published_date = self._iso_date(published_raw)

        authors = self._authors(fields) or self._join_values(item.get("authors")) or html_detail.get("authors")
        producers = self._producers(fields) or self._join_values(item.get("producers"))
        publisher = self._join_unique(
            item.get("publisher"),
            detail.get("publisher"),
            producers,
        )
        department = self._join_unique(
            item.get("name_of_dataverse"),
            item.get("identifier_of_dataverse"),
        )

        keywords = self._keywords(fields) or self._join_values(item.get("keywords"), sep=",")
        category = self._join_unique("dataset", self._join_values(item.get("subjects"), sep=","))
        journal_raw = self._journal_raw(fields) or html_detail.get("journal_raw")
        journal = html_detail.get("journal") or None
        series = self._first_text_field(fields, ("series", "seriesInformation"))
        volume = self._first_text_field(fields, ("journalVolume", "volume"))
        issue = self._first_text_field(fields, ("journalIssue", "issue"))

        selected_file = self._select_file(version.get("files") or [])
        pdf_url = None
        original_filename = None
        datafile_id = None
        if selected_file:
            data_file = selected_file.get("dataFile") or {}
            datafile_id = data_file.get("id")
            if datafile_id is not None:
                pdf_url = f"{self.base_url}/api/access/datafile/{datafile_id}"
            original_filename = (
                data_file.get("filename")
                or selected_file.get("label")
                or self._filename_from_url(pdf_url)
            )
        if not original_filename:
            original_filename = self._filename_from_url(pdf_url)

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "dataset_id": dataset_id,
            "version_id": version_id,
            "datafile_id": datafile_id,
            "persistent_id": persistent_id,
            "global_id": item.get("global_id"),
            "doi": doi,
            "identifier": detail.get("identifier"),
            "identifier_of_dataverse": item.get("identifier_of_dataverse"),
            "name_of_dataverse": item.get("name_of_dataverse"),
            "storageIdentifier": detail.get("storageIdentifier") or item.get("storageIdentifier"),
            "versionState": version.get("versionState") or item.get("versionState"),
            "majorVersion": version.get("versionNumber") or item.get("majorVersion"),
            "minorVersion": version.get("versionMinorNumber") or item.get("minorVersion"),
            "createdAt": item.get("createdAt") or version.get("createTime"),
            "updatedAt": item.get("updatedAt") or version.get("lastUpdateTime"),
            "releaseTime": version.get("releaseTime"),
            "productionDate": version.get("productionDate"),
            "dateOfDeposit": self._text_field(fields, "dateOfDeposit"),
            "subjects": item.get("subjects"),
            "contacts": item.get("contacts"),
            "publications": item.get("publications"),
            "geographicCoverage": item.get("geographicCoverage"),
            "dataSources": item.get("dataSources"),
            "files": self._file_metadata(version.get("files") or []),
            "raw_search_item": item,
            "raw_metadata_blocks": version.get("metadataBlocks"),
        }

        return {
            "id": f"{self.site_id}:{external_id}",
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
            "url": self._dataset_url(persistent_id),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _curl_json(self, url, context):
        raw = self._curl_text(url, context=context, accept="application/json")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] {context}: JSON parse failed: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] {context}: JSON root was not an object")
            return None
        return data

    def _curl_text(self, url, context, accept="text/html,application/json,*/*"):
        waits = (1, 3, 9)
        last_error = None
        for attempt in range(3):
            try:
                proc = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-skL",
                        "--compressed",
                        "--connect-timeout",
                        "20",
                        "--max-time",
                        "60",
                        "-A",
                        self.USER_AGENT,
                        "-H",
                        f"Accept: {accept}",
                        url,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=75,
                )
                body = proc.stdout.decode("utf-8", errors="replace")
                stderr = proc.stderr.decode("utf-8", errors="replace").strip()
                if proc.returncode == 0 and body.strip():
                    return body
                last_error = stderr or f"curl exit {proc.returncode}"
            except Exception as exc:
                last_error = str(exc)

            print(f"[{self.site_id}] {context}: fetch failed attempt {attempt + 1}/3: {last_error}")
            if attempt < 2:
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] {context}: failed after 3 attempts; skipping")
        return None

    def _parse_detail_html(self, raw):
        soup = self._safe_soup(raw)
        if soup is None:
            return {}
        meta = {}
        for tag in soup.find_all("meta"):
            key = tag.get("name") or tag.get("property")
            val = tag.get("content")
            if key and val:
                meta.setdefault(key, []).append(self._clean_text(val))
        return {
            "title": self._first(meta.get("citation_title")) or self._first(meta.get("og:title")),
            "abstract": self._first(meta.get("citation_abstract")) or self._first(meta.get("description")),
            "authors": self._join_values(meta.get("citation_author")),
            "journal": self._first(meta.get("citation_journal_title")),
            "journal_raw": self._first(meta.get("citation_journal_title")),
        }

    def _safe_soup(self, raw):
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup unavailable: {exc}")
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
        return None

    def _field_map(self, version):
        out = {}
        blocks = version.get("metadataBlocks") or {}
        for block in blocks.values():
            for field in block.get("fields") or []:
                name = field.get("typeName")
                if name:
                    out[name] = field
        return out

    def _text_field(self, fields, name):
        field = fields.get(name) or {}
        value = field.get("value")
        if isinstance(value, str):
            return self._clean_text(value)
        if isinstance(value, list):
            return self._join_values(value)
        return None

    def _first_text_field(self, fields, names):
        for name in names:
            value = self._text_field(fields, name)
            if value:
                return value
        return None

    def _compound_values(self, fields, field_name, child_name):
        field = fields.get(field_name) or {}
        values = field.get("value") or []
        out = []
        if isinstance(values, dict):
            values = [values]
        for entry in values:
            if not isinstance(entry, dict):
                continue
            child = entry.get(child_name)
            if isinstance(child, dict):
                val = child.get("value")
            else:
                val = child
            val = self._clean_text(val)
            if val:
                out.append(val)
        return out

    def _authors(self, fields):
        return self._join_values(self._compound_values(fields, "author", "authorName"))

    def _producers(self, fields):
        return self._join_values(self._compound_values(fields, "producer", "producerName"))

    def _abstract(self, fields):
        descriptions = self._compound_values(fields, "dsDescription", "dsDescriptionValue")
        return "\n\n".join(descriptions) if descriptions else None

    def _keywords(self, fields):
        values = self._compound_values(fields, "keyword", "keywordValue")
        if not values:
            values = self._compound_values(fields, "topicClassification", "topicClassValue")
        return self._join_values(values, sep=",")

    def _journal_raw(self, fields):
        pubs = self._compound_values(fields, "publication", "publicationCitation")
        return self._join_values(pubs)

    def _select_file(self, files):
        unrestricted = [f for f in files if not f.get("restricted")]
        candidates = unrestricted or list(files)
        if not candidates:
            return None
        for item in candidates:
            data_file = item.get("dataFile") or {}
            ctype = (data_file.get("contentType") or "").lower()
            name = (data_file.get("filename") or item.get("label") or "").lower()
            if ctype == "application/pdf" or name.endswith(".pdf"):
                return item
        return candidates[0]

    def _file_metadata(self, files):
        out = []
        for item in files:
            data_file = item.get("dataFile") or {}
            out.append(
                {
                    "id": data_file.get("id"),
                    "filename": data_file.get("filename") or item.get("label"),
                    "label": item.get("label"),
                    "contentType": data_file.get("contentType"),
                    "filesize": data_file.get("filesize"),
                    "restricted": item.get("restricted"),
                    "persistentId": data_file.get("persistentId"),
                    "storageIdentifier": data_file.get("storageIdentifier"),
                    "checksum": data_file.get("checksum"),
                    "creationDate": data_file.get("creationDate"),
                    "description": item.get("description") or data_file.get("description"),
                }
            )
        return out

    def _persistent_id(self, item):
        for key in ("global_id", "datasetPersistentId", "persistentId"):
            value = item.get(key)
            if value:
                return str(value)
        url = item.get("url") or ""
        if "10.34934/" in url:
            return "doi:" + url.split("10.34934/", 1)[1].strip("/")
        return None

    def _post_number_from_persistent_id(self, persistent_id):
        if not persistent_id:
            return None
        tail = persistent_id.rstrip("/").split("/")[-1]
        return tail or persistent_id

    def _doi_from_persistent_id(self, persistent_id):
        if not persistent_id:
            return None
        value = str(persistent_id)
        if value.lower().startswith("doi:"):
            return value[4:]
        if value.startswith("10."):
            return value
        return None

    def _dataset_url(self, persistent_id):
        if not persistent_id:
            return None
        return self.base_url + "/dataset.xhtml?" + urlencode({"persistentId": persistent_id})

    def _iso_date(self, value):
        if not value:
            return None
        text = str(value).strip()
        match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        if re.fullmatch(r"\d{4}", text):
            return text
        return text[:10] if len(text) >= 10 else text

    def _filename_from_url(self, url):
        if not url:
            return None
        path = urlparse(url).path
        tail = path.rstrip("/").split("/")[-1]
        tail = quote(tail, safe="").replace("%", "")
        return tail or None

    def _clean_url(self, url):
        if not url:
            return None
        return str(url).strip()

    def _clean_text(self, value):
        if value is None:
            return None
        text = str(value)
        if "<" in text and ">" in text:
            text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    def _join_values(self, values, sep=";"):
        if values is None:
            return None
        if isinstance(values, str):
            values = [values]
        out = []
        for value in values:
            cleaned = self._clean_text(value)
            if cleaned and cleaned not in out:
                out.append(cleaned)
        return sep.join(out) if out else None

    def _join_unique(self, *values):
        out = []
        for value in values:
            if not value:
                continue
            parts = value if isinstance(value, list) else str(value).split(";")
            for part in parts:
                cleaned = self._clean_text(part)
                if cleaned and cleaned not in out:
                    out.append(cleaned)
        return "; ".join(out) if out else None

    def _first(self, values):
        if not values:
            return None
        if isinstance(values, str):
            return self._clean_text(values)
        for value in values:
            cleaned = self._clean_text(value)
            if cleaned:
                return cleaned
        return None

    def _item_label(self, item):
        return (
            item.get("global_id")
            or item.get("url")
            or self._clean_text(item.get("name"))
            or "unknown"
        )
