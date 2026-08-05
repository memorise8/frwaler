# -*- coding: utf-8 -*-
"""Crawler for gov.gr service search results.

Start URL:
https://www.gov.gr/search?SearchSensor=%22%22&SingleRangeSensor=%22%CE%8C%CE%BB%CE%B5%CF%82+%CE%BF%CE%B9+%CF%85%CF%80%CE%B7%CF%81%CE%B5%CF%83%CE%AF%CE%B5%CF%82%22

Repaired 2026-08-05: gov.gr migrated its frontend from a Nuxt/ReactiveSearch
app (backed by a public Elasticsearch proxy at
/elasticsearch/services/_search) to a Next.js app served off an Azure Front
Door origin. The old Elasticsearch proxy and the old
/el/api/v1/services/{id}/?format=json detail API are both gone (proxy now
just returns the Next.js app shell HTML instead of JSON; detail API 404s).

The new, still-public JSON APIs (verified via curl, no auth needed) are:

- list API:   https://www.gov.gr/api/search?query=&page={page}&per_page={n}
              -> {"pagination": {"page","per_page","total_items","total_pages"},
                  "data": [{"title": {"el_text","en_text"}, "slug",
                            "human_readable_id"}, ...]}
              per_page caps server-side at 100.
- detail API: https://www.gov.gr/api/services/{human_readable_id}
              -> full service record: title, description (el_text/en_text
                 HTML), exit_links/useful_links/support_links, organization,
                 groups[].category/subcategory/group, created/modified, etc.

Detail page URLs are only resolvable client-side (JS SPA route; curl always
gets the app-shell "page not found" HTML even for real, live services), so
the canonical URL is *constructed* from the confirmed live pattern
https://www.gov.gr/ipiresies/{category_slug}/{subcategory_slug}/{slug}
(verified against real indexed gov.gr service pages).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


class GovGrSearchCrawler(BaseCrawler):
    site_id = "gov-gr-search"
    site_name = "Custom: gov-gr-search"
    base_url = "https://www.gov.gr"

    START_URL = (
        'https://www.gov.gr/search?SearchSensor=%22%22&SingleRangeSensor='
        '%22%CE%8C%CE%BB%CE%B5%CF%82+%CE%BF%CE%B9+%CF%85%CF%80%CE%B7'
        '%CF%81%CE%B5%CF%83%CE%AF%CE%B5%CF%82%22'
    )
    SEARCH_API = "https://www.gov.gr/api/search"
    DETAIL_API = "https://www.gov.gr/api/services/{service_id}"

    PAGE_SIZE = 100
    MAX_PAGES = 200
    WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT_SECONDS = 45
    MIN_ABSTRACT_CHARS = 100
    _CURL_META_MARKER = "__GOV_GR_SEARCH_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_display = str(limit) if limit is not None else "inf"

        for page in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if self._near_time_budget(started_at):
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly")
                break

            if page == 1 or page % 10 == 0:
                print(f"[gov-gr-search] page {page}: saved {saved}/{limit_display}")

            list_payload = self._fetch_list_page(page)
            records = self._extract_hits(list_payload)
            if not records:
                print(f"[{self.site_id}] page {page}: no records returned; stopping")
                break

            new_urls_on_page = 0
            for idx, hit in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._near_time_budget(started_at):
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly")
                    return saved

                item_label = str(hit.get("human_readable_id") or hit.get("slug") or f"{page}.{idx}")
                if item_label in seen_urls:
                    continue
                seen_urls.add(item_label)
                new_urls_on_page += 1

                try:
                    time.sleep(self.detail_delay)
                    detail = self._fetch_detail(item_label)
                    detail_url = self._build_detail_url(hit, detail)
                    if not detail_url:
                        print(f"[{self.site_id}] item {item_label} skipped: no detail URL")
                        continue
                    parsed = self._parse_record(hit, detail, detail_url)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract shorter than {self.MIN_ABSTRACT_CHARS} chars "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(self._paper_from_record(parsed))
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_display}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[gov-gr-search] item {item_label} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break
            if len(records) < self.PAGE_SIZE:
                print(f"[{self.site_id}] page {page}: final partial page ({len(records)} records)")
                break

        else:
            print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        url = f"{self.SEARCH_API}?query=&page={page}&per_page={self.PAGE_SIZE}"
        raw = self._curl(
            url,
            context=f"list page={page}",
            accept="application/json,text/plain,*/*",
            referer=self.START_URL,
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] list JSON parse failed at page={page}: {exc}")
            return None

    def _fetch_detail(self, service_id):
        url = self.DETAIL_API.format(service_id=service_id)
        raw = self._curl(
            url,
            context=f"detail service_id={service_id}",
            accept="application/json,text/plain,*/*",
            referer=self.START_URL,
        )
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] detail JSON parse failed for {service_id}: {exc}")
            return {}

    def _curl(
        self,
        url,
        *,
        context,
        method="GET",
        json_payload=None,
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        credentials=None,
        referer=None,
    ):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT_SECONDS),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: el-GR,el;q=0.9,en;q=0.8",
            "-w",
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if credentials:
            cmd.extend(["-u", credentials])
        if method.upper() == "POST":
            cmd.extend(["-X", "POST"])
        if json_payload is not None:
            cmd.extend(["-H", "Content-Type: application/json"])
            cmd.extend(["--data", json.dumps(json_payload, ensure_ascii=False)])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT_SECONDS + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                body, http_code, effective_url = self._split_curl_output(stdout, url)
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                if "Access Denied" in body[:1000]:
                    raise RuntimeError(f"access denied for {effective_url}")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER) :].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing and mapping
    # ------------------------------------------------------------------

    def _extract_hits(self, payload):
        if not isinstance(payload, dict):
            return []
        records = payload.get("data") or []
        return records if isinstance(records, list) else []

    def _first_group(self, detail):
        groups = detail.get("groups") if isinstance(detail, dict) else None
        if isinstance(groups, list) and groups:
            first = groups[0]
            return first if isinstance(first, dict) else {}
        return {}

    def _parse_record(self, hit, detail, detail_url):
        detail = detail if isinstance(detail, dict) else {}
        title_field = detail.get("title") or hit.get("title") or {}
        title = self._clean_text(title_field.get("el_text") or title_field.get("en_text") or "")
        description_field = detail.get("description") or {}
        description_html = description_field.get("el_text") or description_field.get("en_text") or ""
        abstract = self._html_to_text(description_html)
        service_id = str(detail.get("human_readable_id") or hit.get("human_readable_id") or "").strip()
        post_number = service_id if service_id else None

        created_raw = detail.get("created")
        modified_raw = detail.get("modified")
        published_date = self._iso_date(created_raw)
        listed_date = self._iso_date(modified_raw) or published_date

        group = self._first_group(detail)
        category_title = self._nested_text(group, "category", "title", lang="el_text")
        subcategory_title = self._nested_text(group, "subcategory", "title", lang="el_text")
        group_title = self._nested_text(group, "group", "title", lang="el_text")
        organization_field = detail.get("organization") or detail.get("support_company") or {}
        organization_title = self._clean_text(
            (organization_field.get("title") or {}).get("el_text")
            or (organization_field.get("title") or {}).get("en_text")
            or ""
        )
        publisher = organization_title or None
        department = organization_title or None
        keywords = self._join_unique([category_title, subcategory_title, group_title], sep=", ")

        all_links = self._collect_links(detail, description_html)
        pdf_url = self._first_pdf_url(all_links)
        original_filename = self._filename_from_url(pdf_url)

        metadata = {
            "source": "gov.gr /api/search + /api/services/{human_readable_id} (post-2026-08 Next.js migration)",
            "start_url": self.START_URL,
            "search_api_url": self.SEARCH_API,
            "detail_api_url": self.DETAIL_API.format(service_id=service_id) if service_id else None,
            "posted_date": modified_raw,
            "created_timestamp": created_raw,
            "modified_timestamp": modified_raw,
            "listed_date": listed_date,
            "post_number": post_number,
            "service_id": service_id,
            "node_id": detail.get("id") or hit.get("id"),
            "slug": detail.get("slug") or hit.get("slug"),
            "status": detail.get("status"),
            "category": group.get("category"),
            "sub_category": group.get("subcategory"),
            "group": group.get("group"),
            "organization": detail.get("organization"),
            "support_company": detail.get("support_company"),
            "exit_links": detail.get("exit_links"),
            "useful_links": detail.get("useful_links"),
            "support_links": detail.get("support_links"),
            "all_links": all_links,
            "provides_feedback": detail.get("provides_feedback"),
            "is_support": detail.get("is_support"),
            "is_translation_confirmed": detail.get("is_translation_confirmed"),
            "raw_list_hit": hit,
            "raw_detail": detail,
        }
        if original_filename:
            metadata["originalFilename"] = original_filename

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": service_id or (detail.get("slug") or detail_url),
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category_title,
            "doi": None,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _paper_from_record(self, parsed):
        metadata = parsed.get("metadata") or {}
        return {
            "id": parsed.get("id"),
            "site_id": self.site_id,
            "external_id": parsed.get("external_id"),
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("posted_date"),
            "authors": parsed.get("authors"),
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department"),
            "journal": parsed.get("journal"),
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    def _build_detail_url(self, hit, detail):
        detail = detail if isinstance(detail, dict) else {}
        slug = (detail.get("slug") or hit.get("slug") or "").strip()
        group = self._first_group(detail)
        category_slug = self._nested_text(group, "category", "slug")
        subcategory_slug = self._nested_text(group, "subcategory", "slug")
        if category_slug and subcategory_slug and slug:
            return f"{self.base_url}/ipiresies/{category_slug}/{subcategory_slug}/{slug}"
        if slug:
            return f"{self.base_url}/ipiresies/{slug}"
        return None

    def _collect_links(self, detail, description_html):
        links = []

        def add_link(title, url, kind):
            if not url:
                return
            absolute = urljoin(self.base_url, str(url).strip())
            links.append({"title": self._clean_text(title or ""), "url": absolute, "kind": kind})

        def add_link_group(entries, kind):
            for link in entries or []:
                if not isinstance(link, dict):
                    continue
                url_text = link.get("url_text") or {}
                title = url_text.get("el_text") or url_text.get("en_text") or ""
                add_link(title, link.get("url_link") or link.get("url_link_en"), kind)

        add_link_group(detail.get("exit_links"), "exit_link")
        add_link_group(detail.get("useful_links"), "useful_link")
        add_link_group(detail.get("support_links"), "support_link")
        if detail.get("support_url"):
            add_link("support_url", detail.get("support_url"), "support_url")

        soup = self._make_soup(description_html)
        if soup is not None:
            for a_tag in soup.find_all("a", href=True):
                add_link(a_tag.get_text(" ", strip=True), a_tag.get("href"), "description_link")
        else:
            for href in re.findall(r'href=["\']([^"\']+)["\']', description_html or "", re.I):
                add_link("", href, "description_link")

        seen = set()
        unique_links = []
        for link in links:
            url = link.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            unique_links.append(link)
        return unique_links

    def _first_pdf_url(self, links):
        for link in links or []:
            url = link.get("url") if isinstance(link, dict) else None
            if self._looks_like_pdf(url):
                return url
        return None

    def _looks_like_pdf(self, url):
        if not url:
            return False
        parsed = urlparse(url)
        lower_url = url.lower()
        lower_path = parsed.path.lower()
        query = parse_qs(parsed.query)
        return (
            ".pdf" in lower_path
            or lower_path.endswith("/downloadfeksapi/")
            or "fek_pdf" in query
            or ".pdf" in lower_url
        )

    def _filename_from_url(self, url):
        if not url:
            return None
        parsed = urlparse(url)
        name = unquote(os.path.basename(parsed.path.rstrip("/")))
        if name and "." in name and len(name) <= 240:
            return name
        query = parse_qs(parsed.query)
        for key in ("fek_pdf", "filename", "file", "name"):
            values = query.get(key) or []
            if values:
                candidate = unquote(values[0]).strip()
                if candidate:
                    if "." not in os.path.basename(candidate) and self._looks_like_pdf(url):
                        candidate = f"{candidate}.pdf"
                    return os.path.basename(candidate)[:240]
        return None

    # ------------------------------------------------------------------
    # Text/date helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw):
        if raw is None:
            return None
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup unavailable: {exc}")
            return None

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
                continue
        return None

    def _html_to_text(self, html):
        if not html:
            return ""
        soup = self._make_soup(html)
        if soup is not None:
            for tag in soup.find_all(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = soup.get_text(" ", strip=True)
        else:
            text = re.sub(r"<[^>]+>", " ", html)
        return self._clean_text(text)

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _iso_date(self, raw):
        if not raw:
            return None
        match = re.search(r"(\d{4}-\d{2}-\d{2})", str(raw))
        return match.group(1) if match else None

    def _nested_text(self, mapping, key, subkey, lang="el_text"):
        value = mapping.get(key) if isinstance(mapping, dict) else None
        if not isinstance(value, dict):
            return ""
        sub = value.get(subkey)
        if isinstance(sub, dict):
            # {"el_text": ..., "en_text": ...} shape (new gov.gr API).
            return self._clean_text(sub.get(lang) or sub.get("en_text") or sub.get("el_text"))
        return self._clean_text(sub)

    def _list_titles(self, values):
        titles = []
        if not isinstance(values, list):
            return titles
        for value in values:
            if isinstance(value, dict):
                title = self._clean_text(value.get("title"))
                if title:
                    titles.append(title)
        return titles

    def _join_unique(self, values, sep="; "):
        result = []
        seen = set()
        for value in values:
            text = self._clean_text(value)
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return sep.join(result) if result else None

    def _has_rich_value(self, value):
        if isinstance(value, dict):
            return any(v not in (None, "", [], {}) for v in value.values())
        if isinstance(value, list):
            return bool(value)
        return value not in (None, "")

    def _has_expanded_links(self, value):
        return isinstance(value, list) and any(isinstance(item, dict) and item.get("url") for item in value)

    def _near_time_budget(self, started_at):
        return time.monotonic() - started_at >= self.WALL_CLOCK_BUDGET_SECONDS - 30
