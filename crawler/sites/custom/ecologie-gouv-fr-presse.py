# -*- coding: utf-8 -*-
"""Crawler for ecologie.gouv.fr press releases."""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EcologieGouvFrPresseCrawler(BaseCrawler):
    site_id = "ecologie-gouv-fr-presse"
    site_name = "Custom: ecologie-gouv-fr-presse"
    base_url = "https://www.ecologie.gouv.fr"

    START_URL = "https://www.ecologie.gouv.fr/presse"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 100
    _CURL_META_MARKER = "__ECOLOGIE_GOUV_FR_CURL_META__:"

    MONTHS = {
        "janvier": 1,
        "fevrier": 2,
        "mars": 3,
        "avril": 4,
        "mai": 5,
        "juin": 6,
        "juillet": 7,
        "aout": 8,
        "septembre": 9,
        "octobre": 10,
        "novembre": 11,
        "decembre": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the Drupal press list and each public detail HTML page."""
        saved = 0
        page = 0
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw = self._curl_get(
                list_url,
                context=f"list page {page}",
                referer=self.base_url + "/",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            print(
                f"[{self.site_id}] page {page}: discovered {len(records)} records "
                "from HTML list endpoint"
            )

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"page {page} item {idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"item {item_label} detail",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_label} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, detail_raw, record)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup, page):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
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
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
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
                body, http_code, effective_url = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
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

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

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

    def _parse_list(self, soup, list_url):
        records = []
        seen = set()
        for card in soup.select(".fr-card"):
            link = card.select_one(".fr-card__title a[href]") or card.select_one("h3 a[href]")
            if link is None:
                continue

            url = urljoin(self.base_url, link.get("href", "").strip())
            if not self._is_press_detail_url(url) or url in seen:
                continue
            seen.add(url)

            title = self._node_text(link)
            if not title:
                continue

            desc_node = card.select_one(".fr-card__desc")
            date_node = card.select_one(".fr-card__detail")
            tag_node = card.select_one(".fr-tag")

            date_raw = self._node_text(date_node)
            records.append(
                {
                    "title": title,
                    "url": url,
                    "external_id": self._external_id_from_url(url),
                    "published_date": self._parse_date(date_raw),
                    "category": self._node_text(tag_node),
                    "teaser": self._node_text(desc_node),
                    "list_date_text": date_raw,
                    "list_endpoint": list_url,
                }
            )
        return records

    def _parse_detail(self, soup, raw_html, record):
        node = (
            soup.select_one(".node--type-press.node--view-mode-full")
            or soup.select_one(".node--type-press")
            or soup
        )

        canonical_url = (
            self._link_href(soup, "link[rel='canonical']")
            or self._meta_content(soup, "og:url")
            or record.get("url")
            or ""
        )
        url = urljoin(self.base_url, canonical_url)

        h1_node = (
            node.select_one(".heading h1")
            or node.select_one("h1.fr-h1")
            or node.select_one("h1")
        )
        title = (
            self._node_text(h1_node)
            or self._clean_title(self._meta_content(soup, "og:title", "twitter:title"))
            or record.get("title", "")
        )
        title = self._clean_title(title)

        heading_info = " ".join(
            self._node_text(part)
            for part in node.select(".heading-info p")
            if self._node_text(part)
        )
        published_date = self._parse_date(heading_info) or record.get("published_date") or ""
        updated_date = self._parse_labeled_date(raw_html, "Mis a jour")

        tags = self._dedupe(self._node_text(tag) for tag in node.select(".heading-tags .fr-tag"))
        category = tags[0] if tags else record.get("category", "")

        pdf_links = []
        related_links = []
        for link in node.select("a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            label = self._node_text(link)
            if not href or href.startswith("mailto:"):
                continue
            if self._is_share_url(href):
                continue
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I):
                pdf_links.append({"label": label, "url": href})
            elif label and self.base_url in href:
                related_links.append({"label": label, "url": href})
        pdf_links = self._dedupe_dicts(pdf_links, "url")
        related_links = self._dedupe_dicts(related_links, "url")
        pdf_url = pdf_links[0]["url"] if pdf_links else ""

        content = node.select_one(".layout-inner") or node
        abstract = self._extract_article_text(content)
        if not abstract:
            abstract = self._meta_content(soup, "description", "og:description", "twitter:description")
        abstract = self._clean_multiline(abstract)

        content_id = self._extract_node_id(raw_html, soup)
        external_id = content_id or record.get("external_id") or self._external_id_from_url(url)
        image_url = (
            self._meta_content(soup, "og:image", "twitter:image")
            or self._link_href(soup, "link[rel='image_src']")
        )

        doi = ""
        doi_match = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", raw_html or "")
        if doi_match:
            doi = doi_match.group(1).rstrip(".,)")

        metadata = {
            "source": "Drupal HTML press list and public detail HTML",
            "list_endpoint": record.get("list_endpoint", self.START_URL),
            "detail_endpoint": "public detail HTML page",
            "html_route_only": True,
            "jsonapi_probe": "/jsonapi/node/press returned 404; ?_format=json returned rendered-route format error",
            "canonical_url": url,
            "content_id": content_id,
            "updated_date": updated_date,
            "list_date_text": record.get("list_date_text", ""),
            "teaser": record.get("teaser", ""),
            "image_url": image_url,
            "tags": tags,
            "pdf_links": pdf_links,
            "related_links": related_links[:20],
        }

        return {
            "external_id": external_id,
            "title": title,
            "authors": [],
            "abstract": abstract,
            "category": category,
            "keywords": tags,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": "Ministeres Transition ecologique, Amenagement du Territoire, Transports, Ville et Logement",
            "metadata": metadata,
        }

    def _extract_article_text(self, container):
        soup = BeautifulSoup(str(container), "html.parser")
        for bad in soup.select(
            "script, style, noscript, iframe, svg, picture, "
            ".fr-share, .fr-translate, .fr-content-media, "
            "#block-customer-languageswitcher, .visually-hidden"
        ):
            bad.decompose()

        parts = []
        for node in soup.find_all(["p", "li", "h2", "h3"], recursive=True):
            text = self._node_text(node)
            if not text:
                continue
            clean_key = self._strip_accents(text)
            if clean_key in {"FR - Francais", "Partager la page", "Page address copied to the clipboard."}:
                continue
            if text not in parts:
                parts.append(text)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 0:
            return self.START_URL
        return f"{self.START_URL}?page={page}"

    def _has_next_page(self, soup, page):
        next_link = soup.select_one(".fr-pagination__link--next[href]")
        if next_link is not None and next_link.get("href"):
            return True
        return bool(soup.select_one(f"a.fr-pagination__link[href='?page={page + 1}']"))

    @classmethod
    def _parse_date(cls, value):
        text = cls._strip_accents(cls._one_line(value).lower())
        if not text:
            return ""
        iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text)
        if iso:
            return iso.group(0)
        match = re.search(
            r"\b(\d{1,2})\s+"
            r"(janvier|fevrier|mars|avril|mai|juin|juillet|aout|"
            r"septembre|octobre|novembre|decembre)\s+"
            r"((?:19|20)\d{2})\b",
            text,
            flags=re.I,
        )
        if not match:
            return ""
        day = int(match.group(1))
        month = cls.MONTHS.get(match.group(2).lower())
        year = int(match.group(3))
        if not month:
            return ""
        return f"{year:04d}-{month:02d}-{day:02d}"

    @classmethod
    def _parse_labeled_date(cls, raw_html, label):
        if not raw_html:
            return ""
        raw_html = cls._strip_accents(raw_html)
        label = cls._strip_accents(label)
        pattern = rf"{re.escape(label)}\s+le\s+([^<\n\r]+)"
        match = re.search(pattern, raw_html, flags=re.I)
        if match:
            return cls._parse_date(match.group(1))
        return ""

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\xa0", " ")
        text = text.replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @classmethod
    def _clean_multiline(cls, value):
        text = cls._clean_text(value)
        text = re.sub(r" *\n+ *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _node_text(cls, node):
        if node is None:
            return ""
        return cls._one_line(node.get_text(" ", strip=True))

    @classmethod
    def _clean_title(cls, title):
        title = cls._one_line(title)
        title = re.sub(r"\s*\|\s*.*$", "", title, flags=re.I)
        return title.strip()

    @staticmethod
    def _meta_content(soup, *keys):
        for key in keys:
            node = soup.find("meta", attrs={"name": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
            node = soup.find("meta", attrs={"property": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
        return ""

    @staticmethod
    def _link_href(soup, selector):
        node = soup.select_one(selector)
        if node and node.get("href"):
            return node.get("href", "").strip()
        return ""

    @staticmethod
    def _external_id_from_url(url):
        path = urlparse(url or "").path.strip("/")
        if path:
            return path
        return url or ""

    @staticmethod
    def _is_press_detail_url(url):
        parsed = urlparse(url or "")
        return parsed.netloc in {"www.ecologie.gouv.fr", "ecologie.gouv.fr"} and parsed.path.startswith("/presse/")

    @staticmethod
    def _is_share_url(url):
        parsed = urlparse(url or "")
        return parsed.netloc in {
            "www.facebook.com",
            "facebook.com",
            "www.linkedin.com",
            "linkedin.com",
            "x.com",
            "twitter.com",
        }

    @classmethod
    def _extract_node_id(cls, raw_html, soup):
        match = re.search(r'"currentPath"\s*:\s*"node\\?/(\d+)"', raw_html or "")
        if match:
            return match.group(1)
        body = soup.find("body")
        if body:
            for klass in body.get("class", []):
                match = re.match(r"page-node-(\d+)$", klass)
                if match:
                    return match.group(1)
        return ""

    @staticmethod
    def _strip_accents(value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    @classmethod
    def _dedupe(cls, values):
        seen = set()
        result = []
        for value in values:
            clean = cls._one_line(value)
            key = clean.lower()
            if clean and key not in seen:
                result.append(clean)
                seen.add(key)
        return result

    @staticmethod
    def _dedupe_dicts(items, key):
        seen = set()
        result = []
        for item in items:
            value = item.get(key)
            if not value or value in seen:
                continue
            result.append(item)
            seen.add(value)
        return result
