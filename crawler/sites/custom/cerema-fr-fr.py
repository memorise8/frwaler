# -*- coding: utf-8 -*-
"""Crawler for Cerema (Centre d'études et d'expertise sur les risques, l'environnement, la mobilité et l'aménagement).

Targets: https://www.cerema.fr/fr/presse/dossier and /fr/presse/document
Drupal 11 Views listing — HTML pagination (?page=N), 15 items/page.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import time
import unicodedata
from datetime import date
from html import unescape
from urllib.parse import urljoin, urlparse, parse_qs

from crawler.base_crawler import BaseCrawler

_SITE_ID = "cerema-fr-fr"
_BASE_URL = "https://www.cerema.fr"
_LIST_URL = "https://www.cerema.fr/fr/presse/dossier"
_LIST_SCOPES = {"dossier": _LIST_URL, "document": _BASE_URL + "/fr/presse/document"}
_PAGE_SIZE = 15
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 100

# Exact aliases after accent/case folding; never accept arbitrary prefixes.
_FR_MONTHS = {
    name: f"{number:02d}"
    for number, names in enumerate((
        "janvier janv january jan", "fevrier fevr fev february feb",
        "mars march mar", "avril avr april apr", "mai may",
        "juin june jun", "juillet juil july jul", "aout aou august aug",
        "septembre september sept sep", "octobre october oct",
        "novembre november nov", "decembre december dec",
    ), 1)
    for name in names.split()
}


class CeremaFrFrCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: cerema-fr-fr"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, referer: str | None = None, timeout: int = 45) -> str | None:
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15, check=False)
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = f"exit={result.returncode}"

            if attempt < len(waits):
                print(f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed ({last_error}) for {url}; retrying in {wait}s")
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw: str | bytes):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value) -> str:
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @staticmethod
    def _parse_fr_date(day: str, month: str, year: str) -> str:
        """Convert an explicit French/English calendar date, without guessing."""
        if not all(isinstance(value, str) for value in (day, month, year)):
            return ""
        month_key = ''.join(
            ch for ch in unicodedata.normalize("NFKD", month.strip().casefold())
            if not unicodedata.combining(ch)
        ).removesuffix(".")
        mm = _FR_MONTHS.get(month_key)
        dd, yyyy = day.strip(), year.strip()
        if not mm or not re.fullmatch(r"[0-9]{1,2}", dd) or not re.fullmatch(r"[0-9]{4}", yyyy):
            return ""
        try:
            return date(int(yyyy), int(mm), int(dd)).isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _node_id_from_article(article) -> str:
        """Extract numeric node ID from article[data-history-node-id] or class node--NNN."""
        if article is None:
            return ""
        nid = article.get("data-history-node-id", "").strip()
        if nid:
            return nid
        for cls in article.get("class", []):
            m = re.match(r"node--(\d+)$", cls)
            if m:
                return m.group(1)
        return ""

    @staticmethod
    def _pdf_from_soup(soup, base: str = _BASE_URL) -> tuple[str, str]:
        """Return (pdf_url, original_filename) for the first PDF link found."""
        if soup is None:
            return "", ""
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not href:
                continue
            if re.search(r"\.pdf(?:[?&#]|$)", href, re.I):
                full = href if href.startswith("http") else urljoin(base, href)
                # Extract filename from query param ?file=…/name.pdf or last path segment
                parsed = urlparse(href)
                qs_file = parse_qs(parsed.query).get("file", [""])
                if qs_file and qs_file[0]:
                    fname = qs_file[0].rsplit("/", 1)[-1]
                else:
                    fname = parsed.path.rsplit("/", 1)[-1] or ""
                return full, fname
        return "", ""

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int, list_url: str = _LIST_URL) -> str | None:
        url = list_url if page == 0 else f"{list_url}?page={page}"
        return self._curl(url, referer=_BASE_URL + "/")

    def _parse_list_page(self, raw: str) -> list[dict]:
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        for row in soup.select(".views-row"):
            try:
                art = row.find("article")
                if art is None:
                    continue

                node_id = self._node_id_from_article(art)

                # Link & title from h2 > a
                h2 = row.find("h2")
                link_node = h2.find("a", href=True) if h2 else row.find("a", href=True)
                if link_node is None:
                    continue
                href = link_node.get("href", "").strip()
                if not href:
                    continue
                url = urljoin(_BASE_URL, href)
                title = self._one_line(link_node.get_text(" ", strip=True))
                if not title:
                    continue

                # Date from .publication-date > .day / .month / .year
                date_str = ""
                pub_div = row.find(class_="publication-date")
                if pub_div:
                    day = pub_div.find(class_="day")
                    month = pub_div.find(class_="month")
                    year = pub_div.find(class_="year")
                    if day and month and year:
                        date_str = self._parse_fr_date(
                            day.get_text(strip=True),
                            month.get_text(strip=True),
                            year.get_text(strip=True),
                        )

                # Category
                cat_node = row.find(class_=re.compile(r"field--name-field-press.*category"))
                category = self._one_line(cat_node.get_text(" ", strip=True)) if cat_node else ""

                items.append({
                    "node_id": node_id,
                    "url": url,
                    "title": title,
                    "published_date": date_str,
                    "category": category,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list row parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> str | None:
        return self._curl(url, referer=_LIST_URL)

    def _parse_detail(self, raw: str, item: dict) -> dict:
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed on detail page")

        url = item["url"]
        title = item["title"]
        published_date = item.get("published_date", "")
        category = item.get("category", "")

        # Node ID: prefer from article class on detail page
        art = soup.find("article")
        node_id = self._node_id_from_article(art) or item.get("node_id", "")

        # Slug as fallback external_id
        slug = urlparse(url).path.strip("/").rsplit("/", 1)[-1] or url
        external_id = node_id if node_id else slug
        post_number = node_id if node_id else None

        # Refine title from h1 or og:title
        h1 = soup.find("h1")
        if h1:
            t = self._one_line(h1.get_text(" ", strip=True))
            if t:
                title = t
        if not title:
            meta_title = soup.find("meta", attrs={"property": "og:title"})
            if meta_title and meta_title.get("content"):
                title = self._one_line(meta_title.get("content", ""))

        # Abstract from field--name-body
        abstract = ""
        body_field = None
        if art:
            body_field = art.find(class_="field--name-body")
        if body_field is None:
            body_field = soup.find(class_="field--name-body")

        if body_field:
            for bad in body_field.select("script, style, noscript"):
                bad.decompose()
            parts = []
            for node in body_field.find_all(["p", "li", "div"], recursive=True):
                if node.find(["p", "li"]):
                    continue
                text = self._one_line(node.get_text(" ", strip=True))
                if text and len(text) > 20:
                    parts.append(text)
            if parts:
                abstract = "\n\n".join(parts)

        # Fallback: og:description / meta description
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            for attr_pair in [
                ("property", "og:description"),
                ("name", "description"),
            ]:
                meta = soup.find("meta", attrs={attr_pair[0]: attr_pair[1]})
                if meta and meta.get("content"):
                    text = self._clean(meta.get("content", ""))
                    if len(text) > len(abstract):
                        abstract = text
                if len(abstract) >= _ABSTRACT_MIN_CHARS:
                    break

        abstract = self._clean(abstract)

        # PDF
        pdf_url, original_filename = self._pdf_from_soup(soup)

        # Keywords from any tag/keyword field
        kw_parts = []
        for sel in ["field--name-field-tags", "field--name-field-keywords", "field--name-field-domaine"]:
            el = soup.find(class_=sel)
            if el:
                kw_parts += [self._one_line(a.get_text(" ", strip=True)) for a in el.find_all("a")]
        keywords = ",".join(kw_parts) if kw_parts else ""

        metadata = {
            "source": "cerema.fr press listing + detail page",
            "list_url": item.get("source_list_url", _LIST_URL),
            "canonical_url": url,
            "node_id": node_id,
            "slug": slug,
        }
        if pdf_url and len(abstract) < _ABSTRACT_MIN_CHARS:
            metadata["source_abstract_state"] = "empty" if not abstract else "short"
            metadata["source_fulltext_file_url"] = pdf_url
        if art and len(abstract) < _ABSTRACT_MIN_CHARS:
            resources = []
            seen_resources = set()
            for link in art.select('.field--name-field-paragraph-link-link .document-link a[href]'):
                href = link.get('href', '').strip()
                target = urljoin(url, href)
                if (not href or href.startswith('#') or urlparse(target).scheme not in ('http', 'https')
                        or not urlparse(target).netloc or target in seen_resources):
                    continue
                seen_resources.add(target)
                resources.append({"url": target, "label": self._one_line(link.get_text(' ', strip=True))})
            if resources:
                metadata["source_abstract_state"] = "empty" if not abstract else "short"
                metadata["source_resource_links"] = resources

        return {
            "id": external_id,
            "site_id": _SITE_ID,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "authors": "",
            "publisher": "Cerema",
            "department": "",
            "journal": "",
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def _parse_source_page(self, raw, scope, page):
        soup = self._parse_html(raw)
        selector = ".view-id-press_release" if scope == "dossier" else ".view-id-document_reference_press"
        view = soup.select_one(selector) if soup else None
        if view is None or soup.find("h1") is None:
            raise ValueError("expected original source catalogue not found")
        items = self._parse_list_page(raw)
        if len(items) != len(view.select(".views-row")):
            raise ValueError("source rows could not all be parsed")
        if len({x["url"] for x in items}) != len(items):
            raise ValueError("duplicate source URLs within page")
        links = {urljoin(_LIST_SCOPES[scope], a["href"])
                 for a in view.select(".pager__item--next a[href]")}
        if len(links) > 1:
            raise ValueError("ambiguous source next page")
        next_page = None
        if links:
            target = urlparse(next(iter(links)))
            expected = urlparse(_LIST_SCOPES[scope])
            if (target.scheme, target.netloc, target.path) != (expected.scheme, expected.netloc, expected.path):
                raise ValueError("source next page changed catalogue")
            if parse_qs(target.query) != {"page": [str(page + 1)]} or not items:
                raise ValueError("invalid source next page")
            next_page = page + 1
        elif items:
            for link in view.select(".pager__item--last a[href]"):
                last = parse_qs(urlparse(link["href"]).query).get("page", [])
                if last and last[0].isdigit() and int(last[0]) > page:
                    raise ValueError("source next page missing before declared last page")
        for item in items:
            item["source_list_url"] = _LIST_SCOPES[scope]
        return items, next_page

    def crawl(self, limit=None):
        self._exhausted = False
        self._source_failure = None
        if limit is not None and limit <= 0:
            return 0
        self._check_cancelled()
        cursor = dict(self.delivery_cursor or {})
        scope, page, index = cursor.get("scope", "dossier"), cursor.get("page", 0), cursor.get("next_index", 0)
        if (cursor.get("version", 1) != 1 or scope not in (*_LIST_SCOPES, "done")
                or type(page) is not int or page < 0 or type(index) is not int or index < 0):
            raise ValueError("invalid Cerema source cursor")
        expected_digest = cursor.get("list_digest", "")
        saved = 0
        start = time.monotonic()
        budget = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        while scope != "done":
            self._check_cancelled()
            if limit is not None and saved >= limit:
                return saved
            if time.monotonic() - start >= budget:
                return saved
            if page >= _MAX_PAGES:
                self._report_source_failure("configuration")
                return saved
            try:
                raw = self._fetch_list_page(page, _LIST_SCOPES[scope])
                self._check_cancelled()
                if not raw:
                    self._report_source_failure("fetch_failed")
                    raise ValueError("empty source response")
                items, next_page = self._parse_source_page(raw, scope, page)
                digest = hashlib.sha256(json.dumps([x["url"] for x in items], ensure_ascii=False).encode()).hexdigest()
                if (expected_digest and expected_digest != digest) or index > len(items):
                    raise ValueError("source list changed since checkpoint; overlap review required")
                for offset in range(index, len(items)):
                    self._check_cancelled()
                    if limit is not None and saved >= limit:
                        return saved
                    if time.monotonic() - start >= budget:
                        return saved
                    item = items[offset]
                    time.sleep(self._detail_delay)
                    self._check_cancelled()
                    raw_detail = self._fetch_detail(item["url"])
                    self._check_cancelled()
                    if not raw_detail:
                        self._report_source_failure("fetch_failed")
                        raise ValueError("empty detail response")
                    paper = self._parse_detail(raw_detail, item)
                    if (len(paper.get("abstract") or "") < _ABSTRACT_MIN_CHARS
                            and not paper.get("pdf_url")
                            and not json.loads(paper["metadata"]).get("source_resource_links")):
                        self._report_source_failure("item_failed")
                        raise ValueError("source abstract requires review before advancing cursor")
                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                    self._advance_cursor(dict(version=1, scope=scope, page=page,
                                              next_index=offset+1, list_digest=digest), items_done=1)
            except Exception:
                self._report_source_failure("invalid_response")
                raise
            if next_page is not None:
                page = next_page
            else:
                scope, page = ("document", 0) if scope == "dossier" else ("done", 0)
            index, expected_digest = 0, ""
            self._advance_cursor(dict(version=1, scope=scope, page=page, next_index=0, list_digest=""))
        self._check_cancelled()
        self._mark_exhausted()
        return saved
