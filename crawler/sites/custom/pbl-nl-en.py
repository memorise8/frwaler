# -*- coding: utf-8 -*-
"""Crawler for PBL Netherlands Environmental Assessment Agency – English publications.

Target: https://www.pbl.nl/en/publications?key=&type%5B28%5D=28&...
type[28] = Publication (1852 items as of 2026-05-11), page-0-indexed HTML pagination.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

# spec_from_file_location has no package context — add project root explicitly.
sys.path.insert(0, ".")

from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore


# ---------------------------------------------------------------------------
# Module-level helpers (used by both class methods and unit tests)
# ---------------------------------------------------------------------------

def _make_soup(raw):
    """Parse HTML with fallback parsers; never raise on malformed input."""
    if BeautifulSoup is None or not raw:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean(value):
    text = unescape(value or "")
    return re.sub(r"\s+", " ", text).strip()


def _tag_text(node):
    if not node:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _parse_date(raw):
    """Return ISO YYYY-MM-DD from any date-like string, or ''."""
    text = _clean(raw)
    if not text:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _filename_from_url(url):
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    if "." in tail and len(tail) <= 200:
        from urllib.parse import unquote
        return unquote(tail)
    return None


class PblNlEnCrawler(BaseCrawler):
    site_id = "pbl-nl-en"
    site_name = "Custom: pbl-nl-en"
    base_url = "https://www.pbl.nl"

    # type[28] = "Publication" (1 852 items); type[26] = "Report" (214 items)
    _LIST_BASE = (
        "https://www.pbl.nl/en/publications"
        "?key=&type%5B28%5D=28&published%5Bmin%5D=&published%5Bmax%5D="
    )
    _PAGE_SIZE = 10
    _SAFETY_CAP_PAGES = 200
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25 minutes
    _MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, accept=None, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept or 'text/html,application/xhtml+xml,*/*;q=0.8'}",
            "-H", "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
            except Exception as exc:
                last_error = str(exc)
            else:
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                last_error = f"exit={result.returncode}"
            if attempt < 2:
                wait = waits[attempt]
                print(f"[{self.site_id}] curl attempt {attempt+1}/3 failed ({last_error}); retry in {wait}s")
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _curl_head(self, url, timeout=20):
        """Follow redirects and return the final URL (for PDF resolution)."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skLI", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
            if result.returncode == 0:
                # Extract last Location: or the effective URL from final 200 block
                headers = result.stdout.decode("utf-8", errors="replace")
                locations = re.findall(r"(?i)^location:\s*(.+)$", headers, re.M)
                if locations:
                    loc = locations[-1].strip()
                    if loc.startswith("/"):
                        return self.base_url + loc
                    return loc
        except Exception:
            pass
        return url

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 0:
            return self._LIST_BASE
        return f"{self._LIST_BASE}&page={page}"

    def _parse_list_items(self, soup):
        if soup is None:
            return []
        items = []
        for row in soup.select(".view-publications-overview__row"):
            link = row.select_one("a.node-publication-teaser__read-more-link[href]")
            if not link:
                continue
            url = urljoin(self.base_url, link.get("href", "").strip())

            title = _tag_text(row.select_one(".node-publication-teaser__title"))
            if not title:
                label = link.get("aria-label") or ""
                title = re.sub(r"^Read more about\s+", "", label).strip()

            teaser = _tag_text(row.select_one(".node-publication-teaser__body"))
            category = _tag_text(row.select_one(".node-publication-teaser__type"))
            topic = _tag_text(row.select_one(".node-publication-teaser__topic"))
            time_tag = row.select_one(".node-publication-teaser__published-date time")
            published_date = _parse_date(time_tag.get("datetime") if time_tag else "")

            if url:
                items.append({
                    "title": title, "url": url, "teaser": teaser,
                    "category": category, "topic": topic,
                    "published_date": published_date,
                })
        return items

    @staticmethod
    def _has_next_page(soup):
        return soup is not None and soup.select_one("li.pager__item--next a[href]") is not None

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _extract_abstract(self, soup, list_item):
        content = soup.select_one(".node-publication-full__content")
        chunks = []

        if content:
            for child in content.find_all(recursive=False):
                classes = child.get("class", []) if hasattr(child, "get") else []
                if child.name == "div" and "node-publication-full__body" in classes:
                    for node in child.select(
                        ".node-publication-full__body-item.text-basic-html, "
                        ".paragraph-text-partial__body-item.text-basic-html"
                    ):
                        text = _tag_text(node)
                        if text:
                            chunks.append(text)

        if not chunks:
            meta = soup.select_one('meta[name="description"][content]')
            if meta:
                chunks.append(_clean(meta.get("content", "")))

        if list_item.get("teaser") and list_item["teaser"] not in chunks:
            chunks.append(list_item["teaser"])

        seen, deduped = set(), []
        for c in chunks:
            if c.lower() not in seen:
                seen.add(c.lower()); deduped.append(c)
        return "\n\n".join(deduped).strip()

    def _extract_authors(self, soup):
        authors = []
        seen = set()
        for selector in (
            ".node-publication-full__authors-item",
            ".node-publication-full__authors-external-item",
        ):
            for node in soup.select(selector):
                name = _tag_text(node)
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    authors.append(name)
        return authors

    def _extract_specs(self, soup):
        specs = {}
        for item in soup.select(".node-publication-full__specifications-item"):
            label = _tag_text(item.select_one("dt"))
            value = _tag_text(item.select_one("dd"))
            if label and value:
                specs[label] = value
        return specs

    def _extract_topics(self, soup, list_item):
        topics = []
        seen = set()
        for link in soup.select(".node-publication-full__main_topic a"):
            t = _tag_text(link)
            if t and t.lower() not in seen:
                seen.add(t.lower()); topics.append(t)
        if not topics:
            t = _tag_text(soup.select_one(".taxonomy-term-topic-teaser__name"))
            if t and t.lower() not in seen:
                seen.add(t.lower()); topics.append(t)
        if list_item.get("topic"):
            t = list_item["topic"]
            if t.lower() not in seen:
                topics.append(t)
        return topics

    def _extract_pdf(self, soup):
        """Return (download_path_or_url, is_file_reference) for the primary PDF link."""
        node = soup.select_one(".node-publication-full__links-primary a[data-file-reference][href]")
        if not node:
            node = soup.select_one(".node-publication-full__links-primary a[href]")
        if node:
            href = node.get("href", "").strip()
            if href:
                return href, node.has_attr("data-file-reference")
        return None, False

    @staticmethod
    def _normalize_doi(value):
        m = re.search(r"\b10\.\d{4,9}/[^\s\"<>]+", value or "", flags=re.I)
        if not m:
            return ""
        doi = m.group(0)
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
        return doi.rstrip(".,;:)]}")

    def _extract_doi(self, soup, specs):
        for label, value in specs.items():
            if "doi" in label.lower():
                doi = self._normalize_doi(value)
                if doi:
                    return doi
        for node in soup.select('a[href*="doi.org/"]'):
            doi = self._normalize_doi(node.get("href", "") or _tag_text(node))
            if doi:
                return doi
        return self._normalize_doi(soup.get_text(" ", strip=True))

    def _extract_node_id(self, soup, url):
        """Return Drupal node numeric ID (used as external_id for dedup)."""
        shortlink = soup.select_one('link[rel="shortlink"][href]')
        if shortlink:
            m = re.search(r"/node/(\d+)", shortlink.get("href", ""))
            if m:
                return m.group(1)
        settings = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
        if settings:
            try:
                data = json.loads(settings.string or settings.get_text() or "{}")
            except (TypeError, ValueError):
                data = {}
            current_path = ((data.get("path") or {}).get("currentPath") or "")
            m = re.search(r"node/(\d+)", current_path)
            if m:
                return m.group(1)
        # fallback: URL slug
        return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

    def _resolve_pdf_url(self, download_href):
        """Follow the /downloads/ 303 redirect to the real /system/files/ PDF URL."""
        if not download_href:
            return None, None
        full_url = urljoin(self.base_url, download_href)
        final_url = self._curl_head(full_url)
        if final_url and final_url != full_url:
            filename = _filename_from_url(final_url)
            return final_url, filename
        return full_url, _filename_from_url(full_url)

    def _parse_detail(self, raw, detail_url, list_item):
        soup = _make_soup(raw)
        if soup is None:
            raise ValueError("detail HTML could not be parsed")

        canonical = soup.select_one('link[rel="canonical"][href]')
        if canonical:
            detail_url = urljoin(self.base_url, canonical.get("href", detail_url))

        specs = self._extract_specs(soup)

        title = (
            specs.get("Publication title")
            or _tag_text(soup.select_one(".node-publication-full__page-title h1"))
            or _tag_text(soup.find("h1"))
            or list_item.get("title", "")
        )
        if not title:
            raise ValueError("missing title")

        time_tag = soup.select_one(".node-publication-full__published-date time")
        published_date = (
            _parse_date(time_tag.get("datetime") if time_tag else "")
            or _parse_date(specs.get("Publication date", ""))
            or list_item.get("published_date", "")
        )
        category = (
            specs.get("Publication type")
            or _tag_text(soup.select_one(".node-publication-full__type"))
            or list_item.get("category", "")
        )

        # Product number from specifications — used as post_number for incremental crawl
        product_number = specs.get("Product number") or specs.get("product number") or None

        authors = self._extract_authors(soup)
        topics = self._extract_topics(soup, list_item)

        download_href, is_file = self._extract_pdf(soup)
        if download_href and "/downloads/" in download_href:
            pdf_url, original_filename = self._resolve_pdf_url(download_href)
        elif download_href:
            pdf_url = urljoin(self.base_url, download_href)
            original_filename = _filename_from_url(pdf_url)
        else:
            pdf_url, original_filename = None, None

        abstract = self._extract_abstract(soup, list_item)
        doi = self._extract_doi(soup, specs)
        node_id = self._extract_node_id(soup, detail_url)

        # external_id = stable Drupal node ID for deduplication
        external_id = node_id
        # post_number = product number (numeric string) for incremental-crawl watermark
        post_number = product_number if product_number else (node_id if str(node_id).isdigit() else None)

        metadata = {
            "posted_date": published_date,
            "originalFilename": original_filename,
            "node_id": node_id,
            "product_number": product_number,
            "publicationType": category,
            "topics": topics,
            "specifications": specs,
            "slug": urlparse(detail_url).path.rstrip("/").rsplit("/", 1)[-1],
            "teaserAbstract": list_item.get("teaser", ""),
            "downloadHref": download_href,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "authors": "; ".join(authors) if authors else None,
            "abstract": abstract,
            "category": category,
            "keywords": ", ".join(topics) if topics else None,
            "published_date": published_date,
            "listed_date": published_date,
            "posted_date": published_date,
            "url": detail_url,
            "meta_url": detail_url,
            "pdf_url": pdf_url,
            "doi": doi or None,
            "department": None,
            "publisher": "PBL Netherlands Environmental Assessment Agency",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls: set = set()
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page < self._SAFETY_CAP_PAGES:
            # Wall-clock budget
            elapsed = time.monotonic() - started_at
            if elapsed >= self._MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget nearly reached at page {page}; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if page > 0:
                time.sleep(self._delay)

            list_url = self._list_url(page)
            raw = self._curl(list_url, referer=self.base_url + "/en/publications")
            if not raw:
                print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page} HTML parse failed; stopping")
                break

            items = self._parse_list_items(soup)
            if not items:
                print(f"[{self.site_id}] no items on page {page}; end of pagination")
                break

            # Deduplicate against already-seen URLs
            new_items = [it for it in items if it["url"] not in seen_urls]
            for it in new_items:
                seen_urls.add(it["url"])

            if not new_items:
                print(f"[{self.site_id}] page {page}: all URLs already seen; stopping")
                break

            for row_no, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break

                # Wall-clock check inside inner loop too
                if time.monotonic() - started_at >= self._MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget reached inside page {page}; stopping")
                    return saved

                detail_url = item.get("url", "")
                item_label = item.get("title") or f"page {page} row {row_no}"

                try:
                    time.sleep(self.detail_delay)
                    detail_raw = self._curl(
                        detail_url,
                        referer=list_url,
                        accept="text/html,application/xhtml+xml,*/*;q=0.8",
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    paper = self._parse_detail(detail_raw, detail_url, item)
                    abstract = (paper.get("abstract") or "").strip()
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping '{item_label[:60]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[pbl-nl-en] item {item_label[:60]} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup):
                print(f"[{self.site_id}] no next-page link after page {page}; done")
                break
            page += 1

        if page >= self._SAFETY_CAP_PAGES:
            print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP_PAGES} pages reached")

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
