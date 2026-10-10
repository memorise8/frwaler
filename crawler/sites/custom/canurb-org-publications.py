# -*- coding: utf-8 -*-
"""Crawler for Canadian Urban Institute publications (canurb.org/publications/)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlsplit

from crawler.base_crawler import BaseCrawler
from crawler.canurb_traversal import CanurbTraversal

_SITE_ID = "canurb-org-publications"
_BASE_URL = "https://canurb.org"
_LIST_URL = "https://canurb.org/publications/"
_MIN_ABSTRACT = 50
_BACKOFF = (1, 3, 9)
_PAGE_SAFETY_CAP = 200
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


class CanurbOrgPublicationsCrawler(CanurbTraversal, BaseCrawler):
    site_id = "canurb-org-publications"
    site_name = "Custom: canurb-org-publications"
    base_url = "https://canurb.org"

    # ------------------------------------------------------------------
    # Public crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if self.delivery_mode == 'backfill':
            return self._crawl_catalogue_backfill(
                limit, max_pages=_PAGE_SAFETY_CAP, max_seconds=_CRAWL_BUDGET_SECS)
        return self._crawl_listing(limit)

    def _crawl_listing(self, limit=None):
        self._check_cancelled()
        saved = 0
        seen_urls: set[str] = set()
        t_start = time.monotonic()

        # The observed publications listing loads its entries on a single page
        # (FacetWP loop, no traditional pagination). We walk it page-by-page
        # anyway to stay compatible with any future pagination.
        page = 1
        while True:
            self._check_cancelled()
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
                self._report_source_failure('fetch_failed')
                print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                break

            soup = self._make_soup(raw)
            if soup is None:
                self._report_source_failure('invalid_response')
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
                self._report_source_failure('invalid_response')
                print(f"[{self.site_id}] page {page}: all URLs already seen; stopping")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            for record in new_records:
                self._check_cancelled()
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
                    self._report_source_failure('item_failed')
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
            if not url:
                continue
            url = urljoin(_BASE_URL, url)
            target = urlsplit(url)
            if target.scheme not in ('http', 'https') or not target.hostname or target.username or target.password:
                continue
            link_type = ('direct_pdf' if target.path.lower().endswith('.pdf') else
                         'external_publication' if target.hostname not in ('canurb.org', 'www.canurb.org') else
                         'detail_html')

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
                "link_type": link_type,
            })
        return records

    # ------------------------------------------------------------------
    # Detail-page fetch + parse
    # ------------------------------------------------------------------

    def _process_record(self, record: dict) -> bool:
        if record.get('link_type') in ('direct_pdf', 'external_publication'):
            return self._save_linked_publication(record)
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

        # The report's visible Date field can precede the webpage's posting
        # date by years and often supplies only a month or a year.
        web_date_published = self._extract_published_date(soup) or ""
        detail_publication_date = self._detail_publication_date(soup)
        published_date = (detail_publication_date if detail_publication_date is not None
                          else web_date_published)

        # Some publisher pages reuse another report's social description.
        # Prefer the current article's body and preserve the fallback source.
        social_description = (
            self._meta(soup, "og:description")
            or self._meta(soup, "description")
        )
        body_excerpt = self._extract_body_text(soup)
        abstract = body_excerpt or social_description or record.get("excerpt", "")
        abstract_source = ('detail_body_excerpt' if body_excerpt else
                           'social_description' if social_description else 'listing_excerpt')

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
            "pdf_candidates": self._pdf_links(soup, url),
            "abstract_source": abstract_source,
            "social_description_raw": social_description,
            "publication_date_source": ('detail_publication_date' if detail_publication_date is not None
                                        else 'web_date_published_fallback'),
            "web_date_published_raw": web_date_published,
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
            "listed_date": web_date_published,
            "url": url,
            "pdf_url": pdf_url or None,
            "doi": "",
            "original_filename": original_filename or None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

        self._last_processed_seq_id = self._save_paper(paper)
        return self._last_save_created

    def _save_linked_publication(self, record: dict) -> bool:
        """Use the publisher's card metadata for non-detail publication links.

        Binary files and other publishers' pages are separate resources. Keep
        their metadata provenance explicit; file retrieval is the file worker's
        responsibility and an external page is not a CANURB detail template.
        """
        abstract = record.get('excerpt', '')
        if len(abstract) < _MIN_ABSTRACT:
            return False
        url = record['url']
        post_id = record.get('post_id')
        pdf_url = url if record['link_type'] == 'direct_pdf' else None
        self._last_processed_seq_id = self._save_paper({
            'site_id': self.site_id,
            'external_id': post_id or self._slug_from_url(url),
            'post_number': post_id or None,
            'title': record['title'], 'abstract': abstract,
            'publisher': 'Canadian Urban Institute',
            'category': record.get('category', ''),
            'url': url, 'pdf_url': pdf_url,
            'original_filename': urlsplit(pdf_url).path.rsplit('/', 1)[-1] if pdf_url else None,
            'published_date': None, 'listed_date': None,
            'metadata': json.dumps({
                'post_id': post_id, 'excerpt': abstract,
                'metadata_source_url': _LIST_URL,
                'link_type': record['link_type'], 'linked_resource_fetched': False,
            }, ensure_ascii=False),
        })
        return self._last_save_created

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
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw = item.get("datePublished")
                if isinstance(raw, str) and raw.strip():
                    return raw
        # Fallback: meta article:published_time
        raw = self._meta(soup, "article:published_time") or ""
        if raw:
            return raw
        return ""

    def _detail_publication_date(self, soup) -> str | None:
        node = soup.select_one('article .single__date')
        if node is None:
            return None
        label = node.find('h3', recursive=False)
        value = node.find('div', recursive=False)
        if label is None or value is None or self._clean_text(label).casefold() != 'date:':
            return None
        # Empty, partial and unrecognized source values remain explicit; the
        # normalizer must not manufacture a day from the webpage timestamp.
        return self._clean_text(value)

    def _extract_body_text(self, soup) -> str:
        # Scope body selectors to the article, excluding surrounding sidebars.
        root = soup.find("article") or soup.find("main") or soup
        for sel in (".the__content", ".with__content", ".entry-content", ".elementor-text-editor"):
            node = root.select_one(sel)
            if node:
                # Remove noise
                for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                if len(text) >= _MIN_ABSTRACT:
                    return text[:3000]
        return ""

    def _find_pdf_url(self, soup, base_url: str) -> str:
        links = self._pdf_links(soup, base_url)
        # Publisher primary actions can occur after executive summaries and
        # translations. Preserve those variants, but prefer the full report.
        for labels in ({'launch'},
                       {'final report', 'read the final report', 'full report', 'download full report'},
                       {'read the report', 'read report', 'download report'}):
            for link in links:
                if link['label'].casefold() in labels:
                    return link['url']
        return links[0]['url'] if links else ''

    def _pdf_links(self, soup, base_url: str) -> list[dict]:
        links = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if '.pdf' not in href.lower():
                continue
            url = urljoin(base_url, href)
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                continue
            if url not in seen:
                links.append({'url': url, 'label': self._clean_text(a)})
                seen.add(url)
        return links

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
            "curl", "--tls-max", "1.3", "-sSL", "--compressed",
            "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-CA,en;q=0.9",
            "-H", f"Referer: {_BASE_URL}/",
            '--write-out', '\n%{http_code}', url,
        ]
        last_error = ""
        for attempt in range(3):
            self._check_cancelled()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                self._check_cancelled()
                stdout = result.stdout or b""
                body, separator, raw_status = stdout.rpartition(b'\n')
                status = int(raw_status) if separator and raw_status.isdigit() else 0
                if status in (401, 403, 429, 503):
                    self._report_source_failure('rate_limited' if status in (429, 503) else 'authentication_failed')
                    return None
                if result.returncode == 0 and status == 200 and body.strip():
                    from delivery.health.access_challenge import access_challenge
                    if access_challenge(body):
                        self._report_source_failure('authentication_failed')
                        return None
                    return body.decode("utf-8", errors="replace")
                if status and status < 500:
                    return None
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
