# -*- coding: utf-8 -*-
"""Crawler for EFK/CDF French press releases."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse, unquote

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EfkAdminChFrCrawler(BaseCrawler):
    site_id = "efk-admin-ch-fr"
    site_name = "Custom: efk-admin-ch-fr"
    base_url = "https://www.efk.admin.ch"

    START_URL = "https://www.efk.admin.ch/fr/actualites/?category_name=medienmitteilung"
    REST_BASE = "https://www.efk.admin.ch/fr/wp-json/wp/v2"
    LIST_API = REST_BASE + "/posts"
    CATEGORY_API = REST_BASE + "/categories"
    TARGET_CATEGORY_SLUG = "communique-de-presse"
    FALLBACK_CATEGORY_ID = 213

    PAGE_SIZE = 20
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__EFK_ADMIN_CH_FR_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl the WordPress REST list and public detail pages."""
        saved = 0
        page = 1
        seen_urls = set()
        started_at = time.time()
        limit_label = str(limit) if limit is not None else "inf"

        category_id = self._discover_category_id()
        print(
            f"[{self.site_id}] list endpoint: {self.LIST_API} "
            f"category={category_id}"
        )

        while page <= self.SAFETY_PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if self._budget_nearly_exhausted(started_at):
                print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                break
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_payload, list_meta = self._fetch_list_page(page, category_id)
            if list_payload is None:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break
            if not list_payload:
                print(f"[{self.site_id}] list page {page} returned no records; stopping")
                break

            records = self._parse_list_records(list_payload)
            if not records:
                print(f"[{self.site_id}] no parseable records on page {page}; stopping")
                break

            new_on_page = 0
            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._budget_nearly_exhausted(started_at):
                    print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                    return saved

                detail_url = record.get("url") or ""
                if not detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                item_label = f"page {page} item {idx}"
                try:
                    time.sleep(self.detail_delay)
                    detail_json = self._fetch_detail_json(record["external_id"])
                    if detail_json is None:
                        detail_json = record.get("raw") or {}

                    time.sleep(self.detail_delay)
                    detail_html, effective_url, _ = self._curl_get_text(
                        detail_url,
                        context=f"item {item_label} detail HTML",
                        referer=self.START_URL,
                        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    )
                    if not detail_html:
                        raise RuntimeError("detail HTML fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_html,
                        context=f"item {item_label} detail HTML",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(record, detail_json, detail_soup, effective_url)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = self._to_paper(parsed)
                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page} had no unseen URLs; stopping")
                break

            total_pages = self._safe_int((list_meta or {}).get("total_pages"))
            if total_pages is not None and page >= total_pages:
                break
            page += 1

        if page > self.SAFETY_PAGE_CAP:
            print(f"[{self.site_id}] reached safety page cap ({self.SAFETY_PAGE_CAP}); stopping")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # API/network helpers
    # ------------------------------------------------------------------

    def _discover_category_id(self):
        raw, _, _ = self._curl_get_text(
            self.CATEGORY_API + "?per_page=100",
            context="category discovery",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            print(
                f"[{self.site_id}] category discovery failed; "
                f"using fallback category {self.FALLBACK_CATEGORY_ID}"
            )
            return self.FALLBACK_CATEGORY_ID
        try:
            categories = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(
                f"[{self.site_id}] category JSON invalid ({exc}); "
                f"using fallback category {self.FALLBACK_CATEGORY_ID}"
            )
            return self.FALLBACK_CATEGORY_ID

        for category in categories if isinstance(categories, list) else []:
            if (category.get("slug") or "") == self.TARGET_CATEGORY_SLUG:
                return category.get("id") or self.FALLBACK_CATEGORY_ID
        for category in categories if isinstance(categories, list) else []:
            name = self._clean_text(category.get("name") or "").lower()
            if "communiqu" in name and "presse" in name:
                return category.get("id") or self.FALLBACK_CATEGORY_ID

        return self.FALLBACK_CATEGORY_ID

    def _fetch_list_page(self, page, category_id):
        params = {
            "per_page": str(self.PAGE_SIZE),
            "page": str(page),
            "categories": str(category_id),
            "_embed": "wp:term",
        }
        url = f"{self.LIST_API}?{urlencode(params)}"
        raw, _, meta = self._curl_get_text(
            url,
            context=f"list page {page}",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            return None, meta
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid list JSON on page {page}: {exc}")
            return None, meta
        if isinstance(payload, dict) and payload.get("code") == "rest_post_invalid_page_number":
            return [], meta
        if not isinstance(payload, list):
            print(f"[{self.site_id}] unexpected list JSON shape on page {page}")
            return None, meta
        return payload, meta

    def _fetch_detail_json(self, external_id):
        url = f"{self.REST_BASE}/posts/{external_id}?_embed=wp:term"
        raw, _, _ = self._curl_get_text(
            url,
            context=f"detail API {external_id}",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid detail JSON for {external_id}: {exc}")
            return None
        if not isinstance(payload, dict) or payload.get("code"):
            return None
        return payload

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        write_out = (
            "\n"
            + self._CURL_META_MARKER
            + "%{http_code}\t%{url_effective}\t%header{x-wp-total}\t%header{x-wp-totalpages}"
        )
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'}",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            "-w",
            write_out,
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                body, meta = self._split_curl_output(stdout, url)
                http_code = self._safe_int(meta.get("http_code"))

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code is not None and http_code >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {meta.get('effective_url') or url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {meta.get('effective_url') or url}")
                return body, meta.get("effective_url") or url, meta
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

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None, url, {}

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, {"effective_url": fallback_url}
        body = raw[:marker_pos]
        meta_raw = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
        parts = meta_raw.split("\t")
        while len(parts) < 4:
            parts.append("")
        return body, {
            "http_code": parts[0].strip(),
            "effective_url": parts[1].strip() or fallback_url,
            "total": parts[2].strip(),
            "total_pages": parts[3].strip(),
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list_records(self, payload):
        records = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            external_id = str(item.get("id") or "").strip()
            url = (item.get("link") or "").strip()
            title = self._html_to_text(((item.get("title") or {}).get("rendered")) or "")
            if not external_id or not url or not title:
                continue
            listed_raw = item.get("date") or item.get("date_gmt") or ""
            records.append({
                "external_id": external_id,
                "post_number": external_id if external_id.isdigit() else (item.get("slug") or None),
                "url": url,
                "title": title,
                "slug": item.get("slug") or "",
                "listed_date": self._date_only(listed_raw),
                "listed_date_raw": listed_raw,
                "excerpt": self._html_to_text(((item.get("excerpt") or {}).get("rendered")) or ""),
                "categories": self._extract_terms(item),
                "raw": item,
            })
        return records

    def _parse_detail(self, list_record, detail_json, soup, effective_url):
        title = self._detail_title(soup) or self._json_title(detail_json) or list_record["title"]
        categories = self._extract_terms(detail_json) or list_record.get("categories") or []
        category_names = [c["name"] for c in categories if c.get("taxonomy") == "category" and c.get("name")]
        tag_names = [c["name"] for c in categories if c.get("taxonomy") == "post_tag" and c.get("name")]

        published_raw = self._published_raw(detail_json, soup)
        published_date = self._date_only(published_raw) or list_record.get("listed_date") or ""
        listed_date = list_record.get("listed_date") or published_date

        content_node = soup.select_one("article.single__content") or soup.select_one("article")
        abstract = self._extract_abstract(content_node)
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = self._html_to_text(((detail_json.get("content") or {}).get("rendered")) or "")
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = list_record.get("excerpt") or self._meta_description(soup)

        attachments = self._extract_attachments(soup, effective_url)
        selected_pdf = self._select_pdf_attachment(attachments)
        pdf_url = selected_pdf.get("url") if selected_pdf else None
        original_filename = (
            selected_pdf.get("originalFilename")
            if selected_pdf else self._filename_from_url(pdf_url)
        )

        author = self._schema_author(detail_json) or self._meta_author(soup)
        contact = self._extract_contact(soup)
        department = contact.get("department") or "Communication"
        publisher = "Contrôle fédéral des finances (CDF)"

        keywords = self._dedupe(category_names + tag_names)
        category = "Communiqué de presse"
        category_display = self._category_display(soup) or " - ".join(category_names)

        external_id = str(detail_json.get("id") or list_record["external_id"])
        post_number = external_id if external_id.isdigit() else (detail_json.get("slug") or list_record.get("slug"))

        target_category_id = None
        for category_item in categories:
            if category_item.get("slug") == self.TARGET_CATEGORY_SLUG:
                target_category_id = category_item.get("id")
                break

        metadata = {
            "posted_date": list_record.get("listed_date_raw") or listed_date,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "post_id": external_id,
            "wp_post_id": external_id,
            "node_id": external_id,
            "post_number": post_number,
            "slug": detail_json.get("slug") or list_record.get("slug"),
            "category_id": target_category_id or self.FALLBACK_CATEGORY_ID,
            "category_names": category_names,
            "tag_names": tag_names,
            "category_display": category_display,
            "published_date_raw": published_raw,
            "modified": detail_json.get("modified"),
            "modified_gmt": detail_json.get("modified_gmt"),
            "date_gmt": detail_json.get("date_gmt"),
            "guid": (detail_json.get("guid") or {}).get("rendered"),
            "detail_api": f"{self.REST_BASE}/posts/{external_id}",
            "detail_effective_url": effective_url,
            "list_url": self.START_URL,
            "list_api": self.LIST_API,
            "list_record": self._compact_wp_record(list_record.get("raw") or {}),
            "detail_record": self._compact_wp_record(detail_json),
            "attachments": attachments,
            "contact": contact,
            "yoast_head_json": detail_json.get("yoast_head_json"),
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": author or "",
            "publisher": publisher,
            "department": department,
            "journal": "",
            "url": detail_json.get("link") or list_record.get("url") or effective_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords),
            "category": category,
            "doi": "",
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _to_paper(self, parsed):
        metadata = dict(parsed["metadata"])
        metadata.update({
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
        })
        return {
            "id": f"{self.site_id}:{parsed['external_id']}",
            "site_id": self.site_id,
            "external_id": parsed["external_id"],
            "post_number": parsed.get("post_number"),
            "title": parsed["title"],
            "abstract": parsed["abstract"],
            "published_date": parsed["published_date"],
            "listed_date": parsed["listed_date"],
            "posted_date": parsed["posted_date"],
            "authors": parsed["authors"],
            "publisher": parsed["publisher"],
            "department": parsed["department"],
            "journal": parsed["journal"],
            "url": parsed["url"],
            "pdf_url": parsed["pdf_url"],
            "keywords": parsed["keywords"],
            "category": parsed["category"],
            "doi": parsed["doi"],
            "original_filename": parsed["original_filename"],
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _extract_terms(self, item):
        terms = []
        embedded = item.get("_embedded") or {}
        for group in embedded.get("wp:term") or []:
            for term in group or []:
                if not isinstance(term, dict):
                    continue
                terms.append({
                    "id": term.get("id"),
                    "name": self._clean_text(term.get("name") or ""),
                    "slug": term.get("slug") or "",
                    "taxonomy": term.get("taxonomy") or "",
                    "link": term.get("link") or "",
                })
        return terms

    def _detail_title(self, soup):
        node = soup.select_one("h1.primary-title") or soup.select_one("h1")
        return self._clean_text(node.get_text(" ", strip=True)) if node else ""

    def _json_title(self, item):
        return self._html_to_text(((item.get("title") or {}).get("rendered")) or "")

    def _published_raw(self, detail_json, soup):
        yoast = detail_json.get("yoast_head_json") or {}
        raw = yoast.get("article_published_time")
        if raw:
            return raw
        for node in self._schema_graph(detail_json):
            if isinstance(node, dict) and node.get("@type") == "Article" and node.get("datePublished"):
                return node.get("datePublished")
        meta = soup.select_one('meta[property="article:published_time"][content]')
        if meta:
            return meta.get("content")
        tag = soup.select_one(".single__tags .tag") or soup.select_one("p.tag")
        if tag:
            return tag.get_text(" ", strip=True)
        return detail_json.get("date") or ""

    def _schema_author(self, detail_json):
        yoast = detail_json.get("yoast_head_json") or {}
        if yoast.get("author"):
            return self._clean_text(yoast.get("author"))
        for node in self._schema_graph(detail_json):
            if not isinstance(node, dict) or node.get("@type") != "Article":
                continue
            author = node.get("author")
            if isinstance(author, dict) and author.get("name"):
                return self._clean_text(author.get("name"))
        return ""

    def _schema_graph(self, detail_json):
        yoast = detail_json.get("yoast_head_json") or {}
        schema = yoast.get("schema") or {}
        graph = schema.get("@graph") if isinstance(schema, dict) else []
        return graph if isinstance(graph, list) else []

    def _meta_author(self, soup):
        meta = soup.select_one('meta[name="author"][content]')
        return self._clean_text(meta.get("content")) if meta else ""

    def _meta_description(self, soup):
        meta = soup.select_one('meta[name="description"][content]')
        return self._clean_text(meta.get("content")) if meta else ""

    def _extract_abstract(self, content_node):
        if content_node is None:
            return ""
        soup = self._make_soup(str(content_node), context="detail content fragment")
        if soup is None:
            return self._clean_text(content_node.get_text(" ", strip=True))
        for tag in soup.select("script, style, noscript, svg, figure, img, .single__tags"):
            tag.decompose()
        return self._clean_text(soup.get_text(" ", strip=True))

    def _extract_attachments(self, soup, base_url):
        attachments = []
        for idx, link in enumerate(soup.select("aside.audit__files a.download__file[href]"), start=1):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(base_url or self.base_url, href)
            label = self._clean_text(link.get_text(" ", strip=True))
            filename = self._filename_from_url(url)
            attachments.append({
                "url": url,
                "label": label,
                "filename": filename,
                "originalFilename": filename,
                "is_pdf": self._looks_like_pdf(url),
                "position": idx,
            })
        if attachments:
            return attachments

        for idx, link in enumerate(soup.select("a[href]"), start=1):
            href = (link.get("href") or "").strip()
            url = urljoin(base_url or self.base_url, href)
            if not self._looks_like_pdf(url):
                continue
            label = self._clean_text(link.get_text(" ", strip=True))
            filename = self._filename_from_url(url)
            attachments.append({
                "url": url,
                "label": label,
                "filename": filename,
                "originalFilename": filename,
                "is_pdf": True,
                "position": idx,
            })
        return attachments

    def _select_pdf_attachment(self, attachments):
        pdfs = [item for item in attachments if item.get("is_pdf")]
        if not pdfs:
            return None
        preferred_patterns = (
            "communique",
            "communiqué",
            "medienmitteilung",
            "mm-",
            "_mm",
            "-mm",
        )
        for item in pdfs:
            haystack = f"{item.get('label') or ''} {item.get('filename') or ''}".lower()
            if any(pattern in haystack for pattern in preferred_patterns):
                return item
        return pdfs[0]

    def _extract_contact(self, soup):
        contact = {
            "organization": "Contrôle fédéral des finances",
            "department": "",
            "phone": "",
            "email_label": "",
        }
        contact_box = None
        for aside in soup.select("aside.files-container"):
            title = self._clean_text(
                (aside.select_one(".files-container__title") or aside).get_text(" ", strip=True)
            )
            if title.lower().startswith("contact"):
                contact_box = aside
                break
        if contact_box is None:
            return contact

        names = [
            self._clean_text(node.get_text(" ", strip=True))
            for node in contact_box.select(".single__author__name")
        ]
        names = [name for name in names if name]
        if names:
            contact["organization"] = names[0]
        if len(names) > 1:
            contact["department"] = names[1]
        phone = contact_box.select_one('a[href^="tel:"]')
        if phone:
            contact["phone"] = self._clean_text(phone.get_text(" ", strip=True))
        email = contact_box.select_one("a.single__author__email")
        if email:
            contact["email_label"] = self._clean_text(email.get_text(" ", strip=True))
        return contact

    def _category_display(self, soup):
        node = soup.select_one(".single__tags__cat")
        return self._clean_text(node.get_text(" ", strip=True)) if node else ""

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    def _budget_nearly_exhausted(self, started_at):
        return (time.time() - started_at) >= (self.WALL_CLOCK_BUDGET_SECONDS - 30)

    def _compact_wp_record(self, item):
        if not isinstance(item, dict):
            return {}
        compact = {}
        for key in (
            "id", "date", "date_gmt", "modified", "modified_gmt", "slug",
            "status", "type", "link", "author", "featured_media",
            "categories", "tags", "template",
        ):
            if key in item:
                compact[key] = item.get(key)
        for key in ("guid", "title", "excerpt"):
            value = item.get(key)
            if isinstance(value, dict) and "rendered" in value:
                compact[key] = self._html_to_text(value.get("rendered") or "")
            elif value:
                compact[key] = value
        return compact

    @staticmethod
    def _date_only(raw):
        if not raw:
            return ""
        text = str(raw).strip()
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})", text)
        if match:
            day, month, year = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return ""

    @staticmethod
    def _safe_int(value):
        try:
            if value in (None, ""):
                return None
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _html_to_text(self, html):
        if not html:
            return ""
        if "<" not in html:
            return self._clean_text(html)
        soup = self._make_soup(html, context="HTML fragment")
        if soup is None:
            text = re.sub(r"<[^>]+>", " ", html)
            return self._clean_text(text)
        return self._clean_text(soup.get_text(" ", strip=True))

    @staticmethod
    def _dedupe(values):
        seen = set()
        result = []
        for value in values:
            text = str(value or "").strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        filename = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        return filename or None

    @staticmethod
    def _looks_like_pdf(url):
        path = urlparse(url or "").path.lower()
        return path.endswith(".pdf")
