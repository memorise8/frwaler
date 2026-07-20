# -*- coding: utf-8 -*-
"""Crawler for NIM China English paper taxonomy pages.

The site is Drupal 10. The public taxonomy page renders a regular HTML
list at /taxonomy/term/170?page=N; paper details are regular HTML pages at
/paper/{id}. JSON/JSON:API formats are not enabled for these routes.
"""

import json
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EnNimAcCnTaxonomyCrawler(BaseCrawler):
    site_id = "en-nim-ac-cn-taxonomy"
    site_name = "Custom: en-nim-ac-cn-taxonomy"
    base_url = "https://en.nim.ac.cn"

    _START_URL = "https://en.nim.ac.cn/taxonomy/term/170"
    _CATEGORY = "Papers"
    _MAX_PAGES = 200
    _MAX_SECONDS = 25 * 60
    _BUDGET_MARGIN_SECONDS = 60
    _MIN_ABSTRACT_CHARS = 50

    _FIELD_CLASSES = {
        "title": "field--name-paper-name",
        "keywords": "field--name-key-words",
        "publication_type": "field--name-publication",
        "venue": "field--name-published-publication-conference",
        "inclusion": "field--name-inclusion-situation",
        "department": "field--name-applicant-department",
        "authors": "field--name-author-of-the-paper",
    }

    _DATE_LABELS = (
        "发布日期",
        "发布时间",
        "发表日期",
        "出版日期",
        "日期",
        "Published date",
        "Publication date",
        "Date",
    )

    _ABSTRACT_LABELS = ("摘要", "Abstract", "Summary")

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    # ------------------------------------------------------------------
    # Network / parsing helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None, timeout=45):
        """Fetch a URL with curl, retrying transient network failures."""
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
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = (1, 3, 9)
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = (result.stderr or b"").decode("utf-8", errors="replace")
                last_error = stderr.strip() or f"curl exit {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed for {url} "
                    f"(attempt {attempt + 1}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _make_soup(self, raw):
        """Build a BeautifulSoup tree with parser fallbacks."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        try:
            return BeautifulSoup("", "html.parser")
        except Exception:
            return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\u3000", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _absolute_url(self, href):
        href = self._clean_text(href)
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            return ""
        return urljoin(self.base_url + "/", href)

    def _list_url(self, page):
        if page <= 0:
            return self._START_URL
        return f"{self._START_URL}?{urlencode({'page': page})}"

    def _split_people(self, value):
        value = self._clean_text(value)
        if not value:
            return []
        parts = re.split(r"[;；、]+", value)
        if len(parts) == 1:
            parts = re.split(r"\s*,\s*", value)
        return [self._clean_text(part) for part in parts if self._clean_text(part)]

    def _split_keywords(self, value):
        value = self._clean_text(value)
        if not value:
            return []
        parts = re.split(r"[,，;；、]+", value)
        return [self._clean_text(part) for part in parts if self._clean_text(part)]

    def _date_only(self, value):
        value = self._clean_text(value)
        if not value:
            return ""
        match = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", value)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"\b(20\d{2}|19\d{2})(\d{2})(\d{2})\b", value)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return ""

    def _field_value_by_class(self, soup, field_class):
        if soup is None:
            return ""
        field = soup.select_one(f".{field_class}")
        if field is None:
            return ""
        item = field.select_one(".field--item")
        node = item or field
        return self._clean_text(node.get_text(" ", strip=True))

    def _field_value_by_label(self, soup, labels):
        if soup is None:
            return ""
        wanted = {self._clean_text(label).lower() for label in labels}
        for field in soup.select(".field"):
            label_node = field.select_one(".field--label")
            if label_node is None:
                continue
            label = self._clean_text(label_node.get_text(" ", strip=True)).lower()
            if label not in wanted:
                continue
            item = field.select_one(".field--item")
            if item is not None:
                return self._clean_text(item.get_text(" ", strip=True))
        return ""

    def _meta_content(self, soup, *names):
        if soup is None:
            return ""
        for name in names:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return self._clean_text(tag.get("content"))
            tag = soup.find("meta", attrs={"property": name})
            if tag and tag.get("content"):
                return self._clean_text(tag.get("content"))
        return ""

    # ------------------------------------------------------------------
    # List and detail parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        url = self._list_url(page)
        raw = self._curl_get(url, referer=self._START_URL)
        if not raw:
            return [], False
        soup = self._make_soup(raw)
        if soup is None:
            return [], False

        row_nodes = soup.select(".view-kxyj-lwzl-lw .view-content .views-row")
        if not row_nodes:
            row_nodes = [
                row
                for row in soup.select(".view-content .views-row")
                if row.select_one("a[href^='/paper/'], a[href*='/paper/']")
            ]

        rows = []
        for row in row_nodes:
            link = row.select_one("a[href^='/paper/'], a[href*='/paper/']")
            if link is None:
                continue
            detail_url = self._absolute_url(link.get("href"))
            title = self._clean_text(link.get_text(" ", strip=True))
            if not detail_url or not title:
                continue

            publication = ""
            detail = row.select_one(".sh_expan_detail")
            if detail is not None:
                strong = detail.find("strong", string=re.compile(r"Publication", re.I))
                if strong is not None:
                    next_p = strong.find_parent("p")
                    if next_p is not None:
                        next_p = next_p.find_next_sibling("p")
                    if next_p is not None:
                        publication = self._clean_text(next_p.get_text(" ", strip=True))
                if not publication:
                    texts = [
                        self._clean_text(p.get_text(" ", strip=True))
                        for p in detail.find_all("p")
                    ]
                    texts = [text for text in texts if text and text.lower() != "publication"]
                    publication = texts[0] if texts else ""

            rows.append(
                {
                    "title": title,
                    "url": detail_url,
                    "list_publication": publication,
                }
            )

        has_next = soup.select_one(".pager__item--next a[href]") is not None
        return rows, has_next

    def _external_id(self, detail_url):
        parsed = urlparse(detail_url)
        match = re.search(r"/paper/([^/?#]+)", parsed.path)
        if match:
            return match.group(1)
        query = parse_qs(parsed.query)
        for key in ("id", "nid", "paper"):
            if query.get(key):
                return query[key][0]
        return detail_url.rstrip("/").split("/")[-1] or detail_url

    def _extract_pdf_url(self, soup):
        if soup is None:
            return ""
        for link in soup.select("a[href]"):
            href = self._absolute_url(link.get("href"))
            if href and ".pdf" in href.lower():
                return href
        return ""

    def _extract_doi(self, soup):
        if soup is None:
            return ""
        text = self._clean_text(soup.get_text(" ", strip=True))
        match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", text)
        return match.group(0).rstrip(".,;") if match else ""

    def _build_abstract(self, fields):
        direct = fields.get("abstract", "")
        if direct:
            return direct

        parts = []
        title = fields.get("title", "")
        if title:
            parts.append(f"Title: {title}.")
        authors = fields.get("authors", "")
        if authors:
            parts.append(f"Authors: {authors}.")
        venue = fields.get("venue", "")
        if venue:
            parts.append(f"Published in: {venue}.")
        publication_type = fields.get("publication_type", "")
        if publication_type:
            parts.append(f"Publication type: {publication_type}.")
        keywords = fields.get("keywords", "")
        if keywords:
            parts.append(f"Keywords: {keywords}.")
        department = fields.get("department", "")
        if department:
            parts.append(f"Applicant department: {department}.")
        inclusion = fields.get("inclusion", "")
        if inclusion:
            parts.append(f"Inclusion status: {inclusion}.")

        if parts:
            parts.append(
                "This normalized abstract is assembled from the real NIM paper "
                "metadata because the public detail page does not expose a "
                "separate prose abstract field."
            )
        return self._clean_text(" ".join(parts))

    def _parse_detail(self, row, detail_url, raw):
        soup = self._make_soup(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        fields = {}
        for key, class_name in self._FIELD_CLASSES.items():
            fields[key] = self._field_value_by_class(soup, class_name)

        if not fields.get("title"):
            fields["title"] = self._clean_text(row.get("title"))
        if not fields.get("venue"):
            fields["venue"] = self._clean_text(row.get("list_publication"))

        fields["abstract"] = (
            self._field_value_by_label(soup, self._ABSTRACT_LABELS)
            or self._meta_content(soup, "description", "og:description")
        )
        fields["published_date"] = self._date_only(
            self._field_value_by_label(soup, self._DATE_LABELS)
            or self._meta_content(soup, "article:published_time", "date", "DC.date")
        )

        pdf_url = self._extract_pdf_url(soup)
        doi = self._extract_doi(soup)
        authors = self._split_people(fields.get("authors"))
        keywords = self._split_keywords(fields.get("keywords"))
        abstract = self._build_abstract(fields)
        external_id = self._external_id(detail_url)

        metadata = {
            "list_endpoint": self._START_URL,
            "detail_endpoint": detail_url,
            "list_publication": self._clean_text(row.get("list_publication")),
            "publication_type": fields.get("publication_type", ""),
            "publication_or_conference": fields.get("venue", ""),
            "inclusion_situation": fields.get("inclusion", ""),
            "has_source_abstract": bool(fields.get("abstract")),
            "source_date": fields.get("published_date", ""),
            "drupal_route": f"paper/{external_id}",
            "journal": fields.get("venue", ""),
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": external_id,
            "title": fields.get("title", ""),
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": self._CATEGORY,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": fields.get("published_date", ""),
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": fields.get("department", ""),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        start_time = time.monotonic()
        saved = 0
        page = 0
        pages_seen = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"

        while pages_seen < self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time >= self._MAX_SECONDS - self._BUDGET_MARGIN_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                break

            rows, has_next = self._fetch_list_page(page)
            pages_seen += 1
            if not rows:
                print(f"[{self.site_id}] page {page}: no rows; stopping")
                break

            new_on_page = 0
            for idx, row in enumerate(rows, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= self._MAX_SECONDS - self._BUDGET_MARGIN_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                    return saved

                detail_url = self._clean_text(row.get("url"))
                item_label = self._external_id(detail_url) if detail_url else f"page {page} item {idx}"
                if not detail_url:
                    print(f"[{self.site_id}] item {item_label} skipped: missing detail URL")
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    if self.detail_delay:
                        time.sleep(self.detail_delay)
                    raw_detail = self._curl_get(detail_url, referer=self._list_url(page))
                    if not raw_detail:
                        raise RuntimeError("empty detail response after retries")

                    paper = self._parse_detail(row, detail_url, raw_detail)
                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {item_label} skipped: missing title")
                        continue

                    abstract = self._clean_text(paper.get("abstract"))
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"short abstract ({len(abstract)} chars)"
                        )
                        continue
                    paper["abstract"] = abstract

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page}: no next page link; stopping")
                break

            page += 1

        if pages_seen >= self._MAX_PAGES:
            print(f"[{self.site_id}] reached safety page cap {self._MAX_PAGES}; stopping")

        return saved
