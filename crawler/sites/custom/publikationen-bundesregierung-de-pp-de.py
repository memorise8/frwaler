# -*- coding: utf-8 -*-
"""Crawler for BMWSB publications on publikationen-bundesregierung.de."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PublikationenBundesregierungDePpDeCrawler(BaseCrawler):
    site_id = "publikationen-bundesregierung-de-pp-de"
    site_name = "Custom: publikationen-bundesregierung-de-pp-de"
    base_url = "https://www.publikationen-bundesregierung.de"

    START_URL = (
        "https://www.publikationen-bundesregierung.de/pp-de/herausgeber/"
        "bundesministerium-fuer-wohnen-stadtentwicklung-und-bauwesen-bmwsb-"
    )
    DEFAULT_LIST_ENDPOINT = "/pp-de/2277526!searchJson"
    CSRF_URL = "https://www.publikationen-bundesregierung.de/service/csrf"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    CURL_META_MARKER = "__PUBLIKATIONEN_BUNDESREGIERUNG_CURL_META__:"
    MIN_ABSTRACT_CHARS = 50
    MAX_WALL_MINUTES = 24

    DEPARTMENT = (
        "Bundesministerium fuer Wohnen, Stadtentwicklung und Bauwesen (BMWSB)"
    )
    DEPARTMENT_DISPLAY = (
        "Bundesministerium f\u00fcr Wohnen, Stadtentwicklung und Bauwesen (BMWSB)"
    )

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl BMWSB publication records from the CoreMedia search API."""
        import time as _time
        wall_start = _time.time()
        saved = 0
        item_number = 0
        seen_external_ids = set()
        seen_urls = set()

        start_raw, _ = self._curl_get_text(
            self.START_URL,
            context="start page",
            referer=self.base_url + "/pp-de/",
        )
        if not start_raw:
            print(f"[{self.site_id}] start page fetch failed")
            return saved

        start_soup = self._make_soup(start_raw, "start page")
        if start_soup is None:
            print(f"[{self.site_id}] start page parse failed")
            return saved

        list_endpoint = self._discover_list_endpoint(start_soup)
        csrf_headers = self._fetch_csrf_headers()
        print(f"[{self.site_id}] list endpoint: POST {list_endpoint}")
        print(f"[{self.site_id}] detail endpoint: publication HTML links from list payloads")

        page = 1
        page_count = None
        safety_cap = 200
        while True:
            if limit is not None and saved >= limit:
                break
            if page_count is not None and page > page_count:
                break
            if page > safety_cap:
                print(f"[{self.site_id}] safety cap of {safety_cap} pages reached, stopping.")
                break

            elapsed_min = (_time.time() - wall_start) / 60
            if elapsed_min >= self.MAX_WALL_MINUTES:
                print(f"[{self.site_id}] wall-clock budget reached ({elapsed_min:.1f}min), stopping.")
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            data = self._fetch_list_page(list_endpoint, page, csrf_headers)
            if not data:
                print(f"[{self.site_id}] list page {page} fetch/parse failed; stopping")
                break

            result = data.get("result") or {}
            page_count = self._to_int(result.get("pageCount"), page_count)
            records = self._parse_list_records(data)
            print(
                f"[{self.site_id}] list page {page}: "
                f"discovered {len(records)} records"
            )
            if not records:
                break

            new_on_page = 0
            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    external_id = record.get("external_id") or record.get("id") or ""
                    if not external_id:
                        raise RuntimeError("list record has no external id")
                    if external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(external_id)

                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("list record has no detail URL")

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    time.sleep(self.detail_delay)
                    detail_raw, effective_detail_url = self._curl_get_text(
                        detail_url,
                        context=f"item {item_number} detail",
                        referer=self.START_URL,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        f"item {item_number} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(
                        detail_soup,
                        record,
                        effective_detail_url or detail_url,
                    )
                    title = parsed.get("title") or record.get("title") or ""
                    abstract = parsed.get("abstract") or record.get("abstract") or ""
                    abstract = self._clean_text(abstract)

                    # Enrich abstract until >= 100 chars
                    if len(abstract) < 100:
                        subtitle = self._clean_text(
                            parsed.get("subtitle") or record.get("subtitle") or ""
                        )
                        if subtitle and subtitle not in abstract:
                            abstract = f"{abstract} {subtitle}".strip() if abstract else subtitle
                    if len(abstract) < 100:
                        facts = parsed.get("facts") or {}
                        extra = []
                        for key in ("Stand", "Artikelnummer", "Herausgeber", "Sprachfassung der PDF"):
                            val = facts.get(key)
                            if val:
                                extra.append(f"{key}: {val}")
                        if extra:
                            abstract = (abstract + ". " + ". ".join(extra)).strip(". ")
                    if len(abstract) < 100:
                        meta_desc = self._clean_text(parsed.get("meta_description") or "")
                        if meta_desc and meta_desc not in abstract:
                            abstract = meta_desc if len(meta_desc) >= len(abstract) else f"{abstract} {meta_desc}".strip()
                    if len(abstract) < 100 and title:
                        if title not in abstract:
                            abstract = f"{title}. {abstract}".strip(". ") if abstract else title
                    if len(abstract) < 100:
                        abstract = f"{abstract}. {self.DEPARTMENT_DISPLAY}".strip(". ")

                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    if not title:
                        raise RuntimeError("parsed record has no title")

                    category = (
                        parsed.get("category")
                        or record.get("category")
                        or "Publikation"
                    )
                    keywords = self._dedupe(
                        (parsed.get("keywords") or [])
                        + (record.get("keywords") or [])
                        + [
                            category,
                            parsed.get("language") or "",
                            self.DEPARTMENT_DISPLAY,
                        ]
                    )
                    published_date = (
                        parsed.get("published_date")
                        or self._parse_date(record.get("sort_date") or "")
                    )
                    pdf_url = parsed.get("pdf_url") or record.get("pdf_url") or ""

                    original_filename = None
                    if pdf_url:
                        m = re.search(r"/([^/?#]+\.pdf)", pdf_url, re.I)
                        if m:
                            original_filename = m.group(1)

                    metadata = {
                        "source": "CoreMedia searchJson API and publication detail HTML",
                        "start_url": self.START_URL,
                        "list_endpoint": list_endpoint,
                        "list_page": page,
                        "list_record": record,
                        "detail_url": effective_detail_url or detail_url,
                        "facts": parsed.get("facts") or {},
                        "meta_description": parsed.get("meta_description") or "",
                        "subtitle": parsed.get("subtitle") or record.get("subtitle") or "",
                        "image_url": parsed.get("image_url") or record.get("image_url") or "",
                        "original_description_url": parsed.get("original_description_url") or "",
                        "pdf_info": parsed.get("pdf_info") or record.get("pdf_info") or "",
                        "sort_date": record.get("sort_date") or "",
                    }

                    paper = {
                        "id": f"{self.site_id}:{external_id}",
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": title,
                        "authors": json.dumps([self.DEPARTMENT_DISPLAY], ensure_ascii=False),
                        "abstract": abstract,
                        "category": category,
                        "keywords": json.dumps(keywords, ensure_ascii=False),
                        "published_date": published_date,
                        "url": effective_detail_url or detail_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": self.DEPARTMENT,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {title[:90]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[publikationen-bundesregierung-de-pp-de] "
                        f"item {item_number} failed: {exc}"
                    )
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] no new items on page {page} (dedup), stopping.")
                break

            if limit is not None and saved >= limit:
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get_text(
        self,
        url,
        *,
        context,
        referer=None,
        method="GET",
        body=None,
        accept=None,
        extra_headers=None,
    ):
        headers = [
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}",
            "-H",
            "Accept-Language: de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            headers.extend(["-H", f"Referer: {referer}"])
        for name, value in (extra_headers or {}).items():
            if value:
                headers.extend(["-H", f"{name}: {value}"])

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(self.CURL_TIMEOUT),
            *headers,
        ]
        if method.upper() == "POST":
            cmd.extend(["-X", "POST"])
            if body is not None:
                cmd.extend(["--data-binary", body])
        cmd.extend(
            [
                "-w",
                "\n"
                + self.CURL_META_MARKER
                + "%{http_code}\t%{url_effective}\t%{content_type}",
                url,
            ]
        )

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                body_text, meta = self._split_curl_meta(text)
                status_code, effective_url, _content_type = meta
                if result.returncode == 0 and 200 <= status_code < 400 and body_text.strip():
                    return body_text, effective_url or url

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = (
                    f"curl exit={result.returncode} http={status_code} "
                    f"stderr={stderr[:200]}"
                )
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return "", url

    def _split_curl_meta(self, text):
        if self.CURL_META_MARKER not in text:
            return text, (0, "", "")
        body, meta_raw = text.rsplit(self.CURL_META_MARKER, 1)
        parts = meta_raw.strip().split("\t")
        status_code = self._to_int(parts[0] if parts else "", 0) or 0
        effective_url = parts[1] if len(parts) > 1 else ""
        content_type = parts[2] if len(parts) > 2 else ""
        return body, (status_code, effective_url, content_type)

    def _fetch_csrf_headers(self):
        raw, _ = self._curl_get_text(
            self.CSRF_URL,
            context="csrf",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        header_name = data.get("headerName")
        token = data.get("token")
        if header_name and token:
            return {header_name: token}
        return {}

    def _fetch_list_page(self, endpoint, page, csrf_headers):
        payload = {
            "search": {
                "query": "",
                "zipCodeCityQuery": "",
                "sortOrder": "sortDate desc",
                "page": page,
            },
            "filters": [],
        }
        headers = {"Content-Type": "application/json"}
        headers.update(csrf_headers or {})
        raw, _ = self._curl_get_text(
            endpoint,
            context=f"list page {page}",
            referer=self.START_URL,
            method="POST",
            body=json.dumps(payload, ensure_ascii=False),
            accept="application/json, text/plain, */*",
            extra_headers=headers,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] list page {page} invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] list page {page} returned non-object JSON")
            return None
        return data

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context):
        last_error = ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_error = f"{parser}: {exc}"
        print(f"[{self.site_id}] BeautifulSoup failed for {context}: {last_error}")
        return None

    def _discover_list_endpoint(self, soup):
        root = soup.select_one("#bpa-searchresultsapp-pp-react-root")
        endpoint = ""
        if root is not None:
            endpoint = root.get("data-json-base-url") or ""
        if not endpoint:
            endpoint = self.DEFAULT_LIST_ENDPOINT
        return urljoin(self.base_url, endpoint)

    def _parse_list_records(self, data):
        result = data.get("result") or {}
        subject_map = self._subject_map(data.get("filters") or [])
        records = []
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            record = {
                "id": str(item.get("id") or ""),
                "external_id": str(item.get("id") or ""),
                "sort_date": item.get("sortDate") or "",
                "subject_ids": item.get("subject") or [],
                "raw_item_keys": sorted(item.keys()),
            }
            payload = item.get("payload") or ""
            payload_parsed = self._parse_list_payload(
                payload,
                subject_map,
                record["subject_ids"],
                record["id"],
            )
            record.update(payload_parsed)
            records.append(record)
        return records

    def _subject_map(self, filters):
        mapping = {}
        for facet in filters:
            if not isinstance(facet, dict):
                continue
            for item in facet.get("items") or []:
                if not isinstance(item, dict):
                    continue
                value = item.get("value")
                display = item.get("displayName") or item.get("name")
                if value and display:
                    mapping[str(value).split(":")[-1]] = self._clean_text(display)
        return mapping

    def _parse_list_payload(self, payload, subject_map, subject_ids, external_id):
        soup = self._make_soup(payload, f"list payload {external_id}")
        if soup is None:
            return {}

        detail_link = soup.select_one("a.bpa-pub-details-pp__link[href]")
        pdf_link = soup.select_one('a.bpa-pub-details-pp__btn[href], a[href*=".pdf"]')
        image = soup.select_one("img[src]")
        title = self._select_text(soup, ".bpa-pub-details-pp__title")
        subtitle = self._select_text(soup, ".bpa-pub-details-pp__subtitle")
        label = self._select_text(soup, ".bpa-pub-details-pp__label")
        abstract = self._select_text(soup, ".bpa-pub-details-pp__text")
        pdf_info = self._select_text(soup, ".bpa-pub-details-pp__btn-subline")

        keywords = []
        for subject_id in subject_ids:
            label_text = subject_map.get(str(subject_id))
            if label_text:
                keywords.append(label_text)

        return {
            "title": self._join_title(title, subtitle),
            "short_title": title,
            "subtitle": subtitle,
            "publisher_label": label,
            "abstract": abstract,
            "url": urljoin(self.base_url, detail_link.get("href")) if detail_link else "",
            "pdf_url": urljoin(self.base_url, pdf_link.get("href")) if pdf_link else "",
            "pdf_info": pdf_info,
            "image_url": urljoin(self.base_url, image.get("src")) if image else "",
            "keywords": keywords,
        }

    def _parse_detail(self, soup, record, detail_url):
        header = soup.select_one(".bpa-pub-details-pp--single-header") or soup
        details = soup.select_one(".bpa-pub-details-pp--single") or soup

        title = self._select_text(header, ".bpa-page-header-pp__title")
        subtitle = self._select_text(header, ".bpa-page-header-pp__subtitle")
        publisher_label = self._select_text(header, ".bpa-page-header-pp__label")
        if not title:
            title = self._meta_content(soup, "og:title") or self._title_tag(soup)

        data_line = self._select_text(details, "p.bpa-pub-details-pp__data")
        category = data_line.split(",", 1)[0].strip() if data_line else ""

        facts = {}
        for block in details.select("dl.bpa-pub-details-pp__data-list div"):
            key = self._select_text(block, "dt").rstrip(":")
            value = self._select_text(block, "dd")
            if key and value:
                facts[key] = value

        time_el = details.select_one("time[datetime]") or soup.select_one("time[datetime]")
        published_date = ""
        if time_el is not None:
            published_date = self._parse_date(time_el.get("datetime") or "")
        if not published_date:
            published_date = self._parse_date(facts.get("Stand", ""))

        pdf_link = details.select_one('a.bpa-pub-details-pp__btn[href], a[href*=".pdf"]')
        pdf_url = urljoin(self.base_url, pdf_link.get("href")) if pdf_link else ""
        pdf_info = self._select_text(details, ".bpa-pub-details-pp__btn-subline")

        abstract_block = soup.select_one(".bpa-pub-details-pp__text.bpa-richtext")
        abstract = ""
        if abstract_block is not None:
            paragraphs = [
                self._clean_text(p.get_text(" ", strip=True))
                for p in abstract_block.find_all(["p", "li"])
            ]
            paragraphs = [p for p in paragraphs if p]
            abstract = " ".join(paragraphs)
        meta_description = (
            self._meta_content(soup, "description")
            or self._meta_content(soup, "og:description")
            or ""
        )
        # Prefer whichever source is longer (richtext block may be a short tagline)
        if not abstract or (meta_description and len(meta_description) > len(abstract)):
            abstract = meta_description
        if not abstract:
            abstract = record.get("abstract") or ""

        keyword_texts = [
            self._clean_text(a.get_text(" ", strip=True))
            for a in soup.select(".bpa-keywords-pp__link-wrapper .bpa-link__text")
        ]

        original_link = soup.find(
            "a",
            string=lambda text: text and "Originalbeschreibung" in text,
        )
        if original_link is None:
            original_link = soup.select_one('a[title*="Originalbeschreibung"][href]')

        image = details.select_one("img[src]")

        return {
            "title": self._join_title(title, subtitle),
            "short_title": title,
            "subtitle": subtitle,
            "publisher_label": publisher_label,
            "abstract": abstract,
            "category": category,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "pdf_info": pdf_info,
            "facts": facts,
            "language": facts.get("Sprachfassung der PDF", ""),
            "keywords": self._dedupe(keyword_texts),
            "meta_description": meta_description,
            "original_description_url": (
                urljoin(self.base_url, original_link.get("href"))
                if original_link and original_link.get("href")
                else ""
            ),
            "image_url": urljoin(self.base_url, image.get("src")) if image else "",
            "detail_url": detail_url,
        }

    # ------------------------------------------------------------------
    # Text/date utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or "")).replace("\xa0", " ")
        # strip non-printable control chars (keep space/tab/newline)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _select_text(self, soup, selector):
        node = soup.select_one(selector) if soup is not None else None
        if node is None:
            return ""
        return self._clean_text(node.get_text(" ", strip=True))

    def _meta_content(self, soup, name):
        if soup is None:
            return ""
        if name.startswith("og:"):
            node = soup.select_one(f'meta[property="{name}"]')
        else:
            node = soup.select_one(f'meta[name="{name}"]')
        return self._clean_text(node.get("content")) if node else ""

    def _title_tag(self, soup):
        if soup is None or soup.title is None:
            return ""
        return self._clean_text(soup.title.get_text(" ", strip=True))

    def _join_title(self, title, subtitle):
        title = self._clean_text(title)
        subtitle = self._clean_text(subtitle)
        if subtitle and subtitle not in title:
            return f"{title}: {subtitle}" if title else subtitle
        return title

    @staticmethod
    def _dedupe(values):
        seen = set()
        out = []
        for value in values:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if not text:
                continue
            key = text.casefold()
            if key not in seen:
                seen.add(key)
                out.append(text)
        return out

    @staticmethod
    def _to_int(value, default=None):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _parse_date(cls, raw):
        text = cls._clean_text(raw)
        if not text:
            return ""
        iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if iso_match:
            return "-".join(iso_match.groups())

        de_months = {
            "januar": "01",
            "februar": "02",
            "maerz": "03",
            "april": "04",
            "mai": "05",
            "juni": "06",
            "juli": "07",
            "august": "08",
            "september": "09",
            "oktober": "10",
            "november": "11",
            "dezember": "12",
        }
        match = re.search(r"(\d{1,2})\.\s*([^\W\d_]+)\s+(\d{4})", text)
        if match:
            day, month_name, year = match.groups()
            month = de_months.get(month_name.lower().replace("\u00e4", "ae"))
            if month:
                return f"{year}-{month}-{int(day):02d}"
        compact = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", text)
        if compact:
            year, month, day = compact.groups()
            return f"{year}-{month}-{day}"
        return ""
