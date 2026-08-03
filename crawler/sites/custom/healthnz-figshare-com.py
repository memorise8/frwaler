# -*- coding: utf-8 -*-
"""Health New Zealand Figshare crawler.

Starting URL: https://healthnz.figshare.com/

The browser-facing custom domain is protected by AWS WAF for plain curl, but
the same public records are exposed through Figshare's v2 API. Health NZ's
institution_id on figshare is 1129 (discovered via Playwright portal render).
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from html import unescape
from urllib.parse import urlencode

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional dependency guard
    BeautifulSoup = None


_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    )
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_API_BASE = "https://api.figshare.com/v2"
_INSTITUTION_ID = 1129
_PAGE_SIZE = 100
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_STOP_SOON_SECS = _CRAWL_BUDGET_SECS - 60


class HealthNZFigshareComCrawler(BaseCrawler):
    site_id = "healthnz-figshare-com"
    site_name = "Custom: healthnz-figshare-com"
    base_url = "https://healthnz.figshare.com"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, accept="application/json", retries=3):
        """GET via curl with TLS options and exponential-backoff retries."""
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--fail",
            "--max-time", "45",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept}",
            url,
        ]
        delays = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                err = result.stderr.decode("utf-8", errors="replace").strip()
                if attempt < retries - 1:
                    wait = delays[min(attempt, len(delays) - 1)]
                    print(
                        f"[{self.site_id}] curl empty/error response "
                        f"(attempt {attempt + 1}/{retries}) for {url}: {err}; "
                        f"retrying in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    print(
                        f"[{self.site_id}] curl failed after {retries} attempts "
                        f"for {url}: {err}"
                    )
            except Exception as exc:
                if attempt < retries - 1:
                    wait = delays[min(attempt, len(delays) - 1)]
                    print(
                        f"[{self.site_id}] curl error "
                        f"(attempt {attempt + 1}/{retries}) for {url}: {exc}; "
                        f"retrying in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts for {url}: {exc}")
        return None

    def _fetch_json(self, url):
        raw = self._curl_get(url, accept="application/json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode error for {url}: {exc}")
            return None

    @staticmethod
    def _list_url(page):
        params = {
            "institution": _INSTITUTION_ID,
            "page": page,
            "page_size": _PAGE_SIZE,
            "order": "published_date",
            "order_direction": "desc",
        }
        return f"{_API_BASE}/articles?{urlencode(params)}"

    @staticmethod
    def _detail_url(article_id):
        return f"{_API_BASE}/articles/{article_id}"

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_soup(raw):
        """Parse HTML with html5lib first, falling back without raising."""
        if BeautifulSoup is None:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception:
                continue
        return None

    @classmethod
    def _html_to_text(cls, html):
        if not html:
            return ""
        soup = cls._safe_soup(html)
        if soup is not None:
            try:
                text = soup.get_text(" ", strip=True)
                return re.sub(r"\s+", " ", text).strip()
            except Exception:
                pass
        text = re.sub(r"<[^>]+>", " ", html)
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _iso_date(raw):
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        return None

    @staticmethod
    def _join_names(values, key=None, sep="; "):
        out = []
        for value in values or []:
            if isinstance(value, dict):
                item = value.get(key) if key else None
                if item is None:
                    item = value.get("full_name") or value.get("name") or value.get("title")
            else:
                item = value
            item = str(item or "").strip()
            if item and item not in out:
                out.append(item)
        return sep.join(out)

    @staticmethod
    def _select_pdf_file(files):
        for file_info in files or []:
            name = str(file_info.get("name") or "").strip()
            mimetype = str(file_info.get("mimetype") or "").lower()
            download_url = str(file_info.get("download_url") or "").strip()
            if download_url and (mimetype == "application/pdf" or name.lower().endswith(".pdf")):
                return file_info
        return None

    @staticmethod
    def _custom_field_value(detail, name):
        expected = name.lower()
        for field in detail.get("custom_fields") or []:
            if str(field.get("name") or "").strip().lower() == expected:
                value = field.get("value")
                if value not in (None, ""):
                    return value
        return None

    def _build_record(self, list_item, detail):
        article_id = str(detail.get("id") or list_item.get("id") or "").strip()
        if not article_id:
            raise ValueError("missing article id")

        title = str(detail.get("title") or list_item.get("title") or "").strip()
        if not title:
            raise ValueError(f"missing title for article {article_id}")

        abstract = self._html_to_text(detail.get("description") or "")
        if len(abstract) < 50:
            print(f"[{self.site_id}] skipping {article_id}: abstract too short ({len(abstract)} chars)")
            return None

        timeline = detail.get("timeline") or list_item.get("timeline") or {}
        listed_raw = (
            timeline.get("posted")
            or detail.get("published_date")
            or list_item.get("published_date")
            or detail.get("created_date")
            or list_item.get("created_date")
        )
        published_raw = (
            timeline.get("publisherPublication")
            or timeline.get("firstOnline")
            or detail.get("published_date")
            or list_item.get("published_date")
        )
        listed_date = self._iso_date(listed_raw)
        published_date = self._iso_date(published_raw) or listed_date

        authors = self._join_names(detail.get("authors"), "full_name")
        categories = detail.get("categories") or []
        category = self._join_names(categories, "title", sep="; ")
        keywords = self._join_names(detail.get("keywords") or detail.get("tags") or [], sep=", ")

        files = detail.get("files") or []
        pdf_file = self._select_pdf_file(files)
        pdf_url = pdf_file.get("download_url") if pdf_file else None
        original_filename = pdf_file.get("name") if pdf_file else None

        journal_raw = detail.get("resource_title") or self._custom_field_value(detail, "Journal")
        journal = journal_raw or None
        series = self._custom_field_value(detail, "Series")
        volume = self._custom_field_value(detail, "Volume")
        issue = self._custom_field_value(detail, "Issue")

        detail_url = (
            detail.get("figshare_url")
            or detail.get("url_public_html")
            or list_item.get("url_public_html")
            or f"{self.base_url}/articles/{article_id}"
        )

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "figshare_article_id": article_id,
            "group_id": detail.get("group_id") or list_item.get("group_id"),
            "defined_type": detail.get("defined_type") or list_item.get("defined_type"),
            "defined_type_name": detail.get("defined_type_name") or list_item.get("defined_type_name"),
            "version": detail.get("version"),
            "status": detail.get("status"),
            "created_date": detail.get("created_date") or list_item.get("created_date"),
            "modified_date": detail.get("modified_date") or list_item.get("modified_date"),
            "first_online": timeline.get("firstOnline"),
            "publisher_publication": timeline.get("publisherPublication"),
            "resource_doi": detail.get("resource_doi") or list_item.get("resource_doi"),
            "resource_title": detail.get("resource_title") or list_item.get("resource_title"),
            "license": detail.get("license"),
            "funding": detail.get("funding"),
            "funding_list": detail.get("funding_list"),
            "references": detail.get("references"),
            "related_materials": detail.get("related_materials"),
            "custom_fields": detail.get("custom_fields"),
            "files": files,
            "categories": categories,
            "tags": detail.get("tags"),
            "list_item": list_item,
            "detail": detail,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, detail_url)),
            "site_id": self.site_id,
            "external_id": article_id,
            "post_number": article_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors or None,
            "publisher": "Health New Zealand",
            "department": None,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords or None,
            "category": category or detail.get("defined_type_name") or list_item.get("defined_type_name"),
            "doi": detail.get("doi") or list_item.get("doi") or None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            elapsed = time.monotonic() - start_time
            if elapsed >= _STOP_SOON_SECS:
                print(f"[{self.site_id}] approaching 25-minute budget at page {page}, stopping cleanly.")
                break
            if limit is not None and saved >= limit:
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            data = self._fetch_json(self._list_url(page))
            if not isinstance(data, list):
                print(f"[{self.site_id}] page {page}: list fetch failed or returned non-list, stopping.")
                break
            if not data:
                print(f"[{self.site_id}] page {page}: no records, stopping.")
                break

            new_on_page = 0
            for list_item in data:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= _STOP_SOON_SECS:
                    print(f"[{self.site_id}] approaching 25-minute budget during page {page}, stopping cleanly.")
                    return saved

                item_url = (
                    list_item.get("url_public_html")
                    or list_item.get("url_public_api")
                    or list_item.get("url")
                    or str(list_item.get("id") or "")
                )
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                item_id = str(list_item.get("id") or "").strip()
                try:
                    if not item_id:
                        raise ValueError("missing list item id")

                    time.sleep(self._delay)
                    detail = self._fetch_json(self._detail_url(item_id))
                    if not isinstance(detail, dict):
                        print(f"[{self.site_id}] item {item_id} failed: detail fetch returned no JSON object")
                        continue

                    paper = self._build_record(list_item, detail)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id or item_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen, stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached, stopping.")

        print(f"[{self.site_id}] crawl complete: saved {saved} documents.")
        return saved
