# -*- coding: utf-8 -*-
"""CNIC (Centro Nacional de Investigaciones Cardiovasculares) News Crawler.

Crawls https://www.cnic.es/en/actualidad/noticias
Drupal-based listing, 10 items/page, paginated via ?page=N (0-indexed).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.base_crawler import BaseCrawler

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_BASE = "https://www.cnic.es"
_LIST_URL = f"{_BASE}/en/actualidad/noticias"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MIN_ABSTRACT_SAVE = 50
_BACKOFF = (1, 3, 9)


def _make_soup(html: str):
    """Try parsers in order: html5lib → lxml → html.parser. Return None on total failure."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class CnicEsEnCrawler(BaseCrawler):
    site_id = "cnic-es-en"
    site_name = "Custom: cnic-es-en"
    base_url = _BASE

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _curl(self, url: str) -> str | None:
        """Fetch URL with curl; retry 3× with exponential backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "-A", _UA,
            "--max-time", "30",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                return r.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                print(
                    f"[cnic-es-en] curl attempt {attempt + 1}/3 failed "
                    f"for {url[:80]}: {exc}; retrying in {wait}s"
                )
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(el) -> str:
        if el is None:
            return ""
        return re.sub(r"\s+", " ", el.get_text(separator=" ")).strip()

    @staticmethod
    def _parse_date(span) -> str:
        """Return YYYY-MM-DD from a dc:date <span>, or empty string."""
        if span is None:
            return ""
        content = span.get("content", "")
        if content and len(content) >= 10:
            return content[:10]
        return ""

    def _parse_list_page(self, html: str) -> tuple[list[dict], str | None]:
        """Return (items, next_page_url) from a Drupal listing page."""
        soup = _make_soup(html)
        if soup is None:
            return [], None

        view = soup.find("div", class_=re.compile(r"view-Buscadores-noticias"))
        if view is None:
            return [], None

        items: list[dict] = []
        for row in view.find_all("tr", class_=re.compile(r"^row-")):
            try:
                title_el = row.find("div", class_="views-field-title")
                if not title_el:
                    continue
                a = title_el.find("a")
                if not a:
                    continue
                title = self._clean(a)
                href = a.get("href", "")
                if not href:
                    continue
                url = _BASE + href if href.startswith("/") else href

                date_span = row.find("span", attrs={"property": "dc:date"})
                pub_date = self._parse_date(date_span)

                cat_el = row.find("div", class_="views-field-field-notice-type")
                category = ""
                if cat_el:
                    fc = cat_el.find("div", class_="field-content")
                    category = self._clean(fc) if fc else ""

                intro_el = row.find("div", class_="views-field-field-introduction")
                intro = ""
                if intro_el:
                    fc = intro_el.find("div", class_="field-content")
                    intro = self._clean(fc) if fc else ""

                items.append({
                    "title": title,
                    "url": url,
                    "published_date": pub_date,
                    "category": category,
                    "list_intro": intro,
                })
            except Exception as exc:
                print(f"[cnic-es-en] list row parse error: {exc}; skipping")
                continue

        # next-page link from Drupal pager
        next_url: str | None = None
        pager_next = soup.find("li", class_=re.compile(r"pager-next"))
        if pager_next:
            next_a = pager_next.find("a")
            if next_a:
                href = next_a.get("href", "")
                if href:
                    next_url = (_BASE + href) if href.startswith("/") else href

        return items, next_url

    def _fetch_detail(self, url: str) -> dict:
        """Fetch a news detail page and extract body, node_id, intro, date."""
        html = self._curl(url)
        if not html:
            return {}

        soup = _make_soup(html)
        if soup is None:
            return {}

        result: dict = {}

        # Node ID from body class: "page-node-241702"
        body_tag = soup.find("body")
        if body_tag:
            body_cls = " ".join(body_tag.get("class", []))
            m = re.search(r"page-node-(\d+)", body_cls)
            if m:
                result["node_id"] = m.group(1)

        # Full article body from field-name-field-description
        desc_div = soup.find("div", class_=re.compile(r"field-name-field-description"))
        if desc_div:
            fi = desc_div.find("div", class_=re.compile(r"field-item"))
            if fi:
                result["body"] = self._clean(fi)

        # Brief intro from field-name-field-introduction
        intro_div = soup.find("div", class_=re.compile(r"field-name-field-introduction"))
        if intro_div:
            fi = intro_div.find("div", class_=re.compile(r"field-item"))
            if fi:
                result["intro"] = self._clean(fi)

        # Publication date from dc:date span
        date_span = soup.find("span", attrs={"property": "dc:date"})
        result["pub_date"] = self._parse_date(date_span)

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        page = 0
        next_url: str | None = None

        while True:
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[cnic-es-en] 25-minute wall-clock budget reached; stopping cleanly")
                break
            if limit is not None and saved >= limit:
                break
            if page >= _MAX_PAGES:
                print(f"[cnic-es-en] Safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            # first page has no ?page param; subsequent pages follow the pager link
            if next_url:
                list_url = next_url
            elif page == 0:
                list_url = _LIST_URL
            else:
                list_url = f"{_LIST_URL}?page={page}"
            next_url = None

            if page > 0 and page % 10 == 0:
                print(f"[cnic-es-en] page {page}: saved {saved}/{limit_str}")

            html = self._curl(list_url)
            if not html:
                print(f"[cnic-es-en] Failed to fetch listing page {page}; stopping")
                break

            items, next_url = self._parse_list_page(html)
            if not items:
                print(f"[cnic-es-en] No items on listing page {page}; done")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(item_url)

                    node_id = detail.get("node_id", "")
                    body = detail.get("body", "")
                    intro = detail.get("intro", "") or item.get("list_intro", "")
                    pub_date = detail.get("pub_date") or item.get("published_date", "")
                    category = item.get("category", "")

                    # Abstract: prefer full body; build from intro+context if short
                    if body and len(body) >= _MIN_ABSTRACT_SAVE:
                        abstract = body
                    else:
                        parts = []
                        if intro:
                            parts.append(intro)
                        parts.append(
                            f"Source: CNIC - Centro Nacional de Investigaciones "
                            f"Cardiovasculares Carlos III, Madrid, Spain."
                        )
                        if category:
                            parts.append(f"Category: {category}.")
                        if pub_date:
                            parts.append(f"Published: {pub_date}.")
                        abstract = " ".join(parts)

                    # Ensure abstract meets minimum length
                    if len(abstract) < _MIN_ABSTRACT_SAVE:
                        print(
                            f"[cnic-es-en] Skipping — abstract too short "
                            f"({len(abstract)} chars): {item['title'][:50]}"
                        )
                        continue

                    # external_id: prefer numeric node_id, fall back to slug
                    slug = item_url.rstrip("/").rsplit("/", 1)[-1]
                    external_id = node_id if node_id else slug

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": node_id if node_id else None,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": item.get("published_date", ""),
                        "url": item_url,
                        "authors": None,
                        "publisher": "CNIC - Centro Nacional de Investigaciones Cardiovasculares Carlos III",
                        "department": None,
                        "journal": None,
                        "pdf_url": None,
                        "keywords": category if category else None,
                        "category": category,
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": item.get("published_date", ""),
                            "originalFilename": None,
                            "node_id": node_id,
                            "category": category,
                            "list_intro": item.get("list_intro", ""),
                        }, ensure_ascii=False),
                    })
                    saved += 1
                    new_on_page += 1
                    print(f"[cnic-es-en] Saved {saved}/{limit_str}: {item['title'][:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[cnic-es-en] item {item_url} failed: {exc}; continuing")
                    continue

            if new_on_page == 0 and page > 0:
                print(f"[cnic-es-en] Page {page} yielded no new records; stopping")
                break

            if not next_url:
                print(f"[cnic-es-en] No next-page link after page {page}; done")
                break

            page += 1

        print(f"[cnic-es-en] Done. Total saved: {saved}")
        return saved
