# -*- coding: utf-8 -*-
"""Crawler for Inria press room (espace-presse) — Drupal 11 HTML."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class InriaFrFrCrawler(BaseCrawler):
    site_id = "inria-fr-fr"
    site_name = "Custom: inria-fr-fr"
    base_url = "https://www.inria.fr"

    _START_URL = "https://www.inria.fr/fr/espace-presse"
    _BACKOFF = (1, 3, 9)
    _CURL_TIMEOUT = 60
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        while True:
            # 25-minute wall-clock budget
            if time.time() - start_time > 25 * 60:
                print(f"[{self.site_id}] 25-minute budget reached; stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")
                break

            # espace-presse serves all press releases on a single page;
            # no ?page= query parameter exists for this endpoint.
            if page == 0:
                list_url = self._START_URL
            else:
                # No further list pages exist — end pagination naturally.
                break

            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}; stopping.")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] Failed to parse list page {page}; stopping.")
                break

            articles = soup.select("article.news")
            if not articles:
                print(f"[{self.site_id}] No articles at page {page}; stopping.")
                break

            new_items: list[tuple[str, str, str]] = []  # (detail_url, list_title, list_tag)
            for art in articles:
                about = (art.get("about") or "").strip()
                if not about or not about.startswith("/fr/"):
                    continue
                if about in seen_urls:
                    continue
                seen_urls.add(about)
                detail_url = urljoin(self.base_url, about)

                # Title from list (fallback if detail fetch fails)
                h3 = art.select_one("h3 span") or art.select_one("h3")
                list_title = self._clean_text(h3.get_text(" ", strip=True)) if h3 else ""

                # Main tag from list
                tag_el = art.select_one(".field--name-field-main-tag .field__item")
                list_tag = self._clean_text(tag_el.get_text(" ", strip=True)) if tag_el else ""

                new_items.append((detail_url, list_title, list_tag))

            if not new_items:
                print(f"[{self.site_id}] No new URLs at page {page}; stopping.")
                break

            print(f"[{self.site_id}] page {page}: {len(new_items)} new article(s) to fetch")

            for idx, (detail_url, list_title, list_tag) in enumerate(new_items):
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(detail_url, context=f"detail {detail_url}")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {detail_url} failed: empty response; skipping")
                        continue

                    detail_soup = self._make_soup(detail_raw, context=f"detail {detail_url}")
                    if detail_soup is None:
                        print(f"[{self.site_id}] item {detail_url} failed: parse error; skipping")
                        continue

                    parsed = self._parse_detail(detail_soup, detail_url, list_title, list_tag)
                    abstract = parsed.get("abstract", "")
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "abstract": abstract,
                        "authors": parsed["authors"],
                        "department": parsed["publisher"],
                        "category": parsed["category"],
                        "keywords": parsed["keywords"],
                        "published_date": parsed["published_date"],
                        "url": detail_url,
                        "pdf_url": parsed.get("pdf_url") or "",
                        "doi": "",
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {detail_url} failed: {exc}; continue")
                    continue

                if saved % 10 == 0 and saved > 0:
                    elapsed = int(time.time() - start_time)
                    limit_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str} ({elapsed}s elapsed)")

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, soup, url: str, list_title: str = "", list_tag: str = "") -> dict:
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

        # Title
        h1 = soup.select_one("h1 span") or soup.select_one("h1")
        title = self._clean_text(h1.get_text(" ", strip=True)) if h1 else list_title
        if not title:
            title = slug

        # Date — <time datetime="..."> element
        time_tag = soup.select_one("time[datetime]")
        raw_date = time_tag["datetime"] if time_tag else ""
        published_date = self._parse_date(raw_date)

        # Category — type-actualite field or infer from page content
        cat_el = (
            soup.select_one(".field--name-field-type-actualite")
            or soup.select_one(".type-actualite")
        )
        if cat_el:
            category = self._clean_text(cat_el.get_text(" ", strip=True))
        else:
            page_text = str(soup)
            if re.search(r"communiqu[ée]", page_text, re.IGNORECASE):
                category = "Communiqué de presse"
            elif list_tag:
                category = list_tag
            else:
                category = "Actualité"

        # Abstract — description field first, then body
        abstract_parts: list[str] = []

        desc_el = soup.select_one(".field--name-field-description")
        if desc_el:
            txt = self._clean_text(desc_el.get_text(" ", strip=True))
            if txt:
                abstract_parts.append(txt)

        body_el = soup.select_one(".field--name-body")
        if body_el:
            txt = self._clean_text(body_el.get_text(" ", strip=True))
            if txt and txt not in abstract_parts:
                abstract_parts.append(txt)

        # Fallback: largest paragraph block in main content
        if not abstract_parts:
            main_el = soup.select_one("main") or soup
            paras = main_el.select("p")
            para_texts = [self._clean_text(p.get_text(" ", strip=True)) for p in paras]
            long_paras = [t for t in para_texts if len(t) >= 80]
            if long_paras:
                abstract_parts.append(" ".join(long_paras[:5]))

        abstract = "\n\n".join(abstract_parts)

        # Keywords — tags + main-tag fields
        kw_tags = soup.select(
            ".field--name-field-tags .field__item, "
            ".field--name-field-main-tag .field__item"
        )
        kw_list = [self._clean_text(t.get_text(" ", strip=True)) for t in kw_tags]
        kw_list = [k for k in kw_list if k]
        keywords = ",".join(kw_list)

        # PDF URL — look for any .pdf link in the detail page
        pdf_url = ""
        for a in soup.select("a[href]"):
            href = a["href"] or ""
            if href.lower().endswith(".pdf"):
                pdf_url = urljoin(self.base_url, href)
                break

        # Original filename from PDF URL
        original_filename = ""
        if pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if tail.lower().endswith(".pdf"):
                original_filename = tail

        return {
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "authors": "",
            "publisher": "Inria",
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "pdf_url": pdf_url or None,
            "original_filename": original_filename or None,
            "metadata": {
                "posted_date": raw_date,
                "slug": slug,
                "source": "Inria espace-presse Drupal 11 HTML",
                "listEndpoint": self._START_URL,
                "category": category,
            },
        }

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self._CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self._CURL_TIMEOUT + 10
                )
                if result.returncode != 0:
                    raise RuntimeError(f"curl exit {result.returncode}")
                body = result.stdout
                if not body:
                    raise RuntimeError("empty response")
                return body.decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} attempt {attempt}/3 failed: {last_error}"
                )
                if attempt < 3:
                    time.sleep(self._BACKOFF[attempt - 1])
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML / text helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str | bytes, context: str = "HTML") -> BeautifulSoup | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = unescape(str(text)).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _parse_date(self, raw: str) -> str:
        if not raw:
            return ""
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return ""
