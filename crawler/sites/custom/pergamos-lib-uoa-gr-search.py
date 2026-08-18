# -*- coding: utf-8 -*-
"""Crawler for Pergamos (National and Kapodistrian University of Athens
institutional repository) — search results for articles, conference papers,
and graduate/postgraduate theses.

Pergamos's search page (https://pergamos.lib.uoa.gr/search) is a statically
exported Next.js app; the actual result data comes from a JSON POST endpoint
at ``/public-api/search`` (discovered via browser network inspection). That
endpoint returns full item records — title, abstract, authors, dates, and
the attached PDF file id — in a single call, so no separate per-item detail
fetch is needed.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PergamosLibUoaGrSearchCrawler(BaseCrawler):
    site_id = "pergamos-lib-uoa-gr-search"
    site_name = "Custom: pergamos-lib-uoa-gr-search"
    base_url = "https://pergamos.lib.uoa.gr"
    DELIVERY_ORDER = "newest_first"

    SEARCH_URL = "https://pergamos.lib.uoa.gr/public-api/search"
    TARGET_TYPES = (
        "ScientificPublicationArticle",
        "ScientificPublicationInProceedings",
        "BornDigitalPostgraduateThesis",
        "BornDigitalGraduateThesis",
        "ConferencePaper",
    )
    SORTING = ["timestamps.modifiedAt:desc"]

    PAGE_SIZE = 50
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        page = (self.delivery_cursor or {}).get("page", 0)
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed > self.WALL_CLOCK_BUDGET_SECONDS:
                print(f"[{self.site_id}] wall-clock budget of {self.WALL_CLOCK_BUDGET_SECONDS}s "
                      f"exceeded at page {page}; stopping cleanly")
                break

            if page >= self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {self.SAFETY_PAGE_CAP} pages reached; stopping")
                break

            payload = {
                "start": page * self.PAGE_SIZE,
                "count": self.PAGE_SIZE,
                "models": list(self.TARGET_TYPES),
                "fields": [],
                "strict": False,
                "query": {
                    "filters": {"in": {"publicType": list(self.TARGET_TYPES)}},
                    "aggregations": [],
                    "sorting": self.SORTING,
                },
            }
            data = self._fetch_json_post(self.SEARCH_URL, payload, context=f"list page {page}")
            if data is None:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            items = data.get("items") if isinstance(data, dict) else None
            if not items:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            new_count = 0
            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"{page}.{idx}"
                try:
                    if not isinstance(item, dict) or not item.get("id"):
                        raise RuntimeError("record has no item id")

                    store = item.get("storeName") or "uoadl"
                    url = f"{self.base_url}/item/{store}:{item['id']}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_count += 1

                    parsed = self._parse_item(item, url, store)

                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(f"[{self.site_id}] item {item_label} skipped: "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    paper = dict(parsed)
                    paper["metadata"] = json.dumps(parsed["metadata"], ensure_ascii=False)

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            self._advance_cursor({"page": page + 1}, items_done=len(items))

            if new_count == 0:
                print(f"[{self.site_id}] page {page}: all {len(items)} records already seen; stopping")
                break

            if page % 10 == 0:
                limit_label = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            if limit is not None and saved >= limit:
                break

            page += 1
            time.sleep(self.detail_delay)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_post_json(self, url, payload, context="request"):
        body = json.dumps(payload)
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json",
            "-X",
            "POST",
            "--data-binary",
            "@-",
            url,
        ]

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, input=body.encode("utf-8"), capture_output=True, timeout=55
                )
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
            if attempt < 3:
                wait = self.BACKOFF_SECONDS[attempt - 1]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _fetch_json_post(self, url, payload, context):
        raw = self._curl_post_json(url, payload, context=context)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} returned invalid JSON: {exc}")
            return None

    # ------------------------------------------------------------------
    # Item parsing
    # ------------------------------------------------------------------

    def _parse_item(self, item, url, store):
        item_id = item["id"]

        title = self._ml_pick(item.get("title")) or self._ml_pick(item.get("translatedTitle"))
        title = self._one_line(self._strip_html(title)) if title else None
        if not title:
            raise RuntimeError("item has no title")

        abstract = self._extract_abstract(item)

        authors = self._extract_authors(item)
        department, publisher = self._extract_department_publisher(item)
        journal = self._extract_journal(item)
        category = self._extract_category(item)
        keywords = self._extract_keywords(item)

        doi_raw = item.get("doi")
        doi = doi_raw.strip() if isinstance(doi_raw, str) and doi_raw.strip() else None

        pdf_url, original_filename = self._extract_pdf(item, store)

        published_date = self._extract_published_date(item)
        published_at_ms = item.get("timestamps.publishedAt")
        listed_date = self._ms_to_iso_date(published_at_ms)
        posted_date_raw = self._ms_to_iso_datetime(published_at_ms)

        post_number = str(item_id)

        journal_raw = None
        journal_obj = item.get("journal")
        if isinstance(journal_obj, dict):
            journal_raw = {
                k: v for k, v in {
                    "title": journal,
                    "issn": journal_obj.get("issn"),
                }.items() if v
            }

        metadata = {
            "posted_date": posted_date_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw or None,
            "series": None,
            "volume": item.get("volume"),
            "issue": item.get("number"),
            "uoadl_id": item_id,
            "publicType": item.get("publicType"),
            "storeName": store,
            "professors": item.get("professors") if isinstance(item.get("professors"), str) else None,
            "pageRange": item.get("pageRange"),
            "numberOfPages": item.get("numberOfPages"),
            "bibNumber": item.get("bibNumber"),
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": f"{self.site_id}-{post_number}",
            "site_id": self.site_id,
            "external_id": post_number,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": publisher,
            "department": department,
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _extract_abstract(self, item):
        text = self._ml_pick(item.get("abstractText"))
        if not text:
            text = self._ml_pick(item.get("description"))
        if not text:
            return ""
        text = self._strip_html(text)
        return self._clean_text(text)

    def _extract_authors(self, item):
        raw = item.get("authors")
        if isinstance(raw, str) and raw.strip():
            parsed = self._parse_author_string(raw)
            if parsed:
                return parsed

        for key in ("creator", "contributor", "casAuthors"):
            entries = item.get(key)
            if not isinstance(entries, list) or not entries:
                continue
            names = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = self._ml_pick(entry.get("displayTitle") or entry.get("title"))
                if not name and key == "casAuthors":
                    name = self._ml_pick(entry.get("cas.displayName"))
                if name:
                    names.append(self._one_line(name))
            names = self._dedupe(names)
            if names:
                return names
        return []

    def _parse_author_string(self, raw):
        raw = self._clean_text(raw)
        if not raw:
            return []
        if "\n" in raw:
            lines = [self._format_author_name(self._one_line(l).strip(" ;,")) for l in raw.split("\n")]
            return self._dedupe([l for l in lines if l])
        if ";" in raw:
            parts = [self._format_author_name(self._one_line(p)) for p in raw.split(";")]
            return self._dedupe([p for p in parts if p])
        parts = [self._one_line(p) for p in raw.split(",")]
        parts = [p for p in parts if p]
        if len(parts) >= 2 and len(parts) % 2 == 0:
            names = []
            for i in range(0, len(parts), 2):
                surname, given = parts[i], parts[i + 1]
                names.append(f"{given} {surname}".strip())
            return self._dedupe(names)
        return self._dedupe([raw]) if raw else []

    def _extract_department_publisher(self, item):
        department = None
        publisher_from_unit = None
        unit_list = item.get("unit")
        if isinstance(unit_list, list) and unit_list:
            u0 = unit_list[0]
            if isinstance(u0, dict):
                department = self._ml_pick(u0.get("title") or u0.get("displayTitle"))
                publisher_from_unit = self._unit_root_title(u0)

        publisher = None
        publisher_entities = item.get("publisher")
        if isinstance(publisher_entities, list) and publisher_entities:
            names = self._dedupe([
                self._ml_pick(p.get("displayTitle") or p.get("title"))
                for p in publisher_entities if isinstance(p, dict)
            ])
            if names:
                publisher = "; ".join(names)
        if not publisher:
            publisher = publisher_from_unit
        return department, publisher

    def _unit_root_title(self, unit_obj, max_depth=10):
        node = unit_obj
        depth = 0
        while isinstance(node, dict) and node.get("parents") and depth < max_depth:
            parents = node["parents"]
            if not parents or not isinstance(parents[0], dict):
                break
            node = parents[0]
            depth += 1
        if isinstance(node, dict):
            return self._ml_pick(node.get("title") or node.get("displayTitle"))
        return None

    def _extract_journal(self, item):
        journal_obj = item.get("journal")
        if isinstance(journal_obj, dict):
            return self._ml_pick(journal_obj.get("displayTitle") or journal_obj.get("title"))
        return None

    def _extract_category(self, item):
        main_subject = item.get("mainSubject")
        if isinstance(main_subject, list) and main_subject:
            main_subject = main_subject[0]
        if isinstance(main_subject, dict):
            name = self._ml_pick(main_subject.get("displayTitle") or main_subject.get("title"))
            if name:
                return name

        other = item.get("otherSubjects")
        if isinstance(other, list):
            for entry in other:
                if isinstance(entry, dict):
                    name = self._ml_pick(entry.get("displayTitle") or entry.get("title"))
                    if name:
                        return name
        return item.get("publicType")

    def _extract_keywords(self, item):
        kw = self._ml_pick(item.get("keywords"))
        if kw:
            return kw
        other = item.get("otherSubjects")
        if isinstance(other, list) and other:
            names = []
            for entry in other:
                if isinstance(entry, dict):
                    name = self._ml_pick(entry.get("displayTitle") or entry.get("title"))
                    if name:
                        names.append(name)
            names = self._dedupe(names)
            if names:
                return ", ".join(names)
        return None

    def _extract_pdf(self, item, store):
        for key in ("theFile", "pdfFile", "file"):
            f = item.get(key)
            if isinstance(f, dict) and f.get("id"):
                file_store = f.get("storeName") or store
                pdf_url = f"{self.base_url}/public-api/files/{file_store}/{f['id']}/payload-inline"
                filename = f.get("fileName") or None
                return pdf_url, filename
        return None, None

    def _extract_published_date(self, item):
        for key in ("publicationDate", "creationYear", "depositDate"):
            date_iso = self._date_range_to_iso(item.get(key))
            if date_iso:
                return date_iso
        return self._ms_to_iso_date(item.get("timestamps.publishedAt"))

    # ------------------------------------------------------------------
    # Multi-language / generic value helpers
    # ------------------------------------------------------------------

    def _ml_pick(self, value, prefer=("en", "el")):
        """Best-effort extraction of a human-readable string from Pergamos's
        assorted value shapes: plain strings, ``[{"key": lang, "value": v}]``
        multi-language lists, or nested ``{"displayTitle": [...]}`` objects.
        Literal ``"null"`` string placeholders (seen for missing languages)
        are filtered out.
        """
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            if not v or v.lower() == "null":
                return None
            return v
        if isinstance(value, dict):
            for key in ("displayTitle", "title", "term", "name", "value"):
                if key in value:
                    got = self._ml_pick(value[key], prefer)
                    if got:
                        return got
            return None
        if isinstance(value, list):
            by_lang = {}
            plain = []
            for entry in value:
                if isinstance(entry, dict) and "key" in entry and "value" in entry:
                    val = entry.get("value")
                    if isinstance(val, str):
                        val = val.strip()
                        if val and val.lower() != "null":
                            by_lang.setdefault(entry["key"], val)
                elif isinstance(entry, str):
                    v = entry.strip()
                    if v and v.lower() != "null":
                        plain.append(v)
            for lang in prefer:
                if lang in by_lang:
                    return by_lang[lang]
            if by_lang:
                return next(iter(by_lang.values()))
            if plain:
                return plain[0]
            return None
        return None

    @staticmethod
    def _date_range_to_iso(range_obj):
        if not isinstance(range_obj, dict):
            return None
        return PergamosLibUoaGrSearchCrawler._ms_to_iso_date(range_obj.get("from"))

    @staticmethod
    def _ms_to_iso_date(ms):
        if not isinstance(ms, (int, float)):
            return None
        try:
            dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _ms_to_iso_datetime(ms):
        if not isinstance(ms, (int, float)):
            return None
        try:
            dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
            return dt.isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Text / HTML helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    def _strip_html(self, raw):
        if not raw:
            return raw
        if "<" not in raw or ">" not in raw:
            return raw
        soup = self._make_soup(raw)
        if soup is None:
            return raw
        try:
            return soup.get_text("\n").strip()
        except Exception:
            return raw

    @classmethod
    def _clean_text(cls, value):
        if value is None:
            return ""
        text = str(value)
        text = text.replace("\xa0", " ").replace("​", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @staticmethod
    def _format_author_name(raw):
        """Pergamos often stores author names as "Surname, Given". Reformat
        to "Given Surname" so downstream comma-splitting (which treats
        commas as author separators) doesn't mangle a single name into two.
        """
        if not raw:
            return raw
        if "," in raw:
            surname, _, given = raw.partition(",")
            surname = surname.strip()
            given = given.strip()
            if given and surname:
                return f"{given} {surname}"
        return raw

    @staticmethod
    def _dedupe(values):
        seen = set()
        out = []
        for value in values:
            if value is None:
                continue
            key = str(value).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out
