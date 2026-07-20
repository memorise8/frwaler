# -*- coding: utf-8 -*-
"""Crawler for France Diplomatie (English) – latest news press room.

List pages: https://www.diplomatie.gouv.fr/en/press/news?page=N  (0-indexed)
Detail pages: /en/presse-et-ressources/decouvrir-et-informer/actualites/<slug>
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date_en(text: str) -> str:
    """Parse English ordinal date like 'May 13th 2026' → '2026-05-13'."""
    if not text:
        return ""
    # Strip "On : " prefix from detail page dates
    text = re.sub(r"(?i)on\s*:\s*", "", text).strip()
    # Remove ordinal suffixes (1st, 2nd, 3rd, 4th…)
    text = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", text, flags=re.I)
    # Try "Month Day Year"
    m = re.search(
        r"(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+(\d{1,2})\s+((?:19|20)\d{2})",
        text, flags=re.I,
    )
    if m:
        month = _MONTHS_EN.get(m.group(1).lower())
        day = int(m.group(2))
        year = int(m.group(3))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"
    # Try ISO already present
    iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text)
    if iso:
        return iso.group(0)
    return ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


class DiplomatieGouvFrEnCrawler(BaseCrawler):
    site_id = "diplomatie-gouv-fr-en"
    site_name = "Custom: diplomatie-gouv-fr-en"
    base_url = "https://www.diplomatie.gouv.fr"

    _LIST_BASE = "https://www.diplomatie.gouv.fr/en/press/news"
    _BACKOFF = (1, 3, 9)
    _CURL_TIMEOUT = 45
    _MIN_ABSTRACT = 50
    _SAFETY_CAP = 200
    _WALL_CLOCK_MINUTES = 25

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        page = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Limit / cap / budget checks
            if limit is not None and saved >= limit:
                break
            if page >= self._SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP} pages reached; stopping")
                break
            elapsed_min = (time.time() - start_time) / 60
            if elapsed_min >= self._WALL_CLOCK_MINUTES:
                print(f"[{self.site_id}] wall-clock budget of {self._WALL_CLOCK_MINUTES}m reached; stopping")
                break

            list_url = self._LIST_BASE if page == 0 else f"{self._LIST_BASE}?page={page}"

            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page} failed after retries; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] no records on page {page}; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            new_on_page = 0
            for record in records:
                if limit is not None and saved >= limit:
                    break

                detail_url = record.get("url", "")
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail …{detail_url[-55:]}",
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(detail_raw, context="detail")
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, detail_raw, record)
                    abstract = parsed.get("abstract", "")
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] skip …{detail_url[-55:]}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "post_number": parsed["post_number"],
                        "title": parsed["title"],
                        "abstract": abstract,
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"] or "",
                        "authors": "",
                        "publisher": "Ministère de l'Europe et des Affaires étrangères",
                        "department": parsed["department"],
                        "category": parsed["category"],
                        "keywords": parsed["keywords"],
                        "doi": "",
                        "original_filename": parsed["original_filename"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item …{detail_url[-55:]} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            if not self._has_next_page(soup, page):
                print(f"[{self.site_id}] no next page after page {page}; done")
                break

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page} yielded no new URLs; stopping to avoid loop")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self._CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.8,fr;q=0.5",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self._CURL_TIMEOUT + 10,
                )
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode != 0:
                    raise RuntimeError(f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} attempt {attempt}/3: {last_error}")
                if attempt < 3:
                    wait = self._BACKOFF[attempt - 1]
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str, context: str = "HTML") -> BeautifulSoup | None:
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
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    def _parse_list(self, soup: BeautifulSoup) -> list[dict]:
        records: list[dict] = []
        seen: set[str] = set()
        for card in soup.select("article.fr-card"):
            link = card.select_one("h3.fr-card__title a[href]")
            if not link:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            if url in seen:
                continue
            seen.add(url)

            title = _node_text(link)
            tag_node = card.select_one("li.taxonomy-term p.fr-tag")
            category = _node_text(tag_node)
            # Date is in fr-card__end (the bottom detail block)
            date_node = card.select_one("div.fr-card__end p.fr-card__detail")
            date_raw = _node_text(date_node)

            records.append({
                "title": title,
                "url": url,
                "slug": urlparse(url).path.strip("/").split("/")[-1],
                "category": category,
                "list_date_text": date_raw,
                "listed_date": _parse_date_en(date_raw),
            })
        return records

    def _parse_detail(self, soup: BeautifulSoup, raw_html: str, record: dict) -> dict:
        article = soup.select_one("article.node--view-mode-full") or soup

        # --- Node ID (Drupal internal numeric ID) ---
        node_id = ""
        settings_tag = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
        if settings_tag:
            settings_text = settings_tag.get_text()
            m = re.search(r'"currentPath"\s*:\s*"node[/\\]+(\d+)"', settings_text)
            if m:
                node_id = m.group(1)
        if not node_id:
            # Fallback: body class like page-node-8651
            body = soup.find("body")
            if body:
                for cls in (body.get("class") or []):
                    bm = re.match(r"page-node-(\d+)$", cls)
                    if bm:
                        node_id = bm.group(1)
                        break
        if not node_id:
            # Fallback: search raw HTML
            m2 = re.search(r'"currentPath"\s*:\s*"[^"]*node[/\\]+(\d+)', raw_html or "")
            if m2:
                node_id = m2.group(1)

        # --- Title ---
        h1 = article.select_one("h1")
        title = _node_text(h1) or record.get("title", "")

        # --- Category and document type ---
        tag_node = article.select_one("li.taxonomy-term p.fr-tag")
        category = _node_text(tag_node) or record.get("category", "")
        type_node = article.select_one("p.fr-text--sm.fr-mb-1w")
        doc_type = _node_text(type_node)

        # --- Date ---
        date_node = article.select_one(".diplomatie--hdp-date span.diplomatie--text-bold")
        if not date_node:
            date_node = article.select_one(".diplomatie--hdp-date")
        date_text = _node_text(date_node)
        published_date = _parse_date_en(date_text) or record.get("listed_date", "")

        # --- Canonical URL ---
        canonical = soup.select_one("link[rel='canonical']")
        url = (canonical.get("href", "").strip() if canonical else "") or record.get("url", "")
        if url and not url.startswith("http"):
            url = urljoin(self.base_url, url)
        url = url or record.get("url", "")

        # --- Abstract: body section (fr-mt-6w container) ---
        abstract = ""
        body_container = article.select_one(".fr-mt-6w .fr-col-md-8")
        if not body_container:
            # Fallback: any fr-col-md-8 that is NOT the header area (fr-my-2w)
            for div in article.select(".fr-col-md-8"):
                if "fr-my-2w" not in (div.get("class") or []):
                    body_container = div
                    break
        if body_container:
            # Remove noise elements
            for bad in body_container.select("script, style, noscript, .fr-share, nav"):
                bad.decompose()
            parts: list[str] = []
            for node in body_container.find_all(["p", "li", "h2", "h3", "h4"]):
                t = _node_text(node)
                if t and t not in parts:
                    parts.append(t)
            abstract = "\n\n".join(parts)

        if not abstract:
            # Final fallback: all <p> in the article with a minimum length threshold
            for bad in article.select("script, style, noscript"):
                bad.decompose()
            parts = []
            for p in article.find_all("p"):
                t = _node_text(p)
                if (
                    t and len(t) > 30
                    and t not in parts
                    and "Ministère" not in t
                ):
                    parts.append(t)
            abstract = "\n\n".join(parts)

        # --- PDF links ---
        pdf_url = ""
        original_filename = None
        for a in article.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I):
                pdf_url = urljoin(self.base_url, href)
                tail = href.rstrip("/").split("/")[-1].split("?")[0]
                original_filename = tail if tail else None
                break

        # --- IDs ---
        slug = record.get("slug", "") or urlparse(record.get("url", "")).path.strip("/").split("/")[-1]
        external_id = node_id or slug
        post_number = node_id if node_id else None

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": record.get("listed_date", ""),
            "url": url,
            "pdf_url": pdf_url,
            "department": doc_type,
            "category": category,
            "keywords": "",
            "original_filename": original_filename,
            "metadata": {
                "node_id": node_id,
                "doc_type": doc_type,
                "category": category,
                "posted_date": record.get("listed_date", ""),
                "list_date_text": record.get("list_date_text", ""),
                "slug": slug,
            },
        }

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _has_next_page(self, soup: BeautifulSoup, current_page: int) -> bool:
        nxt = soup.select_one("a.fr-pagination__link--next[href]")
        return nxt is not None and bool((nxt.get("href") or "").strip())


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------


def _node_text(node) -> str:
    if node is None:
        return ""
    text = node.get_text(" ", strip=True)
    text = unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()
