# -*- coding: utf-8 -*-
"""Crawler for ACCC (Australian Competition and Consumer Commission) publications."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "accc-gov-au-about-us"
_BASE_URL = "https://www.accc.gov.au"
_LIST_URL = "https://www.accc.gov.au/about-us/publications"
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 100


class AcccGovAuAboutUsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: accc-gov-au-about-us"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-AU,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_list_page(self, page: int):
        url = _LIST_URL if page == 0 else f"{_LIST_URL}?page={page}"
        return self._curl(url, referer=_BASE_URL + "/")

    def _fetch_detail(self, url: str):
        return self._curl(url, referer=_LIST_URL)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
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
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path.strip("/")
        return path.rsplit("/", 1)[-1] if path else url

    @staticmethod
    def _node_id_from_html(soup) -> str:
        body = soup.find("body") if soup else None
        if body:
            for cls in body.get("class", []):
                m = re.match(r"page-node-(\d+)$", cls)
                if m:
                    return m.group(1)
        return ""

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str) -> list[dict]:
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        for card in soup.select("article.accc-card--clickable"):
            try:
                link_node = card.select_one(".accc-card__title a[href]")
                if link_node is None:
                    continue
                href = link_node.get("href", "").strip()
                if not href:
                    continue
                url = urljoin(_BASE_URL, href)

                title = self._one_line(link_node.get_text(" ", strip=True))
                if not title:
                    continue

                time_node = card.select_one("time[datetime]")
                date_raw = time_node.get("datetime", "") if time_node else ""
                published_date = self._parse_iso_date(date_raw)

                summary_node = card.select_one(
                    ".field--name-field-acccgov-summary .field__item"
                )
                card_summary = self._one_line(
                    summary_node.get_text(" ", strip=True) if summary_node else ""
                )

                items.append({
                    "url": url,
                    "title": title,
                    "published_date": published_date,
                    "card_summary": card_summary,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] card parse error: {exc}")
                continue

        return items

    @staticmethod
    def _parse_iso_date(raw: str) -> str:
        if not raw:
            return ""
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        return m.group(0) if m else ""

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw: str, item: dict) -> dict:
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed on detail page")

        url = item["url"]
        title = item["title"]
        published_date = item["published_date"]

        # Node ID for stable external_id
        node_id = self._node_id_from_html(soup)
        external_id = node_id or self._slug_from_url(url)

        # Refine title from page if available
        og_title = ""
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            og_title = meta.get("content", "").strip()
        if og_title:
            title = self._one_line(og_title)

        # Refine date from page
        dt_node = soup.select_one(
            ".field--name-field-accc-publication-date time[datetime]"
        )
        if dt_node:
            page_date = self._parse_iso_date(dt_node.get("datetime", ""))
            if page_date:
                published_date = page_date

        # Abstract: body field paragraphs
        abstract = self._extract_abstract(soup, item.get("card_summary", ""))

        # PDF URL (first .pdf link that is a local path or accc.gov.au domain)
        pdf_url = self._find_pdf(soup)

        # Topics / categories
        topic_nodes = soup.select(
            ".field--name-field-acccgov-topic a, "
            ".field--name-field-acccgov-audience a"
        )
        topics = [self._one_line(n.get_text(" ", strip=True)) for n in topic_nodes]
        topics = [t for t in topics if t]
        category = topics[0] if topics else ""
        keywords = list(dict.fromkeys(topics))  # dedup, preserve order

        # Department from canonical site name
        department = "Australian Competition and Consumer Commission"

        metadata = {
            "source": "ACCC publications HTML listing + detail page",
            "list_url": _LIST_URL,
            "canonical_url": url,
            "node_id": node_id,
            "card_summary": item.get("card_summary", ""),
        }

        return {
            "id": external_id,
            "site_id": _SITE_ID,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _extract_abstract(self, soup, card_summary: str) -> str:
        # Try body field first
        body_field = soup.select_one(
            "div.field--name-body .field__item, "
            "div.field--name-field-accc-body .field__item"
        )
        if body_field:
            for bad in body_field.select("script, style, noscript, figure, .accc-social-links"):
                bad.decompose()
            parts = []
            for node in body_field.find_all(["p", "li"], recursive=True):
                text = self._one_line(node.get_text(" ", strip=True))
                if text and len(text) > 20:
                    parts.append(text)
            if parts:
                abstract = "\n\n".join(parts)
                if len(abstract) >= _ABSTRACT_MIN_CHARS:
                    return self._clean(abstract)

        # Fallback: description field / summary field on detail page
        summary_node = soup.select_one(
            "div.field--name-field-acccgov-summary .field__item, "
            "div.field--name-field-accc-description .field__item"
        )
        if summary_node:
            text = self._clean(summary_node.get_text(" ", strip=True))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        # Fallback: og:description meta
        meta = soup.find("meta", attrs={"property": "og:description"})
        if meta and meta.get("content"):
            text = self._clean(meta.get("content", ""))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        # Fallback: description meta
        meta2 = soup.find("meta", attrs={"name": "description"})
        if meta2 and meta2.get("content"):
            text = self._clean(meta2.get("content", ""))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        # Last resort: card summary (may be too short)
        return self._clean(card_summary)

    @staticmethod
    def _find_pdf(soup) -> str:
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if not href:
                continue
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                if href.startswith("/"):
                    return urljoin(_BASE_URL, href)
                if href.startswith("http") and "accc.gov.au" in href:
                    return href
        return ""

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        crawl_start = time.time()

        for page in range(_MAX_PAGES):
            # Time budget: 25 minutes
            elapsed = time.time() - crawl_start
            if elapsed > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            try:
                raw = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] failed to fetch list page {page}: {exc}")
                break

            if not raw:
                print(f"[{_SITE_ID}] empty list response at page {page}; stopping")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] no cards on page {page}; stopping")
                break

            # Detect looping: if all URLs already seen, we've wrapped around
            new_urls = [it["url"] for it in items if it["url"] not in seen_urls]
            if not new_urls:
                print(f"[{_SITE_ID}] all URLs on page {page} already seen; stopping")
                break

            if page == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._detail_delay)
                    raw_detail = self._fetch_detail(url)
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {url} failed: empty detail response")
                        continue

                    paper = self._parse_detail(raw_detail, item)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] item {url} skipped: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
