# -*- coding: utf-8 -*-
"""Crawler for Cerema datasets listed on data.gouv.fr.

Discovered endpoints:
  - list:   /api/2/datasets/search/?organization={organization_id}&page={n}&page_size=20&lang=fr
  - detail: /api/1/datasets/{slug-or-id}/

Absolute import is intentional: this module is loaded via spec_from_file_location.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urlencode, urljoin

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - regex fallback keeps crawler usable
    BeautifulSoup = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_ORG_ID = "5c812a16634f416583ed1876"
_ORG_SLUG = "cerema"
_PAGE_SIZE = 20
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_TIMEOUT_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_ABSTRACT_MIN_CHARS = 100


class DataGouvFrOrganizationsCrawler(BaseCrawler):
    site_id = "data-gouv-fr-organizations"
    site_name = "Custom: data-gouv-fr-organizations"
    base_url = "https://www.data.gouv.fr"

    detail_delay = 1.0

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url: str,
        *,
        accept: str = "application/json, text/html;q=0.9, */*;q=0.8",
        retries: int = 3,
        max_time: int = 45,
    ) -> str | None:
        """GET with curl, TLS cap, retries, and forgiving UTF-8 decode."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "-f",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            url,
        ]
        waits = (1, 3, 9)
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl GET failed "
                    f"(attempt {attempt + 1}/{retries}) for {url}: "
                    f"code={result.returncode} {err[:200]}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl GET error "
                    f"(attempt {attempt + 1}/{retries}) for {url}: {exc}"
                )
            if attempt < retries - 1:
                time.sleep(waits[attempt])
        return None

    def _curl_json(self, url: str) -> dict | None:
        raw = self._curl_get(url, accept="application/json, */*")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode failed for {url}: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] Unexpected JSON type for {url}: {type(data).__name__}")
            return None
        return data

    # ------------------------------------------------------------------
    # Text / field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(raw: str):
        """Build BeautifulSoup with the required parser fallback chain."""
        if BeautifulSoup is None:
            return None
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            print(f"[data-gouv-fr-organizations] BeautifulSoup failed: {last_exc}")
        return None

    @classmethod
    def _clean_text(cls, value) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            value = "\n".join(str(v) for v in value if v is not None)
        text = str(value)
        if "<" in text and ">" in text:
            soup = cls._make_soup(text)
            if soup is not None:
                text = soup.get_text(" ")
            else:
                text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = text.replace("\\[", "[").replace("\\]", "]")
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
        text = re.sub(r"(?m)^\s{0,3}(>\s*)+", "", text)
        text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
        text = re.sub(r"[*_`]+", "", text)
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _date_only(value) -> str | None:
        if value in (None, ""):
            return None
        match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
        return match.group(0) if match else None

    @staticmethod
    def _join_unique(values, sep: str) -> str | None:
        seen = set()
        out = []
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return sep.join(out) if out else None

    @staticmethod
    def _resource_format(resource: dict) -> str:
        return " ".join(
            str(resource.get(k) or "").lower()
            for k in ("format", "mime", "title", "url")
        )

    @classmethod
    def _is_pdf_resource(cls, resource: dict) -> bool:
        fmt = cls._resource_format(resource)
        return "pdf" in fmt or "application/pdf" in fmt

    @classmethod
    def _pick_pdf_resource(cls, resources) -> dict | None:
        if not isinstance(resources, list):
            return None
        pdfs = [r for r in resources if isinstance(r, dict) and cls._is_pdf_resource(r)]
        if not pdfs:
            return None

        def score(resource: dict) -> tuple[int, int]:
            url = str(resource.get("url") or "")
            title = str(resource.get("title") or "")
            fmt = str(resource.get("format") or "").lower()
            mime = str(resource.get("mime") or "").lower()
            direct_pdf = url.lower().split("?", 1)[0].endswith(".pdf")
            named_pdf = title.lower().endswith(".pdf")
            exact_pdf = fmt == "pdf" or mime == "application/pdf"
            return (int(direct_pdf) + int(named_pdf) + int(exact_pdf), int(resource.get("filesize") or 0))

        return max(pdfs, key=score)

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = unquote(url.rstrip("/").split("/")[-1].split("?", 1)[0].split("#", 1)[0])
        if "." in tail and len(tail) <= 255:
            return tail
        return None

    @classmethod
    def _resource_filename(cls, resource: dict | None) -> str | None:
        if not isinstance(resource, dict):
            return None
        for key in ("url", "latest"):
            filename = cls._filename_from_url(resource.get(key))
            if filename:
                return filename
        title = str(resource.get("title") or "").strip()
        return title if "." in title and len(title) <= 255 else None

    @staticmethod
    def _topic_values(value) -> list[str]:
        if not isinstance(value, list):
            return []
        topics = []
        for item in value:
            if isinstance(item, dict):
                topics.append(item.get("name") or item.get("title") or item.get("id"))
            else:
                topics.append(item)
        return [str(t).strip() for t in topics if str(t or "").strip()]

    @staticmethod
    def _contact_names(detail: dict) -> list[str]:
        names = []
        owner = detail.get("owner")
        if isinstance(owner, dict):
            names.append(owner.get("name") or " ".join(
                p for p in [owner.get("first_name"), owner.get("last_name")] if p
            ))
        for cp in detail.get("contact_points") or []:
            if isinstance(cp, dict):
                names.append(cp.get("name"))
        return names

    # ------------------------------------------------------------------
    # API parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        query = urlencode(
            {
                "organization": _ORG_ID,
                "page": page,
                "page_size": _PAGE_SIZE,
                "lang": "fr",
            }
        )
        return f"{self.base_url}/api/2/datasets/search/?{query}"

    def _api_page(self, page: int) -> tuple[list[dict], str | None]:
        data = self._curl_json(self._list_url(page))
        if not data:
            return [], None
        items = data.get("data") or []
        if not isinstance(items, list):
            print(f"[{self.site_id}] Unexpected page data type: {type(items).__name__}")
            return [], None
        return items, data.get("next_page")

    def _detail_url(self, item: dict) -> str | None:
        uri = item.get("uri")
        if uri:
            return urljoin(self.base_url, str(uri))
        slug_or_id = item.get("slug") or item.get("id")
        if slug_or_id:
            return f"{self.base_url}/api/1/datasets/{slug_or_id}/"
        return None

    def _fetch_detail(self, item: dict) -> dict | None:
        url = self._detail_url(item)
        if not url:
            return None
        return self._curl_json(url)

    def _build_record(self, list_item: dict, detail: dict) -> dict | None:
        title = self._clean_text(detail.get("title") or list_item.get("title"))
        if not title:
            return None

        abstract = self._clean_text(
            detail.get("description")
            or detail.get("description_short")
            or list_item.get("description")
            or list_item.get("description_short")
        )
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{self.site_id}] skip short abstract ({len(abstract)} chars): {title[:80]}")
            return None

        external_id = str(detail.get("id") or list_item.get("id") or "").strip()
        slug = str(detail.get("slug") or list_item.get("slug") or external_id).strip()
        if not external_id:
            external_id = slug
        post_number = slug or external_id or None

        created_raw = detail.get("created_at") or list_item.get("created_at")
        listed_raw = (
            list_item.get("last_update")
            or detail.get("last_update")
            or list_item.get("last_modified")
            or detail.get("last_modified")
            or created_raw
        )
        published_date = self._date_only(created_raw)
        listed_date = self._date_only(listed_raw)

        org = detail.get("organization") if isinstance(detail.get("organization"), dict) else {}
        publisher = self._join_unique(
            [
                org.get("name"),
                org.get("acronym"),
            ],
            "; ",
        )
        authors = self._join_unique(self._contact_names(detail), "; ")

        resources = detail.get("resources")
        pdf_resource = self._pick_pdf_resource(resources)
        pdf_url = None
        original_filename = None
        if pdf_resource is not None:
            pdf_url = pdf_resource.get("url") or pdf_resource.get("latest")
            original_filename = self._resource_filename(pdf_resource)

        tags = detail.get("tags") or list_item.get("tags") or []
        keywords = self._join_unique(tags if isinstance(tags, list) else [], ", ")
        topics = self._topic_values(detail.get("topics") or list_item.get("topics"))
        category = self._join_unique(topics, ", ") or detail.get("frequency") or list_item.get("frequency")

        extras = detail.get("extras") if isinstance(detail.get("extras"), dict) else {}
        doi = extras.get("doi") or extras.get("DOI") or extras.get("dct:identifier")
        journal_raw = extras.get("journal") or extras.get("journal_raw")
        series = extras.get("series")
        volume = extras.get("volume")
        issue = extras.get("issue")

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "dataset_id": external_id,
            "slug": slug,
            "post_number": post_number,
            "api_uri": detail.get("uri") or list_item.get("uri"),
            "list_api": self._list_url(1),
            "detail_api": self._detail_url(list_item),
            "node_id": external_id,
            "organization_id": org.get("id"),
            "organization_slug": org.get("slug") or _ORG_SLUG,
            "created_at": created_raw,
            "last_modified": detail.get("last_modified") or list_item.get("last_modified"),
            "last_update": detail.get("last_update") or list_item.get("last_update"),
            "frequency": detail.get("frequency") or list_item.get("frequency"),
            "frequency_date": detail.get("frequency_date") or list_item.get("frequency_date"),
            "license": detail.get("license") or list_item.get("license"),
            "spatial": detail.get("spatial") or list_item.get("spatial"),
            "temporal_coverage": detail.get("temporal_coverage") or list_item.get("temporal_coverage"),
            "quality": detail.get("quality") or list_item.get("quality"),
            "metrics": detail.get("metrics") or list_item.get("metrics"),
            "extras": extras,
            "selected_pdf_resource": pdf_resource,
            "raw_list": list_item,
            "raw_detail": detail,
        }

        return {
            "id": external_id,
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
            "department": None,
            "journal": journal_raw,
            "url": detail.get("page") or list_item.get("page"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Public crawl entrypoint
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        limit_n = float("inf") if limit is None else int(limit)
        limit_label = "inf" if limit is None else str(limit)
        seen_urls: set[str] = set()
        started = time.time()

        page = 1
        while saved < limit_n:
            if time.time() - started > _CRAWL_TIMEOUT_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached; stopping cleanly.")
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")
                break
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            items, next_page = self._api_page(page)
            if not items:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping.")
                break

            new_on_page = 0
            for index, item in enumerate(items, start=1):
                if saved >= limit_n:
                    break

                item_url = str(item.get("page") or item.get("uri") or item.get("slug") or item.get("id") or "")
                if not item_url:
                    print(f"[{self.site_id}] item {index} on page {page} has no URL/id; skipping.")
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    detail = self._fetch_detail(item)
                    if not detail:
                        print(f"[{self.site_id}] item {page}-{index} failed: empty detail")
                        continue
                    record = self._build_record(item, detail)
                    if record is None:
                        continue
                    self._save_paper(record)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page}-{index} failed: {exc}")
                    continue
                finally:
                    time.sleep(self.detail_delay)

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page} had no unseen records; stopping.")
                break
            if not next_page:
                print(f"[{self.site_id}] no next page after page {page}; stopping.")
                break
            page += 1

        return saved
