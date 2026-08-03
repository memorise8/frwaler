# -*- coding: utf-8 -*-
"""Crawler for Collège de France English news filtered to type:4825.

Discovered live endpoints:
- Listing HTML: https://www.college-de-france.fr/en/news?f%5B0%5D=type%3A4825&page=N
- Drupal Views AJAX is advertised as /en/views/ajax with view search_news /
  all_news_embed, but the paged HTML endpoint is stable and carries the same
  rendered records plus pager state.
- Detail HTML: /en/news/{slug}, with JSON-LD NewsArticle and Drupal
  drupal-settings-json exposing the native node id.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


class CollegeDeFranceFrEnCrawler(BaseCrawler):
    USER_AGENT = "Mozilla/5.0"

    site_id = "college-de-france-fr-en"
    site_name = "Custom: college-de-france-fr-en"
    base_url = "https://www.college-de-france.fr"

    START_URL = "https://www.college-de-france.fr/en/news?f%5B0%5D=type%3A4825"
    LIST_PATH = "/en/news"
    LIST_FILTER = "type:4825"
    LISTING_ENDPOINT = "/en/news?f%5B0%5D=type%3A4825&page={page}"
    DRUPAL_AJAX_ENDPOINT = "/en/views/ajax"
    MAX_PAGES = 200
    WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100
    _CURL_META_MARKER = "__COLLEGE_DE_FRANCE_FR_EN_CURL_META__:"

    MONTHS = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()
        item_number = 0
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "∞"

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break
            if self._time_budget_reached(started_at):
                break

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}", referer=self.START_URL)
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_url, page)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_records = []
            for record in records:
                url = record.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            for record in new_records:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_reached(started_at):
                    break

                item_number += 1
                try:
                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(
                        record["url"],
                        context=f"item {item_number} detail",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_number} detail",
                    )
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

                    paper = self._to_paper(parsed)
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[college-de-france-fr-en] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network and parsing helpers
    # ------------------------------------------------------------------

    def _list_url(self, page):
        query = [("f[0]", self.LIST_FILTER)]
        if page:
            query.append(("page", str(page)))
        return f"{self.base_url}{self.LIST_PATH}?{urlencode(query)}"

    def _time_budget_reached(self, started_at):
        elapsed = time.monotonic() - started_at
        if elapsed >= self.WALL_CLOCK_BUDGET_SECONDS - 30:
            print(f"[{self.site_id}] wall-clock budget reached ({elapsed:.0f}s); exiting cleanly")
            return True
        return False

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
            "Accept: "
            + (
                accept
                or "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8"
            ),
            "-H",
            "Accept-Language: en-US,en;q=0.9,fr;q=0.7",
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
                body, http_code, _final_url = self._split_curl_output(stdout)
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code or "0") >= 400:
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
                    time.sleep(self.BACKOFF_SECONDS[attempt - 1])

        print(f"[{self.site_id}] {context} skipped after 3 failures: {last_error}")
        return ""

    def _split_curl_output(self, stdout):
        marker = "\n" + self._CURL_META_MARKER
        if marker not in stdout:
            return stdout, "", ""
        body, meta = stdout.rsplit(marker, 1)
        parts = meta.strip().split("\t", 1)
        http_code = parts[0].strip() if parts else ""
        final_url = parts[1].strip() if len(parts) > 1 else ""
        return body, http_code, final_url

    def _make_soup(self, raw, context="html"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] {context}: BeautifulSoup({parser}) failed: {exc}")
                continue
        return None

    def _parse_list(self, soup, list_url, page):
        records = []
        for card in soup.select("a.card-news[href]"):
            href = card.get("href") or ""
            url = urljoin(self.base_url, href)
            if "/en/news/" not in urlparse(url).path:
                continue

            title = self._first_text(card, [".card-news__title"])
            date_raw = self._first_text(card, [".card-news__date"])
            list_label = self._first_text(card, [".card-news__author"])
            image = None
            img = card.select_one("img[src]")
            if img:
                image = urljoin(self.base_url, img.get("src"))

            records.append(
                {
                    "url": url,
                    "title": title,
                    "listed_date_raw": date_raw,
                    "listed_date": self._parse_date(date_raw),
                    "list_label": list_label,
                    "image": image,
                    "list_page": page,
                    "list_url": list_url,
                }
            )
        return records

    def _has_next_page(self, soup):
        if soup.select_one('a[rel="next"], .pager__item--next a[href]'):
            return True
        return False

    def _parse_detail(self, soup, raw, record):
        settings = self._extract_drupal_settings(soup)
        news_article = self._extract_news_article(soup)

        title = self._clean_text(
            news_article.get("headline")
            or self._first_text(soup, [".field--name-news-title", "h1"])
            or record.get("title")
        )
        secondary_title = self._clean_text(
            news_article.get("alternativeHeadline")
            or self._first_text(soup, [".field--name-news-secondary-title"])
        )

        node_id = self._node_id_from_settings(settings)
        canonical = self._canonical_url(soup) or news_article.get("url") or record["url"]
        posted_date_raw = record.get("listed_date_raw")
        listed_date = record.get("listed_date") or self._parse_date(posted_date_raw)
        published_date_raw = (
            news_article.get("dateCreated")
            or self._meta_content(soup, "article:published_time")
            or posted_date_raw
        )
        published_date = self._parse_date(published_date_raw) or listed_date

        lead_text = self._first_text(soup, [".field--name-news-lead-in"])
        body_text = self._body_text(soup)
        jsonld_abstract = self._html_to_text(news_article.get("abstract"))
        jsonld_body = self._clean_text(news_article.get("articleBody"))
        abstract = self._compose_abstract(lead_text, body_text, jsonld_abstract, jsonld_body)

        about_names = self._about_names(news_article.get("about"))
        list_label = record.get("list_label") or ""
        authors, department = self._split_author_department("; ".join(about_names) or list_label)
        category = secondary_title or self._category_from_label(list_label) or "News"
        pdf_urls = self._pdf_urls(soup)
        pdf_url = pdf_urls[0] if pdf_urls else None
        original_filename = self._filename_from_url(pdf_url)

        metadata = {
            "posted_date": posted_date_raw,
            "listed_date": listed_date,
            "published_date_raw": published_date_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "drupal_current_path": self._settings_path(settings),
            "canonical_url": canonical,
            "list_record": record,
            "listing_endpoint": self.LISTING_ENDPOINT,
            "drupal_ajax_endpoint": self.DRUPAL_AJAX_ENDPOINT,
            "drupal_view": {
                "view_name": "search_news",
                "view_display_id": "all_news_embed",
            },
            "jsonld": news_article,
            "secondary_title": secondary_title,
            "about": about_names,
            "all_pdf_urls": pdf_urls,
            "image": record.get("image") or self._jsonld_image(news_article),
        }

        external_id = node_id or self._slug_from_url(canonical)
        post_number = node_id or self._slug_from_url(canonical)

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, canonical)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "Collège de France",
            "department": department,
            "journal": None,
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": self._keywords(soup),
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _to_paper(self, parsed):
        metadata = dict(parsed["metadata"])
        metadata.update(
            {
                "post_number": parsed.get("post_number"),
                "listed_date": parsed.get("listed_date"),
                "category": parsed.get("category"),
                "doi": parsed.get("doi"),
            }
        )
        return {
            "id": parsed.get("id"),
            "site_id": self.site_id,
            "external_id": parsed.get("external_id"),
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("listed_date"),
            "authors": parsed.get("authors"),
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department") or parsed.get("publisher"),
            "journal": parsed.get("journal"),
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    # ------------------------------------------------------------------
    # Detail extraction helpers
    # ------------------------------------------------------------------

    def _extract_drupal_settings(self, soup):
        script = soup.select_one('script[type="application/json"][data-drupal-selector="drupal-settings-json"]')
        if not script:
            return {}
        try:
            return json.loads(script.get_text() or "{}")
        except Exception:
            return {}

    def _extract_news_article(self, soup):
        for script in soup.select('script[type="application/ld+json"]'):
            text = script.string or script.get_text() or ""
            try:
                data = json.loads(text.strip())
            except Exception:
                continue
            for item in self._iter_jsonld(data):
                if isinstance(item, dict) and item.get("@type") == "NewsArticle":
                    return item
        return {}

    def _iter_jsonld(self, data):
        if isinstance(data, dict):
            yield data
            graph = data.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    yield from self._iter_jsonld(item)
        elif isinstance(data, list):
            for item in data:
                yield from self._iter_jsonld(item)

    def _settings_path(self, settings):
        path = settings.get("path") if isinstance(settings, dict) else {}
        return path.get("currentPath") if isinstance(path, dict) else None

    def _node_id_from_settings(self, settings):
        current_path = self._settings_path(settings) or ""
        match = re.search(r"node/(\d+)", current_path)
        return match.group(1) if match else None

    def _canonical_url(self, soup):
        link = soup.select_one('link[rel="canonical"][href]')
        if link:
            return urljoin(self.base_url, link.get("href"))
        return None

    def _meta_content(self, soup, prop_or_name):
        node = soup.select_one(f'meta[property="{prop_or_name}"][content]')
        if not node:
            node = soup.select_one(f'meta[name="{prop_or_name}"][content]')
        return node.get("content") if node else None

    def _body_text(self, soup):
        parts = []
        body = soup.select_one(".field--name-news-body")
        if not body:
            return ""
        for node in body.select(".field--name-text-content"):
            text = self._clean_text(node.get_text(" ", strip=True))
            if text:
                parts.append(text)
        return self._clean_text(" ".join(parts))

    def _compose_abstract(self, lead_text, body_text, jsonld_abstract, jsonld_body):
        visible_parts = [lead_text, body_text]
        if any(self._clean_text(part) for part in visible_parts):
            return self._best_abstract(visible_parts)
        return self._best_abstract([jsonld_abstract, jsonld_body])

    def _best_abstract(self, candidates):
        seen = set()
        parts = []
        for candidate in candidates:
            text = self._clean_text(candidate)
            if not text or text in seen:
                continue
            seen.add(text)
            parts.append(text)
        combined = self._clean_text(" ".join(parts))
        if len(combined) > 4000:
            return combined[:4000].rsplit(" ", 1)[0]
        return combined

    def _pdf_urls(self, soup):
        urls = []
        seen = set()
        for link in soup.select('a[href*=".pdf"]'):
            href = link.get("href") or ""
            if ".pdf" not in href.lower():
                continue
            url = urljoin(self.base_url, href)
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    def _about_names(self, about):
        if not about:
            return []
        items = about if isinstance(about, list) else [about]
        names = []
        for item in items:
            if isinstance(item, dict):
                name = self._clean_text(item.get("name"))
                if name:
                    names.append(name)
            elif isinstance(item, str):
                name = self._clean_text(item)
                if name:
                    names.append(name)
        return names

    def _split_author_department(self, text):
        text = self._clean_text(text)
        if not text or text.lower() in {"press release", "news"}:
            return None, None
        if ", chair " in text.lower():
            parts = re.split(r",\s*chair\s+", text, maxsplit=1, flags=re.I)
            author = self._clean_text(parts[0])
            department = self._clean_text("chair " + parts[1]) if len(parts) > 1 else None
            return author or None, department or None
        return None, text or None

    def _category_from_label(self, label):
        label = self._clean_text(label)
        if not label:
            return None
        if label.lower() in {"press release", "spotlight", "print edition"}:
            return label
        return "News"

    def _keywords(self, soup):
        content = self._meta_content(soup, "keywords")
        return self._clean_text(content) if content else None

    def _jsonld_image(self, news_article):
        image = news_article.get("image")
        if isinstance(image, dict):
            return image.get("image") or image.get("url") or image.get("thumbnail")
        if isinstance(image, str):
            return image
        return None

    # ------------------------------------------------------------------
    # Generic normalization helpers
    # ------------------------------------------------------------------

    def _first_text(self, root, selectors):
        for selector in selectors:
            node = root.select_one(selector)
            if node:
                text = self._clean_text(node.get_text(" ", strip=True))
                if text:
                    return text
        return ""

    def _html_to_text(self, value):
        if not value:
            return ""
        if "<" in value and ">" in value:
            soup = self._make_soup(value, context="inline html")
            if soup:
                return self._clean_text(soup.get_text(" ", strip=True))
        return self._clean_text(value)

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        return text

    def _parse_date(self, value):
        text = self._clean_text(value)
        if not text:
            return None
        text = re.sub(r"^Published on\s+", "", text, flags=re.I)

        iso_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if iso_match:
            return iso_match.group(0)

        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            pass

        match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
        if match:
            day = int(match.group(1))
            month = self.MONTHS.get(match.group(2).lower())
            year = int(match.group(3))
            if month:
                return f"{year:04d}-{month:02d}-{day:02d}"
        return None

    def _slug_from_url(self, url):
        path = urlparse(url).path.rstrip("/")
        return unquote(path.rsplit("/", 1)[-1]) or None

    def _filename_from_url(self, url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        return tail if tail else None
