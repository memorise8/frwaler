# -*- coding: utf-8 -*-
"""Crawler for DOE Directives Guidance documents.

Starting URL: https://www.directives.doe.gov/guidance

``www.directives.doe.gov`` redirects its entire domain to
``www.energy.gov/management/directives-guidance-and-delegations``. The
actual searchable dataset lives at ``/management/directives-library``: the
page embeds the *complete* directives dataset (title, native ID, effective
date, description, sponsoring office, series/areas, and a permalink to the
document) as JSON inside the Drupal ``drupal-settings-json`` script tag
(``drupalSettings.data.datatableRows``) -- there is no server-side
pagination, it is a client-side-filtered datatable. Each row's ``TYPE``
field distinguishes ``Guide`` (Guidance), ``Order``, ``Policy``, ``Manual``,
and ``Cancellation Notice``; this crawler keeps only ``TYPE == "Guide"``.

Each record's ``URL`` (``https://energy.gov/media/<id>``) redirects to a
friendly ``https://www.energy.gov/documents/<slug>`` page which itself
serves the PDF bytes directly (``content-type: application/pdf``) -- there
is no separate HTML metadata page. A HEAD request (follow redirects) is
used per item to resolve the friendly URL and look for a
Content-Disposition filename, while avoiding downloading the PDF body.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from email.message import Message
from typing import Any, Optional
from urllib.parse import unquote, urlparse

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


SITE_ID = "directives-doe-gov-guidance"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _make_soup(raw: str, *, context: str = ""):
    """Parse malformed HTML defensively: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup

    last_error = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            last_error = exc
            print(f"[{SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")
            continue
    print(f"[{SITE_ID}] all HTML parsers failed for {context}: {last_error}")
    return None


def _iso_date(raw: Any) -> Optional[str]:
    """Parse ``M/D/YYYY`` (site's native EDATE format) to ISO ``YYYY-MM-DD``."""
    if not raw:
        return None
    text = _clean_text(raw)
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _filename_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if "." in tail and 0 < len(tail) <= 220:
            return tail
    except Exception:
        return None
    return None


def _filename_from_content_disposition(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    try:
        msg = Message()
        msg["content-disposition"] = header_value
        filename = msg.get_param("filename", header="content-disposition")
        if filename:
            filename = unquote(str(filename).strip().strip('"'))
            if 0 < len(filename) <= 220:
                return filename
    except Exception:
        return None
    return None


def _json_dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _semicolons_to_commas(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parts = [p.strip() for p in value.split(";") if p.strip()]
    return ", ".join(parts) if parts else None


class DirectivesDoeGovGuidanceCrawler(BaseCrawler):
    site_id = "directives-doe-gov-guidance"
    site_name = "Custom: directives-doe-gov-guidance"
    base_url = "https://www.directives.doe.gov"

    _LIST_URL = "https://www.energy.gov/management/directives-library"
    _PUBLISHER = "U.S. Department of Energy"
    _WANTED_TYPE = "Guide"

    _SAFETY_CAP = 200
    _WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 50
    _BACKOFF_SECONDS = (1, 3, 9)
    _VIRTUAL_PAGE_SIZE = 5

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        raw = self._curl_get(self._LIST_URL, label="directives-library list page")
        if not raw:
            print(f"[{self.site_id}] failed to fetch list page; aborting")
            return saved

        records = self._parse_dataset(raw)
        if not records:
            print(f"[{self.site_id}] 0 records parsed from embedded dataset; aborting")
            return saved

        guidance_records = [r for r in records if _clean_text(r.get("TYPE")) == self._WANTED_TYPE]
        print(f"[{self.site_id}] embedded dataset: {len(records)} total rows, {len(guidance_records)} guidance rows")

        page_counter = 0
        for batch_start in range(0, len(guidance_records), self._VIRTUAL_PAGE_SIZE):
            if time.time() - start_time > self._WALL_BUDGET_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
                break
            if limit is not None and saved >= limit:
                break
            if page_counter >= self._SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP} pages reached")
                break

            page_counter += 1
            if page_counter % 10 == 0:
                print(f"[{self.site_id}] page {page_counter}: saved {saved}/{limit_or_inf}")

            batch = guidance_records[batch_start:batch_start + self._VIRTUAL_PAGE_SIZE]
            new_on_page = 0

            for record in batch:
                if limit is not None and saved >= limit:
                    break

                media_url = _clean_text(record.get("URL"))
                if not media_url or media_url in seen_urls:
                    continue
                seen_urls.add(media_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    resolved_url, content_disposition = self._resolve_detail(media_url)
                    paper = self._build_paper(record, media_url, resolved_url, content_disposition)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping '{paper.get('title', '')[:70]}' "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {media_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_counter}: no unseen records; stopping")
                break

        print(f"[{self.site_id}] done: saved {saved}")
        return saved

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, label: str = "", timeout: int = 45) -> Optional[str]:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            url,
        ]

        for attempt, wait in enumerate(self._BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                reason = stderr or f"curl exit {result.returncode}, body length {len(body)}"
                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] {label or url} failed after 3 attempts: {reason}")
                    return None
                print(f"[{self.site_id}] {label or url} attempt {attempt}/3 failed: {reason}; retrying in {wait}s")
                time.sleep(wait)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] {label or url} failed after 3 attempts: {exc}")
                    return None
                print(f"[{self.site_id}] {label or url} attempt {attempt}/3 failed: {exc}; retrying in {wait}s")
                time.sleep(wait)
        return None

    def _resolve_detail(self, media_url: str) -> tuple[str, Optional[str]]:
        """HEAD-request (following redirects) to resolve the friendly document
        URL and look for a Content-Disposition filename, without downloading
        the PDF body. Returns ``(resolved_url, content_disposition_header)``.
        On failure after retries, falls back to ``(media_url, None)``.
        """
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skIL",
            "--connect-timeout",
            "10",
            "--max-time",
            "20",
            "-A",
            self.USER_AGENT,
            "-w",
            "\nEFFECTIVE_URL:%{url_effective}\n",
            media_url,
        ]

        for attempt, wait in enumerate(self._BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
                headers = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and headers.strip():
                    resolved_url = media_url
                    content_disposition = None
                    for line in headers.splitlines():
                        if line.lower().startswith("content-disposition:"):
                            content_disposition = line.split(":", 1)[1].strip()
                        if line.startswith("EFFECTIVE_URL:"):
                            candidate = line.split(":", 1)[1].strip()
                            if candidate:
                                resolved_url = candidate
                    return resolved_url, content_disposition

                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] resolve {media_url} failed after 3 attempts")
                    return media_url, None
                time.sleep(wait)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] resolve {media_url} failed after 3 attempts: {exc}")
                    return media_url, None
                time.sleep(wait)
        return media_url, None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_dataset(self, raw: str) -> list[dict]:
        match = re.search(
            r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>',
            raw,
            re.S,
        )
        settings = None
        if match is not None:
            try:
                settings = json.loads(match.group(1))
            except Exception as exc:
                print(f"[{self.site_id}] failed to parse drupal-settings-json via regex: {exc}")
                settings = None

        if settings is None:
            # Fallback: locate the script tag through a defensive HTML parse
            # in case the regex fails to match (attribute reordering, extra
            # whitespace, malformed markup, etc).
            soup = _make_soup(raw, context=self._LIST_URL)
            if soup is not None:
                script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
                if script is not None:
                    raw_json = "".join(str(c) for c in script.contents).strip()
                    try:
                        settings = json.loads(raw_json)
                    except Exception as exc:
                        print(f"[{self.site_id}] failed to parse drupal-settings-json via soup fallback: {exc}")
                        settings = None

        if settings is None:
            print(f"[{self.site_id}] drupal-settings-json script tag not found")
            return []

        rows = self._find_datatable_rows(settings)
        if not rows:
            print(f"[{self.site_id}] datatableRows not found in embedded settings")
            return []
        return [r for r in rows if isinstance(r, dict)]

    def _find_datatable_rows(self, obj: Any) -> Optional[list]:
        if isinstance(obj, dict):
            if "datatableRows" in obj and isinstance(obj["datatableRows"], list):
                return obj["datatableRows"]
            for value in obj.values():
                found = self._find_datatable_rows(value)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = self._find_datatable_rows(value)
                if found is not None:
                    return found
        return None

    # ------------------------------------------------------------------
    # Building the paper dict
    # ------------------------------------------------------------------

    def _build_paper(
        self,
        record: dict,
        media_url: str,
        resolved_url: str,
        content_disposition: Optional[str],
    ) -> dict:
        doc_id = _clean_text(record.get("ID")) or None
        media_id_match = re.search(r"/media/(\d+)", media_url)
        media_id = media_id_match.group(1) if media_id_match else None

        external_id = doc_id or media_id or self._slug_from_url(resolved_url)
        post_number = media_id if media_id else external_id

        title = _clean_text(record.get("TITLE")) or "(untitled)"
        abstract = _clean_text(record.get("DESC"))
        edate_raw = _clean_text(record.get("EDATE")) or None
        published_date = _iso_date(edate_raw)
        listed_date = published_date

        org_raw = _clean_text(record.get("ORG")) or None
        series_raw = _clean_text(record.get("SERIES")) or None
        areas_raw = _clean_text(record.get("AREAS")) or None
        type_raw = _clean_text(record.get("TYPE")) or None

        keywords = _semicolons_to_commas(areas_raw)
        category = series_raw or type_raw

        original_filename = _filename_from_content_disposition(content_disposition)
        if not original_filename:
            original_filename = _filename_from_url(resolved_url) or _filename_from_url(media_url)

        metadata = {
            "posted_date": edate_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": series_raw,
            "volume": None,
            "issue": None,
            "media_id": media_id,
            "doc_id": doc_id,
            "org": org_raw,
            "areas": areas_raw,
            "type": type_raw,
            "list_endpoint": self._LIST_URL,
            "detail_endpoint": resolved_url,
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
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": org_raw,
            "journal": None,
            "url": resolved_url,
            "pdf_url": media_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def _slug_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        try:
            return unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or None
        except Exception:
            return None
