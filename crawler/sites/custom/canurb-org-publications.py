# -*- coding: utf-8 -*-
"""Crawler for Canadian Urban Institute publications (canurb.org/publications/)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_SITE_ID = "canurb-org-publications"
_BASE_URL = "https://canurb.org"
_LIST_URL = "https://canurb.org/publications/"
_MIN_ABSTRACT = 50
_BACKOFF = (1, 3, 9)
_PAGE_SAFETY_CAP = 200
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


class CanurbOrgPublicationsCrawler(BaseCrawler):
    site_id = "canurb-org-publications"
    site_name = "Custom: canurb-org-publications"
    base_url = "https://canurb.org"

    # ------------------------------------------------------------------
    # Public crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        t_start = time.monotonic()

        # The publications listing page loads all ~93 entries on a single page
        # (FacetWP loop, no traditional pagination). We walk it page-by-page
        # anyway to stay compatible with any future pagination.
        page = 1
        while True:
            if limit is not None and saved >= limit:
                break
            if page > _PAGE_SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {_PAGE_SAFETY_CAP} pages reached; stopping")
                break
            if time.monotonic() - t_start > _CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] wall-clock budget exceeded; stopping cleanly")
                break

            page_url = _LIST_URL if page == 1 else f"{_LIST_URL}page/{page}/"
            raw = self._curl_get(page_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                break

            soup = self._make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page} parse failed; stopping")
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] page {page}: no records found; stopping")
                break

            # Deduplicate across pages
            new_records = [r for r in records if r["url"] not in seen_urls]
            for r in records:
                seen_urls.add(r["url"])

            if not new_records:
                print(f"[{self.site_id}] page {page}: all URLs already seen; stopping")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            for record in new_records:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - t_start > _CRAWL_BUDGET_SECS:
                    print(f"[{self.site_id}] wall-clock budget exceeded inside loop; stopping")
                    break

                try:
                    result = self._process_record(record)
                    if result:
                        saved += 1
                        lim_str = str(limit) if limit is not None else "∞"
                        print(f"[{self.site_id}] saved {saved}/{lim_str}: {record['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {record.get('url', '?')} failed: {exc}")
                    continue

                time.sleep(self._delay)

            # Check for next page link
            if not self._has_next_page(soup):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, soup) -> list[dict]:
        records = []
        for article in soup.select("article[class*='publications']"):
            link_tag = article.select_one("a[href]")
            if link_tag is None:
                continue
            url = link_tag.get("href", "").strip()
            if not url or "/publications/" not in url:
                continue
            url = urljoin(_BASE_URL, url)

            title_tag = article.select_one("h4")
            title = self._clean_text(title_tag) if title_tag else self._clean_text(link_tag)
            if not title:
                continue

            # Post ID from article class: post-9635
            post_id = ""
            for cls in article.get("class", []):
                m = re.match(r"^post-(\d+)$", cls)
                if m:
                    post_id = m.group(1)
                    break

            # Category from class: category-research-report → research-report
            category = ""
            for cls in article.get("class", []):
                if cls.startswith("category-"):
                    category = cls[len("category-"):].replace("-", " ").title()
                    break

            # Excerpt from <p> inside posts__content
            excerpt = ""
            content_div = article.select_one(".posts__content")
            if content_div:
                p = content_div.find("p")
                if p:
                    excerpt = self._clean_text(p)

            records.append({
                "post_id": post_id,
                "title": title,
                "url": url,
                "category": category,
                "excerpt": excerpt,
            })
        return records

    # ------------------------------------------------------------------
    # Detail-page fetch + parse
    # ------------------------------------------------------------------

    def _process_record(self, record: dict) -> bool:
        url = record["url"]
        raw = self._curl_get(url, context=f"detail {record['post_id'] or url}")
        if not raw:
            raise RuntimeError(f"detail fetch failed: {url}")

        soup = self._make_soup(raw)
        if soup is None:
            raise RuntimeError(f"detail parse failed: {url}")

        # Title: prefer detail h1 over list title
        h1 = soup.select_one("h1.entry-title")
        title = self._clean_text(h1) if h1 else record["title"]
        if not title:
            title = record["title"]

        # Post ID from body class (or from listing)
        post_id = record.get("post_id") or self._post_id_from_body(soup)
        external_id = post_id or self._slug_from_url(url)

        # Date from JSON-LD datePublished
        published_date = self._extract_published_date(soup) or ""

        # Abstract: og:description → list excerpt
        abstract = (
            self._meta(soup, "og:description")
            or self._meta(soup, "description")
            or record.get("excerpt", "")
        )
        if len(abstract) < _MIN_ABSTRACT:
            # Try to extract from the__content / with__content
            abstract = self._extract_body_text(soup) or abstract

        if len(abstract) < _MIN_ABSTRACT:
            print(f"[{self.site_id}] skipping {url}: abstract too short ({len(abstract)} chars)")
            return False

        # PDF URL
        pdf_url = self._find_pdf_url(soup, url)

        # Original filename from PDF URL
        original_filename = ""
        if pdf_url:
            seg = pdf_url.rstrip("/").split("/")[-1]
            if seg.lower().endswith(".pdf"):
                original_filename = seg

        # Category
        category = record.get("category", "")
        if not category:
            for cls in (soup.find("body") or soup).get("class", []):
                if cls.startswith("category-"):
                    category = cls[len("category-"):].replace("-", " ").title()
                    break

        # Keywords from tags
        keywords = self._extract_keywords(soup)

        metadata = {
            "post_id": post_id,
            "excerpt": record.get("excerpt", ""),
            "og_url": self._meta(soup, "og:url"),
        }

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_id or None,
            "title": title,
            "abstract": abstract,
            "authors": "",
            "publisher": "Canadian Urban Institute",
            "department": "",
            "journal": "",
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": pdf_url or None,
            "doi": "",
            "original_filename": original_filename or None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_published_date(self, soup) -> str:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            # Handle both single object and @graph array
            if isinstance(data, dict):
                items = data.get("@graph", [data])
            elif isinstance(data, list):
                items = data
            else:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw = item.get("datePublished") or item.get("dateModified") or ""
                if raw:
                    return self._normalize_date(raw)
        # Fallback: meta article:published_time
        raw = self._meta(soup, "article:published_time") or ""
        if raw:
            return self._normalize_date(raw)
        return ""

    def _extract_body_text(self, soup) -> str:
        # Try elementor widget text or the__content
        for sel in (".the__content", ".with__content", ".entry-content", ".elementor-text-editor"):
            node = soup.select_one(sel)
            if node:
                # Remove noise
                for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                if len(text) >= _MIN_ABSTRACT:
                    return text[:3000]
        return ""

    def _find_pdf_url(self, soup, base_url: str) -> str:
        # First: look for explicit PDF launch links
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if ".pdf" in href.lower():
                return urljoin(base_url, href)
        return ""

    def _extract_keywords(self, soup) -> str:
        tags: list[str] = []
        for a in soup.select(".posts__tag a, .tags a, .entry-tags a"):
            t = self._clean_text(a)
            if t:
                tags.append(t)
        return ", ".join(tags)

    def _post_id_from_body(self, soup) -> str:
        body = soup.find("body")
        if body:
            for cls in body.get("class", []):
                m = re.match(r"^postid-(\d+)$", cls)
                if m:
                    return m.group(1)
        return ""

    def _slug_from_url(self, url: str) -> str:
        path = url.rstrip("/").split("/")[-1]
        return path[:180] if path else url[:180]

    def _has_next_page(self, soup) -> bool:
        return bool(
            soup.select_one("a.next.page-numbers")
            or soup.select_one(".nav-previous a")
            or soup.select_one('a[rel="next"]')
        )

    def _meta(self, soup, name: str) -> str:
        for attrs in ({"name": name}, {"property": name}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                return self._clean_text(tag["content"])
        return ""

    def _normalize_date(self, raw: str) -> str:
        text = self._clean_text(raw)
        if not text:
            return ""
        # ISO datetime: 2025-06-20T13:58:44+00:00
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        # "June 2025" or "June 20, 2025"
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
        if m:
            try:
                dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        m = re.search(r"([A-Za-z]+)\s+(\d{4})", text)
        if m:
            try:
                dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %Y")
                return dt.strftime("%Y-%m-01")
            except ValueError:
                pass
        return ""

    def _clean_text(self, value) -> str:
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = unescape(str(value)).replace("\xa0", " ").replace("​", "").replace("﻿", "")
        return re.sub(r"\s+", " ", text).strip()

    def _make_soup(self, raw: str):
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-CA,en;q=0.9",
            "-H", f"Referer: {_BASE_URL}/",
            url,
        ]
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = last_error or f"curl exit {result.returncode}; empty"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"
            except Exception as exc:
                last_error = str(exc)

            wait = _BACKOFF[attempt]
            print(f"[{self.site_id}] {context} attempt {attempt + 1}/3 failed: {last_error}")
            if attempt < 2:
                time.sleep(wait)

        print(f"[{self.site_id}] {context} gave up after 3 attempts")
        return None
