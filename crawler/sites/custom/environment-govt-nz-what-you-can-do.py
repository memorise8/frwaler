# -*- coding: utf-8 -*-
"""Crawler for Environment NZ "Have your say" consultation records.

The landing page at environment.govt.nz is protected by Incapsula from this
runtime, but it links to the Ministry's Citizen Space consultation hub.  The
stable full-depth endpoints are:

* list:   https://consult.environment.govt.nz/consultation_finder/?b_start:int=0
* detail: https://consult.environment.govt.nz/api/2.4/json_consultation_details
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EnvironmentGovtNzWhatYouCanDoCrawler(BaseCrawler):
    site_id = "environment-govt-nz-what-you-can-do"
    site_name = "Custom: environment-govt-nz-what-you-can-do"
    base_url = "https://environment.govt.nz"

    START_URL = "https://environment.govt.nz/what-you-can-do/have-your-say/"
    CONSULT_BASE_URL = "https://consult.environment.govt.nz"
    LIST_URL = CONSULT_BASE_URL + "/consultation_finder/"
    DETAIL_API_URL = CONSULT_BASE_URL + "/api/2.4/json_consultation_details"

    MAX_PAGES = 200
    WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    DEADLINE_MARGIN_SECONDS = 60
    MIN_ABSTRACT_CHARS = 50
    BACKOFF_SECONDS = (1, 3, 9)

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network and parsing helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, timeout: int = 45) -> Optional[str]:
        """Fetch text with curl, using the required TLS flags and retries."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-NZ,en;q=0.9",
            url,
        ]

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
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
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except OSError as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{self.site_id}] fetch failed for {url} "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts for {url}: {last_error}")
        return None

    def _make_soup(self, raw: str) -> BeautifulSoup:
        """Build a BeautifulSoup tree with a tolerant parser fallback chain."""
        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_error = exc
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
        print(f"[{self.site_id}] all HTML parsers failed: {last_error}")
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = html.unescape(str(value or ""))
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _html_to_text(self, raw_html: str) -> str:
        soup = self._make_soup(raw_html or "")
        for unwanted in soup.select("script, style, noscript"):
            unwanted.decompose()
        return self._clean_text(soup.get_text(" "))

    @staticmethod
    def _parse_date(value: Optional[str]) -> str:
        if not value:
            return ""
        raw = html.unescape(str(value)).strip()
        raw = re.sub(r"^(Open|Opened|Closed|Close|Forthcoming)\s+", "", raw, flags=re.I)
        raw = raw.replace("\xa0", " ")

        match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", raw)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass

        match = re.search(
            r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4})",
            raw,
            re.I,
        )
        if match:
            candidate = match.group(1).replace("Sept", "Sep")
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(candidate, fmt).date().isoformat()
                except ValueError:
                    pass
        return ""

    def _normalise_consultation_url(self, url: str) -> str:
        absolute = urljoin(self.CONSULT_BASE_URL, url or "")
        parsed = urlparse(absolute)
        path = parsed.path
        path = re.sub(r"/consult_view/?$", "/", path)
        path = re.sub(r"/consultation/?$", "/", path)
        if not path.endswith("/"):
            path += "/"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    @staticmethod
    def _dept_and_native_id(url: str) -> tuple[str, str]:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) < 2:
            return "", ""
        return parts[0], parts[1]

    def _list_url(self, offset: int) -> str:
        return f"{self.LIST_URL}?b_start:int={offset}"

    def _extract_next_url(self, raw: str) -> Optional[str]:
        soup = self._make_soup(raw)
        next_link = soup.find("a", attrs={"aria-label": "Next page"})
        if not next_link or next_link.get("aria-disabled") == "true":
            return None
        href = next_link.get("href") or ""
        if not href:
            return None
        absolute = urljoin(self.CONSULT_BASE_URL, href)

        # Citizen Space sometimes emits both ``b_start:int=0`` and
        # ``b_start=30`` in the same next link.  If fetched verbatim the
        # typed zero can win and the paginator loops/empties; the untyped
        # value is the effective next offset shown in the UI.
        parsed = urlparse(absolute)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if params.get("b_start"):
            return f"{self.LIST_URL}?{urlencode({'b_start': params['b_start']})}"
        return absolute

    def _parse_list_records(self, raw: str, list_url: str) -> List[Dict[str, str]]:
        soup = self._make_soup(raw)
        cards = soup.select("#consultations > li[data-consultation-state]")
        if not cards:
            cards = soup.select("li.dss-card")

        records: List[Dict[str, str]] = []
        for card in cards:
            link = card.select_one("h2 a[href]")
            if not link:
                continue
            title = self._clean_text(link.get_text(" "))
            url = self._normalise_consultation_url(link.get("href") or "")
            if not title or not url:
                continue

            date_node = card.select_one(".cs-date-delta")
            listed_date_raw = self._clean_text(date_node.get_text(" ")) if date_node else ""
            overview_node = card.select_one(".col-md-9 > span")
            overview = self._clean_text(overview_node.get_text(" ")) if overview_node else ""
            state = card.get("data-consultation-state") or ""
            dept, native_id = self._dept_and_native_id(url)

            records.append(
                {
                    "title": title,
                    "url": url,
                    "dept": dept,
                    "native_id": native_id,
                    "state": state,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": self._parse_date(listed_date_raw),
                    "overview": overview,
                    "source_list_url": list_url,
                }
            )
        return records

    def _fetch_detail(self, record: Dict[str, str]) -> Optional[Dict[str, Any]]:
        dept = record.get("dept") or ""
        native_id = record.get("native_id") or ""
        if not dept or not native_id:
            print(f"[{self.site_id}] item {record.get('url', '?')} failed: missing dept/id")
            return None
        query = urlencode({"dept": dept, "id": native_id, "fields": "all"})
        api_url = f"{self.DETAIL_API_URL}?{query}"
        raw = self._curl_get(api_url, timeout=45)
        if not raw:
            return None
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON parse failed for {api_url}: {exc}")
            return None
        if not isinstance(detail, dict) or not detail.get("id"):
            print(f"[{self.site_id}] detail API returned no record for {api_url}")
            return None
        detail["_detail_api_url"] = api_url
        return detail

    def _extract_links_from_html(self, raw_html: str) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        soup = self._make_soup(raw_html or "")
        for link in soup.find_all("a", href=True):
            href = urljoin(self.CONSULT_BASE_URL, link.get("href") or "")
            title = self._clean_text(link.get_text(" ")) or href.rsplit("/", 1)[-1]
            if href:
                links.append({"url": href, "title": title})
        return links

    def _all_detail_links(self, detail: Dict[str, Any]) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        for item in detail.get("supporting_documents") or []:
            if isinstance(item, dict) and item.get("url"):
                links.append(
                    {
                        "url": urljoin(self.CONSULT_BASE_URL, item.get("url") or ""),
                        "title": self._clean_text(item.get("title") or ""),
                        "size": self._clean_text(item.get("size") or ""),
                    }
                )
        for field in ("overview", "why", "what_happens_next"):
            links.extend(self._extract_links_from_html(detail.get(field) or ""))

        deduped: List[Dict[str, str]] = []
        seen = set()
        for item in links:
            url = item.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(item)
        return deduped

    @staticmethod
    def _filename_from_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        if not tail or "." not in tail:
            return None
        try:
            from urllib.parse import unquote

            tail = unquote(tail)
        except Exception:
            pass
        return tail[:200]

    def _pick_pdf(self, links: List[Dict[str, str]]) -> tuple[Optional[str], Optional[str]]:
        for item in links:
            url = item.get("url") or ""
            if urlparse(url).path.lower().endswith(".pdf"):
                return url, self._filename_from_url(url)
        return None, None

    @staticmethod
    def _field_labels(values: Any) -> List[str]:
        labels: List[str] = []
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    label = item.get("name") or item.get("title") or item.get("id")
                else:
                    label = item
                if label and str(label).strip():
                    labels.append(str(label).strip())
        return labels

    def _build_abstract(self, detail: Dict[str, Any], record: Dict[str, str]) -> str:
        parts: List[str] = []
        for field in ("overview", "why", "what_happens_next"):
            text = self._html_to_text(detail.get(field) or "")
            if text:
                parts.append(text)
        if record.get("overview"):
            parts.append(record["overview"])

        deduped: List[str] = []
        seen = set()
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(part)
        return self._clean_text(" ".join(deduped))

    def _make_paper(self, detail: Dict[str, Any], record: Dict[str, str]) -> Optional[Dict[str, Any]]:
        title = self._clean_text(detail.get("title") or record.get("title"))
        canonical_url = self._normalise_consultation_url(detail.get("url") or record.get("url") or "")
        dept = self._clean_text(detail.get("dept") or record.get("dept"))
        native_id = self._clean_text(detail.get("id") or record.get("native_id"))
        external_id = f"{dept}/{native_id}" if dept and native_id else native_id
        post_number = native_id or external_id or None

        if not title or not canonical_url or not external_id:
            print(f"[{self.site_id}] skipping malformed record: {record.get('url', '?')}")
            return None

        abstract = self._build_abstract(detail, record)
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] short abstract for {canonical_url} "
                f"({len(abstract)} chars); skipping"
            )
            return None

        start_date = self._parse_date(detail.get("startdate") or "")
        end_date = self._parse_date(detail.get("enddate") or "") or record.get("listed_date") or ""
        listed_date_raw = record.get("listed_date_raw") or ""
        if not listed_date_raw and detail.get("enddate"):
            listed_date_raw = f"{detail.get('status', '').title()} {detail.get('enddate')}"

        links = self._all_detail_links(detail)
        pdf_url, original_filename = self._pick_pdf(links)

        contacts = [
            self._clean_text(detail.get("contact_name") or ""),
            self._clean_text(detail.get("contact_team") or ""),
        ]
        authors = [c for c in contacts if c]
        if not authors:
            authors = ["Ministry for the Environment"]

        department = self._clean_text(
            detail.get("workspace_title") or detail.get("department") or dept
        )
        publisher = "Ministry for the Environment"

        keyword_values = [
            department,
            self._clean_text(detail.get("status") or ""),
            self._clean_text(detail.get("type_string") or ""),
        ]
        keyword_values.extend(self._field_labels(detail.get("audiences")))
        keyword_values.extend(self._field_labels(detail.get("interests")))
        keywords = []
        for value in keyword_values:
            value = self._clean_text(value)
            if value and value not in keywords:
                keywords.append(value)

        category = department or "Consultation"
        metadata = {
            "posted_date": listed_date_raw,
            "listed_date": end_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "consultation_id": native_id,
            "native_id": native_id,
            "node_id": native_id,
            "workspace_id": detail.get("workspace_id") or dept,
            "dept": dept,
            "post_number": post_number,
            "status": detail.get("status"),
            "progress": detail.get("progress"),
            "type": detail.get("type"),
            "type_string": detail.get("type_string"),
            "startdate": detail.get("startdate"),
            "enddate": detail.get("enddate"),
            "feedbackdate": detail.get("feedbackdate") or detail.get("resultdate"),
            "resultsdate": detail.get("resultsdate") or detail.get("resultdate"),
            "contact_name": detail.get("contact_name"),
            "contact_team": detail.get("contact_team"),
            "contact_email": detail.get("contact_email"),
            "participate_url": detail.get("participate_url"),
            "consult_view_url": detail.get("url"),
            "detail_api_url": detail.get("_detail_api_url"),
            "source_start_url": self.START_URL,
            "source_list_url": record.get("source_list_url"),
            "list_record": record,
            "links": links,
            "supporting_documents": detail.get("supporting_documents") or [],
            "related_links": detail.get("related_links") or [],
            "related_consultations": detail.get("related_consultations") or [],
            "raw": {k: v for k, v in detail.items() if not k.startswith("_")},
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, canonical_url)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": start_date,
            "listed_date": end_date,
            "posted_date": end_date,
            "authors": "; ".join(authors),
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords),
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        list_url = self._list_url(0)
        limit_or_inf = limit if limit is not None else "inf"
        started = time.monotonic()

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - started
            if elapsed >= self.WALL_CLOCK_SECONDS - self.DEADLINE_MARGIN_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            raw = self._curl_get(list_url, timeout=45)
            if not raw:
                print(f"[{self.site_id}] page {page}: empty list response; stopping")
                break

            records = self._parse_list_records(raw, list_url)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_records: List[Dict[str, str]] = []
            for record in records:
                url = record.get("url") or ""
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            for idx, record in enumerate(new_records, start=1):
                if limit is not None and saved >= limit:
                    break
                elapsed = time.monotonic() - started
                if elapsed >= self.WALL_CLOCK_SECONDS - self.DEADLINE_MARGIN_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                    return saved

                item_label = record.get("native_id") or record.get("url") or str(idx)
                try:
                    time.sleep(self.detail_delay)
                    detail = self._fetch_detail(record)
                    if not detail:
                        print(f"[{self.site_id}] item {item_label} failed: empty detail")
                        continue
                    paper = self._make_paper(detail, record)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            next_url = self._extract_next_url(raw)
            if not next_url:
                print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                break
            list_url = next_url
            page += 1

        if page > self.MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        return saved
