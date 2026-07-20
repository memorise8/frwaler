# -*- coding: utf-8 -*-
"""Crawler for data.gv.at PDF-filtered datasets.

Discovered endpoints used by the Vue/piveau UI:
  - list:   https://www.data.gv.at/api/hub/search/search
  - detail: https://www.data.gv.at/api/hub/search/datasets/{dataset_id}

The public page ``/datasets?locale=de&format=PDF&page=1`` is a SPA shell; the
records are loaded from the JSON API above with ``facets={"format":["PDF"]}``.
Absolute import is intentional because this module is loaded through
spec_from_file_location without package context.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urlencode, urljoin, urlparse

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - regex cleanup still works
    BeautifulSoup = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_BASE_URL = "https://www.data.gv.at"
_API_BASE = f"{_BASE_URL}/api/hub/search"
_LIST_API = f"{_API_BASE}/search"
_DETAIL_API = f"{_API_BASE}/datasets"
_PAGE_SIZE = 50
_MAX_PAGES = 200
_WALL_BUDGET_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 100
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


class DataGvAtDatasetsCrawler(BaseCrawler):
    site_id = "data-gv-at-datasets"
    site_name = "Custom: data-gv-at-datasets"
    base_url = _BASE_URL

    detail_delay = 1.0

    def crawl(self, limit=None):
        """Crawl PDF-format datasets and persist one document per dataset."""
        if limit is not None and limit <= 0:
            return 0

        started_at = time.time()
        saved = 0
        page = 1
        seen_urls = set()
        limit_label = limit if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at > _WALL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_data = self._fetch_list(page)
            if not list_data:
                print(f"[{self.site_id}] page {page}: list fetch failed or empty; stopping")
                break

            result = list_data.get("result") if isinstance(list_data, dict) else None
            items = result.get("results", []) if isinstance(result, dict) else []
            if not items:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            new_urls_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_id = self._string(item.get("id")) or "?"
                detail_url = self._human_url(item)
                dedupe_url = detail_url or item_id
                if dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                new_urls_on_page += 1

                try:
                    time.sleep(float(getattr(self, "detail_delay", 1.0)))
                    detail = self._fetch_detail(item_id)
                    if not detail:
                        detail = item
                    else:
                        detail.setdefault("_list_record", item)

                    paper = self._build_paper(detail)
                    if not paper:
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping to avoid pagination loop")
                break

            if len(items) < _PAGE_SIZE:
                print(f"[{self.site_id}] page {page}: last page reached")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url,
        *,
        accept="application/json, text/html;q=0.9, */*;q=0.8",
        retries=3,
        max_time=45,
        head=False,
    ):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: de-AT,de;q=0.9,en;q=0.8",
        ]
        if head:
            cmd.append("-I")
        cmd.append(url)

        waits = (1, 3, 9)
        last_error = None
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode == 0 and stdout.strip():
                    return stdout
                last_error = stderr.strip() or f"curl returned {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < retries - 1:
                print(
                    f"[{self.site_id}] network error for {url}: {last_error}; "
                    f"retrying in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] network failed after 3 attempts for {url}: {last_error}")
        return None

    def _curl_json(self, url):
        raw = self._curl_get(url, accept="application/json, */*")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode failed for {url}: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] unexpected JSON type for {url}: {type(data).__name__}")
            return None
        return data

    def _fetch_list(self, page):
        params = {
            "q": "",
            "filter": "dataset",
            "limit": _PAGE_SIZE,
            "page": page - 1,  # piveau search API is 0-based; the public URL is 1-based.
            "sort": "relevance+desc, modified+desc, title.de+asc",
            "facetOperator": "AND",
            "facetGroupOperator": "AND",
            "dataServices": "false",
            "facets": json.dumps({"format": ["PDF"]}, ensure_ascii=False),
        }
        url = f"{_LIST_API}?{urlencode(params)}"
        return self._curl_json(url)

    def _fetch_detail(self, dataset_id):
        if not dataset_id:
            return None
        url = f"{_DETAIL_API}/{dataset_id}"
        data = self._curl_json(url)
        if not data:
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def _curl_head(self, url):
        if not url:
            return None
        return self._curl_get(url, accept="*/*", retries=3, max_time=20, head=True)

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    def _build_paper(self, item):
        native_id = self._string(item.get("id"))
        if not native_id:
            return None

        title = self._pick_lang(item.get("title")) or native_id
        description = self._pick_lang(item.get("description"))
        publisher = self._names_from_value(item.get("publisher"))
        authors = self._names_from_values(
            item.get("creator"),
            item.get("originator"),
            item.get("maintainer"),
        )
        department = self._department(item)
        category = self._category(item.get("categories"))
        keywords = self._keywords(item.get("keywords"))

        pdf_dist = self._first_pdf_distribution(item.get("distributions"))
        pdf_url = self._distribution_url(pdf_dist)
        original_filename = self._original_filename(pdf_dist, pdf_url)

        published_raw = item.get("issued")
        listed_raw = (
            self._dict_get(item.get("catalog_record"), "issued")
            or self._dict_get(item.get("catalog_record"), "modified")
            or item.get("modified")
            or item.get("issued")
        )
        published_date = self._date_only(published_raw)
        listed_date = self._date_only(listed_raw)
        detail_url = self._human_url(item)
        post_number = self._post_number(native_id)

        abstract = self._abstract(
            description=description,
            title=title,
            publisher=publisher,
            category=category,
            pdf_dist=pdf_dist,
            pdf_url=pdf_url,
        )
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{self.site_id}] item {native_id} skipped: abstract < {_ABSTRACT_MIN_CHARS} chars")
            return None

        metadata = {
            "posted_date": self._string(listed_raw),
            "listed_date": listed_date,
            "published_date_raw": self._string(published_raw),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "dataset_id": native_id,
            "post_number": post_number,
            "catalog_id": self._dict_get(item.get("catalog"), "id"),
            "catalog_title": self._pick_lang(self._dict_get(item.get("catalog"), "title")),
            "distribution_id": self._dict_get(pdf_dist, "id"),
            "api_detail_url": f"{_DETAIL_API}/{native_id}",
            "resource": item.get("resource"),
            "raw": item,
        }

        return {
            "id": f"{self.site_id}:{native_id}",
            "site_id": self.site_id,
            "external_id": native_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
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

    @staticmethod
    def _dict_get(value, key):
        return value.get(key) if isinstance(value, dict) else None

    @staticmethod
    def _string(value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _make_soup(cls, raw):
        if BeautifulSoup is None:
            return None
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            print(f"[data-gv-at-datasets] BeautifulSoup failed for all parsers: {last_exc}")
        return None

    @classmethod
    def _clean_text(cls, value):
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value if v is not None)
        text = str(value)
        if "<" in text and ">" in text:
            soup = cls._make_soup(text)
            if soup is not None:
                text = soup.get_text(" ")
            else:
                text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _pick_lang(cls, value, preferred=("de", "en")):
        if value in (None, ""):
            return None
        if isinstance(value, str):
            return cls._clean_text(value)
        if isinstance(value, dict):
            for lang in preferred:
                if value.get(lang):
                    return cls._clean_text(value.get(lang))
            for val in value.values():
                if val:
                    return cls._clean_text(val)
        if isinstance(value, list):
            parts = [cls._pick_lang(v, preferred) for v in value]
            parts = [p for p in parts if p]
            return "; ".join(parts) if parts else None
        return cls._clean_text(value)

    @classmethod
    def _date_only(cls, value):
        if value in (None, ""):
            return None
        match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
        return match.group(0) if match else None

    @classmethod
    def _names_from_value(cls, value):
        if value in (None, ""):
            return None
        if isinstance(value, dict):
            candidates = [
                value.get("name"),
                value.get("organisation_name"),
                value.get("organization_name"),
                value.get("label"),
                value.get("email"),
                value.get("resource"),
            ]
            names = [cls._clean_text(v) for v in candidates if v]
            return names[0] if names else None
        if isinstance(value, list):
            names = [cls._names_from_value(v) for v in value]
            names = [n for n in names if n]
            return "; ".join(dict.fromkeys(names)) if names else None
        return cls._clean_text(value)

    @classmethod
    def _names_from_values(cls, *values):
        names = []
        for value in values:
            joined = cls._names_from_value(value)
            if joined:
                names.extend([p.strip() for p in joined.split(";") if p.strip()])
        return "; ".join(dict.fromkeys(names)) if names else None

    @classmethod
    def _department(cls, item):
        contacts = cls._names_from_value(item.get("contact_point"))
        if contacts:
            return contacts
        catalog_title = cls._pick_lang(cls._dict_get(item.get("catalog"), "title"))
        return catalog_title

    @classmethod
    def _category(cls, categories):
        if not categories:
            return None
        labels = []
        if isinstance(categories, dict):
            categories = [categories]
        for category in categories:
            if isinstance(category, dict):
                label = cls._pick_lang(category.get("label")) or cls._string(category.get("id"))
            else:
                label = cls._clean_text(category)
            if label:
                labels.append(label)
        return "; ".join(dict.fromkeys(labels)) if labels else None

    @classmethod
    def _keywords(cls, keywords):
        if not keywords:
            return None
        values = []
        if isinstance(keywords, dict):
            keywords = [keywords]
        if isinstance(keywords, list):
            for keyword in keywords:
                if isinstance(keyword, dict):
                    value = keyword.get("label") or keyword.get("id")
                else:
                    value = keyword
                value = cls._clean_text(value)
                if value:
                    values.append(value)
        else:
            values.append(cls._clean_text(keywords))
        return ", ".join(dict.fromkeys(values)) if values else None

    @classmethod
    def _first_pdf_distribution(cls, distributions):
        if not isinstance(distributions, list):
            return None
        fallback = None
        for dist in distributions:
            if not isinstance(dist, dict):
                continue
            fmt = dist.get("format")
            fmt_id = None
            fmt_label = None
            if isinstance(fmt, dict):
                fmt_id = fmt.get("id")
                fmt_label = fmt.get("label")
            else:
                fmt_id = fmt
            media_type = cls._string(dist.get("media_type")) or ""
            title = cls._pick_lang(dist.get("title")) or ""
            looks_pdf = (
                str(fmt_id or "").upper() == "PDF"
                or str(fmt_label or "").upper() == "PDF"
                or "pdf" in media_type.lower()
                or title.lower().endswith(".pdf")
            )
            if looks_pdf:
                return dist
            if fallback is None and title.lower().endswith(".pdf"):
                fallback = dist
        return fallback

    @classmethod
    def _distribution_url(cls, dist):
        if not isinstance(dist, dict):
            return None
        for key in ("download_url", "access_url"):
            value = dist.get(key)
            if isinstance(value, list):
                for url in value:
                    url = cls._string(url)
                    if url:
                        return url
            else:
                url = cls._string(value)
                if url:
                    return url
        return None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if tail and len(tail) <= 200:
            return tail
        return None

    @staticmethod
    def _filename_from_content_disposition(headers):
        if not headers:
            return None
        found = None
        for line in headers.splitlines():
            if line.lower().startswith("content-disposition:"):
                found = line.split(":", 1)[1].strip()
        if not found:
            return None
        _, params = email.utils.decode_params([p.strip() for p in found.split(";")])
        for key, value in params:
            if key.lower() in ("filename", "filename*"):
                if isinstance(value, tuple):
                    charset, _, encoded = value
                    try:
                        return unquote(encoded, encoding=charset or "utf-8")
                    except Exception:
                        return unquote(encoded)
                return unquote(str(value).strip('"'))
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', found, re.I)
        return unquote(match.group(1)) if match else None

    def _original_filename(self, dist, pdf_url):
        title = self._pick_lang(self._dict_get(dist, "title"))
        if title and ("." in title or title.lower().endswith("pdf")):
            return title
        filename = self._filename_from_url(pdf_url)
        if filename:
            return filename
        headers = self._curl_head(pdf_url) if pdf_url else None
        filename = self._filename_from_content_disposition(headers)
        if filename:
            return filename
        return title

    @classmethod
    def _human_url(cls, item):
        resource = cls._string(item.get("resource")) if isinstance(item, dict) else None
        if resource:
            return resource
        dataset_id = cls._string(item.get("id")) if isinstance(item, dict) else None
        if dataset_id:
            return urljoin(_BASE_URL, f"/katalog/datasets/{dataset_id}")
        return None

    @staticmethod
    def _post_number(native_id):
        if not native_id:
            return None
        if native_id.isdigit() or _UUID_RE.match(native_id):
            return native_id
        match = re.search(r"(?:^|[-_/])(\d{2,})$", native_id)
        return match.group(1) if match else native_id

    @classmethod
    def _abstract(cls, *, description, title, publisher, category, pdf_dist, pdf_url):
        parts = []
        if description:
            parts.append(cls._clean_text(description))
        if title and title not in parts:
            parts.append(f"Dataset title: {cls._clean_text(title)}.")
        if publisher:
            parts.append(f"Publisher: {cls._clean_text(publisher)}.")
        if category:
            parts.append(f"Category: {cls._clean_text(category)}.")
        pdf_title = cls._pick_lang(cls._dict_get(pdf_dist, "title"))
        if pdf_title:
            parts.append(f"PDF resource: {pdf_title}.")
        elif pdf_url:
            parts.append(f"PDF resource URL: {pdf_url}.")
        return re.sub(r"\s+", " ", " ".join(parts)).strip()
