# -*- coding: utf-8 -*-
"""Geoscience Australia eCat GeoNetwork crawler.

Queries the GeoNetwork Elasticsearch API for records containing PDF links.
No per-record detail fetches needed — all fields are embedded in the index.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

SEARCH_API = "https://ecat.ga.gov.au/geonetwork/srv/api/search/records/_search"
START_URL = (
    "https://ecat.ga.gov.au/geonetwork/srv/eng/catalog.search#/search"
    "?isTemplate=n&sortBy=relevance&from=1&to=30&any=PDF"
)
PAGE_SIZE = 30
MIN_ABSTRACT_CHARS = 100
MAX_PAGES = 200
MAX_SECONDS = 25 * 60
BACKOFF = (1, 3, 9)


class EcatGaGovAuGeonetworkCrawler(BaseCrawler):
    site_id = "ecat-ga-gov-au-geonetwork"
    site_name = "Custom: ecat-ga-gov-au-geonetwork"
    base_url = "https://ecat.ga.gov.au"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        page = 0
        offset = 0
        limit_display = str(limit) if limit is not None else "inf"
        start_time = time.time()

        while True:
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page >= MAX_PAGES:
                print(f"[{self.site_id}] reached safety cap of {MAX_PAGES} pages; stopping")
                break

            hits = self._fetch_page(offset, PAGE_SIZE)
            if hits is None:
                print(f"[{self.site_id}] page {page + 1}: fetch failed; stopping")
                break
            if not hits:
                print(f"[{self.site_id}] page {page + 1}: no records returned; stopping")
                break

            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            new_this_page = 0
            for hit in hits:
                if limit is not None and saved >= limit:
                    break

                try:
                    src = hit.get("_source", {})
                    uuid = src.get("uuid") or hit.get("_id", "")
                    detail_url = (
                        f"{self.base_url}/geonetwork/srv/eng/"
                        f"catalog.search#/metadata/{uuid}"
                    )

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_this_page += 1

                    parsed = self._parse_hit(src, uuid, detail_url)
                    if parsed is None:
                        continue

                    abstract = parsed["abstract"]
                    if len(abstract) < MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] {uuid} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": detail_url,
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_display}: "
                        f"{parsed['title'][:80]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {hit.get('_id', '?')} failed: {exc}")
                    continue

            if new_this_page == 0:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break

            if len(hits) < PAGE_SIZE:
                print(f"[{self.site_id}] page {page}: partial page ({len(hits)}); done")
                break

            offset += PAGE_SIZE

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # API fetch
    # ------------------------------------------------------------------

    def _fetch_page(self, offset, size):
        body = {
            "from": offset,
            "size": size,
            "query": {
                "bool": {
                    "must": [
                        {"query_string": {"query": "PDF"}},
                        {"terms": {"isTemplate": ["n"]}},
                    ]
                }
            },
        }
        raw = self._curl_post(SEARCH_API, body, context=f"offset={offset}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON parse error at offset {offset}: {exc}")
            return None
        if not isinstance(data, dict):
            return None
        return (data.get("hits") or {}).get("hits") or []

    def _curl_post(self, url, body, context="request"):
        body_json = json.dumps(body)
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json",
            "-H", "Content-Type: application/json",
            "-d", body_json,
            url,
        ]
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"
            except Exception as exc:
                last_error = str(exc)

            print(
                f"[{self.site_id}] {context} attempt {attempt + 1}/3 failed: {last_error}"
            )
            if attempt < 2:
                time.sleep(BACKOFF[attempt])

        print(f"[{self.site_id}] {context} failed after 3 attempts")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_hit(self, src, uuid, detail_url):
        title_obj = src.get("resourceTitleObject", {})
        title = ""
        if isinstance(title_obj, dict):
            title = title_obj.get("default", "") or title_obj.get("langeng", "")
        title = self._clean_text(title)
        if not title:
            print(f"[{self.site_id}] {uuid}: no title, skipping")
            return None

        abs_obj = src.get("resourceAbstractObject", {})
        abstract_raw = ""
        if isinstance(abs_obj, dict):
            abstract_raw = abs_obj.get("default", "") or abs_obj.get("langeng", "")
        abstract = self._clean_text(abstract_raw)

        ecat_id = str(src.get("eCatId") or uuid or "").strip()

        author_raw = src.get("author", "")
        authors = [str(author_raw).strip()] if author_raw else []

        publisher = src.get("publisher", "")
        department = str(publisher).strip() if publisher else ""

        keywords_raw = src.get("keywords", [])
        keywords: list[str] = []
        if isinstance(keywords_raw, list):
            for kw in keywords_raw:
                if isinstance(kw, dict):
                    k = kw.get("keyword", "")
                    if k:
                        keywords.append(str(k).strip())
                elif isinstance(kw, str) and kw.strip():
                    keywords.append(kw.strip())

        resource_type = src.get("resourceType", [])
        if isinstance(resource_type, list) and resource_type:
            category = str(resource_type[0]).strip()
        elif isinstance(resource_type, str) and resource_type.strip():
            category = resource_type.strip()
        else:
            category = "dataset"

        published_date = self._extract_date(src)
        pdf_url = self._extract_pdf_url(src)

        doi = ""
        identifiers = src.get("resourceIdentifier", [])
        if isinstance(identifiers, list):
            for ident in identifiers:
                if isinstance(ident, dict):
                    code = str(ident.get("code", ""))
                    if "doi.org" in code or code.startswith("10."):
                        doi = code
                        break

        metadata = {
            "uuid": uuid,
            "ecat_id": ecat_id,
            "source_api": SEARCH_API,
            "start_url": START_URL,
        }

        return {
            "external_id": ecat_id or uuid,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": department,
            "metadata": metadata,
        }

    def _extract_date(self, src):
        dates = src.get("resourceDate", [])
        if not isinstance(dates, list):
            return ""
        pub_date = ""
        create_date = ""
        for entry in dates:
            if not isinstance(entry, dict):
                continue
            dtype = str(entry.get("type", "")).lower()
            normalized = self._normalize_date(str(entry.get("date", "")))
            if not normalized:
                continue
            if dtype == "publication" and not pub_date:
                pub_date = normalized
            elif dtype == "creation" and not create_date:
                create_date = normalized
        if pub_date:
            return pub_date
        if create_date:
            return create_date
        return self._normalize_date(str(src.get("publicationDateForRecord", "")))

    def _extract_pdf_url(self, src):
        links = src.get("link", [])
        if not isinstance(links, list):
            return ""
        for link in links:
            if not isinstance(link, dict):
                continue
            url_obj = link.get("urlObject", {})
            if isinstance(url_obj, dict):
                url = url_obj.get("default", "") or url_obj.get("langeng", "")
            else:
                url = str(url_obj)
            url = str(url).strip()
            if url.lower().endswith(".pdf") or ".pdf?" in url.lower():
                return url
        return ""

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_date(value):
        if not value:
            return ""
        match = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", str(value))
        if not match:
            return ""
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = str(value)
        text = html.unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#>]+", " ", text)
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
