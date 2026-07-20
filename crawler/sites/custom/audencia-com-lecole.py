# -*- coding: utf-8 -*-
"""Audencia press releases crawler."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class AudenciaComLecoleCrawler(BaseCrawler):
    site_id = "audencia-com-lecole"
    site_name = "Custom: audencia-com-lecole"
    base_url = "https://www.audencia.com"

    START_URL = "https://www.audencia.com/lecole/newsroom/communiques-de-presse"
    MAX_PAGES = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    CURL_TIMEOUT_SECONDS = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

    _CURL_META_MARKER = "__AUDENCIA_COM_LECOLE_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl Audencia's server-rendered Drupal press release listing."""
        saved = 0
        page = 0
        item_number = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_str = str(limit) if limit is not None else "inf"

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] done. Total saved: 0")
            return 0

        while True:
            if limit is not None and saved >= limit:
                break
            elapsed = time.monotonic() - started_at
            if elapsed >= self.WALL_CLOCK_BUDGET_SECONDS:
                print(f"[{self.site_id}] wall-clock budget reached ({elapsed:.0f}s); exiting cleanly")
                break
            if page >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}", referer=self.START_URL)
            if not raw:
                print(f"[{self.site_id}] list fetch failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            new_records = []
            for record in records:
                url = record.get("url") or ""
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break

            for record in new_records:
                if limit is not None and saved >= limit:
                    break
                elapsed = time.monotonic() - started_at
                if elapsed >= self.WALL_CLOCK_BUDGET_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget reached; stopping")
                    break

                item_number += 1
                try:
                    time.sleep(self.detail_delay)
                    detail_url = record.get("url") or ""
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"item {item_number} detail",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(detail_raw, context=f"item {item_number} detail")
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, detail_raw, record)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars): "
                            f"{(parsed.get('title') or record.get('title') or '')[:80]}"
                        )
                        continue

                    self._save_paper(self._build_paper(parsed))
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {parsed['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[audencia-com-lecole] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup, page):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _build_paper(self, parsed):
        metadata = parsed["metadata"]
        metadata_json = json.dumps(metadata, ensure_ascii=False)
        external_id = parsed["external_id"]

        return {
            "id": f"{self.site_id}:{external_id}" if external_id else None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": parsed["post_number"],
            "title": parsed["title"],
            "abstract": parsed["abstract"],
            "published_date": parsed["published_date"],
            "listed_date": parsed["listed_date"],
            "posted_date": parsed["listed_date"],
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
            "metadata": metadata_json,
        }

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
            str(self.CURL_TIMEOUT_SECONDS),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: "
            + (
                accept
                or "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
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
                    timeout=self.CURL_TIMEOUT_SECONDS + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _effective_url = self._split_curl_output(stdout, url)
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code:
                    status = int(http_code) if http_code.isdigit() else 0
                    if status >= 400:
                        raise RuntimeError(f"HTTP {http_code} for {url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {url}")
                return body

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker = "\n" + self._CURL_META_MARKER
        marker_pos = raw.rfind(marker)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + len(marker):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
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
        view = soup.select_one(".view-id-recherche_avancee.view-display-id-press_releases")
        if view is None:
            return []

        records = []
        seen = set()
        for card in view.select(".Card.-news"):
            link = card.select_one("a.Card-inner[href]") or card.select_one("a[href]")
            if link is None:
                continue
            href = (link.get("href") or "").strip()
            url = urljoin(self.base_url, href)
            if not self._is_detail_url(url) or url in seen:
                continue
            seen.add(url)

            title = self._clean_text(card.select_one(".Card-title")) or self._clean_text(link)
            if not title:
                continue

            time_node = card.select_one("time")
            listed_date_raw = self._clean_text(time_node)
            listed_datetime = (time_node.get("datetime") or "").strip() if time_node else ""
            listed_date = self._parse_date(listed_datetime) or self._parse_date(listed_date_raw)

            category = self._clean_text(card.select_one(".TagWrapper span, .TagWrapper a"))
            image = card.select_one("img")
            image_url = urljoin(self.base_url, image.get("src")) if image and image.get("src") else ""
            image_alt = self._clean_text(image.get("alt") or "") if image else ""

            records.append(
                {
                    "title": title,
                    "url": url,
                    "slug": self._slug_from_url(url),
                    "listed_date": listed_date,
                    "listed_date_raw": listed_date_raw,
                    "listed_datetime": listed_datetime,
                    "category": category,
                    "image_url": image_url,
                    "image_alt": image_alt,
                    "list_url": list_url,
                }
            )

        return records

    def _parse_detail(self, soup, raw, record):
        settings = self._drupal_settings(soup)
        path_info = settings.get("path") if isinstance(settings.get("path"), dict) else {}
        node_id = self._node_id_from_path(path_info.get("currentPath") or "")

        canonical = self._link_href(soup, "canonical") or record.get("url") or ""
        title = (
            self._meta_content(soup, property_name="og:title")
            or self._clean_text(soup.select_one("h1.Page-title, h1"))
            or record.get("title")
            or ""
        )
        title = self._normalize_spaces(title)

        article = soup.select_one("article.node--type-actualite") or soup.select_one("main") or soup
        body_text = self._extract_body_text(article)
        meta_desc = self._meta_content(soup, name="description")
        og_desc = self._meta_content(soup, property_name="og:description")
        abstract = body_text or self._clean_text(unescape(og_desc or meta_desc))

        published_date = (
            self._parse_date(self._meta_content(soup, property_name="article:published_time"))
            or self._parse_schema_date(soup)
            or self._parse_date(self._clean_text(article.select_one(".Page-subtitle time, .Page-subtitle")))
            or record.get("listed_date")
            or ""
        )
        listed_date = record.get("listed_date") or published_date

        categories = self._detail_categories(article)
        category = categories[0] if categories else record.get("category") or ""
        keywords = ",".join(self._dedupe([record.get("category") or ""] + categories))

        authors = self._extract_authors(article)
        contacts = self._extract_press_contacts(article)
        doi = self._extract_doi(article, raw)
        pdf_url, action_url = self._extract_pdf_and_action(article)
        original_filename = self._original_filename(pdf_url)

        external_id = node_id or record.get("slug") or self._slug_from_url(canonical)
        post_number = node_id or record.get("slug") or None

        metadata = {
            "posted_date": record.get("listed_date_raw") or listed_date,
            "listed_date_raw": record.get("listed_date_raw") or "",
            "listed_datetime": record.get("listed_datetime") or "",
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "drupal_current_path": path_info.get("currentPath") or "",
            "canonical_url": canonical,
            "slug": record.get("slug") or self._slug_from_url(canonical),
            "list_url": record.get("list_url") or "",
            "list_title": record.get("title") or "",
            "list_category": record.get("category") or "",
            "category_terms": categories,
            "press_release_url": action_url,
            "image_url": record.get("image_url") or "",
            "image_alt": record.get("image_alt") or "",
            "doi": doi,
            "press_contacts": contacts,
            "schema_article": self._schema_article(soup),
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": "Audencia",
            "department": "Newsroom; Relations presse",
            "journal": None,
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _extract_body_text(self, article):
        selectors = [
            ".Page-content .field--name-field-text-content",
            ".Page-content .EditorialWysiwyg",
        ]
        parts = []
        for selector in selectors:
            for node in article.select(selector):
                if node.find_parent(class_="PressContact"):
                    continue
                if "field--name-field-seo-text" in (node.get("class") or []):
                    continue
                text = self._clean_text(node)
                if not text:
                    continue
                if text not in parts:
                    parts.append(text)
            if parts:
                break
        return "\n\n".join(parts).strip()

    def _detail_categories(self, article):
        values = []
        for node in article.select(".TagWrapper a, .TagWrapper span"):
            text = self._clean_text(node)
            if text:
                values.append(text)
        return self._dedupe(values)

    def _extract_authors(self, article):
        text = self._clean_text(article)
        match = re.search(
            r"Les auteurs de l[’']etude\s*:\s*(.+?)(?:A propos|Contacter|$)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        authors = []
        for chunk in re.split(r"[;\n]| {2,}|(?:\s+\u2022\s+)", match.group(1)):
            name = self._clean_text(chunk)
            name = re.sub(r",\s*(professeur|phd|chercheuse|universite|audencia|ie business).*$", "", name, flags=re.I)
            if name and len(name.split()) >= 2:
                authors.append(name)
        return "; ".join(self._dedupe(authors))

    def _extract_press_contacts(self, article):
        contacts = []
        for block in article.select(".PressContactBlock"):
            name = self._clean_text(block.select_one(".field--name-field-name"))
            position = self._clean_text(block.select_one(".field--name-field-position"))
            email = self._clean_text(block.select_one(".field--name-field-email a"))
            if name or email:
                contacts.append({"name": name, "position": position, "email": email})
        return contacts

    def _extract_pdf_and_action(self, article):
        action_url = None
        pdf_url = None
        for link in article.select('a[href]'):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            absolute = urljoin(self.base_url, href)
            link_text = self._clean_text(link).lower()
            path = urlparse(absolute).path.lower()
            if not action_url and (
                "communique" in self._strip_accents(link_text)
                or "telecharger" in self._strip_accents(link_text)
            ):
                action_url = absolute
            if ".pdf" in path:
                pdf_url = absolute
                break
        return pdf_url, action_url

    def _extract_doi(self, article, raw):
        candidates = []
        for link in article.select('a[href]'):
            candidates.append(link.get("href") or "")
            candidates.append(self._clean_text(link))
        candidates.append(raw or "")

        doi_re = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
        for candidate in candidates:
            match = doi_re.search(candidate or "")
            if match:
                return match.group(0).rstrip(".,);")
        return ""

    def _drupal_settings(self, soup):
        node = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if node is None:
            return {}
        raw = node.string or node.get_text() or ""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _schema_article(self, soup):
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = node.string or node.get_text() or ""
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            graph = data.get("@graph") if isinstance(data, dict) else None
            if not isinstance(graph, list):
                continue
            for item in graph:
                if isinstance(item, dict) and item.get("@type") in {"NewsArticle", "Article"}:
                    return item
        return {}

    def _parse_schema_date(self, soup):
        article = self._schema_article(soup)
        if not article:
            return ""
        return self._parse_date(article.get("datePublished") or article.get("dateModified") or "")

    def _has_next_page(self, soup, page):
        next_link = soup.select_one(".view-id-recherche_avancee.view-display-id-press_releases .pager__item--next a[href]")
        if next_link is not None:
            return True
        for link in soup.select(".view-id-recherche_avancee.view-display-id-press_releases .pager a[href]"):
            href = link.get("href") or ""
            match = re.search(r"(?:\?|&)page=(\d+)", href)
            if match and int(match.group(1)) > page:
                return True
        return False

    def _list_url(self, page):
        return f"{self.START_URL}?page={page}"

    def _is_detail_url(self, url):
        parsed = urlparse(url)
        return parsed.netloc.endswith("audencia.com") and parsed.path.startswith("/actualites/")

    def _slug_from_url(self, url):
        return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

    def _node_id_from_path(self, path):
        match = re.search(r"node/(\d+)", path or "")
        return match.group(1) if match else ""

    def _original_filename(self, pdf_url):
        if not pdf_url:
            return None
        tail = unquote(urlparse(pdf_url).path.rstrip("/").rsplit("/", 1)[-1])
        if "." in tail and len(tail) <= 200:
            return tail
        return None

    def _link_href(self, soup, rel):
        node = soup.find("link", rel=rel)
        return self._clean_text(node.get("href") or "") if node else ""

    def _meta_content(self, soup, name=None, property_name=None):
        if property_name:
            node = soup.find("meta", property=property_name)
        else:
            node = soup.find("meta", attrs={"name": name})
        return self._clean_text(unescape(node.get("content") or "")) if node else ""

    def _parse_date(self, raw):
        text = self._clean_text(raw)
        if not text:
            return ""

        iso = re.search(r"\b(20\d{2}|19\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
        if iso:
            y, m, d = iso.groups()
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

        euro = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2}|19\d{2})\b", text)
        if euro:
            d, m, y = euro.groups()
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

        return ""

    def _clean_text(self, node):
        if node is None:
            return ""
        if isinstance(node, str):
            text = node
        else:
            text = node.get_text(" ", strip=True)
        return self._normalize_spaces(unescape(text.replace("\xa0", " ")))

    def _normalize_spaces(self, text):
        return re.sub(r"\s+", " ", text or "").strip()

    def _strip_accents(self, text):
        replacements = str.maketrans(
            "àáâãäåçèéêëìíîïñòóôõöùúûüýÿ",
            "aaaaaaceeeeiiiinooooouuuuyy",
        )
        return (text or "").lower().translate(replacements)

    def _dedupe(self, values):
        seen = set()
        result = []
        for value in values:
            cleaned = self._clean_text(value)
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                result.append(cleaned)
        return result
