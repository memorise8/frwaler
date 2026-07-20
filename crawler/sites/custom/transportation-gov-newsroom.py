# -*- coding: utf-8 -*-
"""Crawler for transportation.gov newsroom press releases.

Starting URL: https://www.transportation.gov/newsroom/press-releases
Listing:      https://www.transportation.gov/newsroom/press-releases?page=N
Detail:       https://www.transportation.gov/briefing-room/{slug}
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


_SITE_ID = "transportation-gov-newsroom"
_BASE_URL = "https://www.transportation.gov"
_LIST_URL = f"{_BASE_URL}/newsroom/press-releases"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 50
_READER_PREFIX = "https://r.jina.ai/http://"


class TransportationGovNewsroomCrawler(BaseCrawler):
    site_id = "transportation-gov-newsroom"
    site_name = "Custom: transportation-gov-newsroom"
    base_url = "https://www.transportation.gov"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, referer: str | None = None, timeout: int = 60) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "20",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-H",
            "Connection: keep-alive",
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
                    timeout=timeout + 15,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{self.site_id}] curl failed {attempt}/{len(waits)} for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _is_access_denied(raw: str | None) -> bool:
        if not raw:
            return False
        head = raw[:1200].lower()
        return "access denied" in head and "edgesuite" in head

    @staticmethod
    def _reader_url(url: str) -> str:
        return _READER_PREFIX + url

    def _fetch(self, url: str, *, referer: str | None = None) -> tuple[str | None, str]:
        raw = self._curl(url, referer=referer)
        if raw and not self._is_access_denied(raw):
            return raw, "native"

        if raw and self._is_access_denied(raw):
            print(f"[{self.site_id}] Akamai denied native fetch, using reader fallback: {url}")

        reader_raw = self._curl(self._reader_url(url), referer=referer)
        if reader_raw and not self._is_access_denied(reader_raw):
            return reader_raw, "reader"
        return None, "failed"

    # ------------------------------------------------------------------
    # Text and date helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str):
        from bs4 import BeautifulSoup

        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup {parser} failed: {exc}")
        return None

    @staticmethod
    def _clean_text(value) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value) -> str:
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @staticmethod
    def _strip_markdown(value: str) -> str:
        text = value or ""
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#>]+", "", text)
        return TransportationGovNewsroomCrawler._one_line(text)

    @staticmethod
    def _parse_date(raw: str) -> str:
        text = TransportationGovNewsroomCrawler._one_line(raw)
        if not text:
            return ""
        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text)
        if match:
            return match.group(0)
        match = re.search(r"\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+)?([A-Z][a-z]+)\s+(\d{1,2}),\s+((?:19|20)\d{2})\b", text)
        if match:
            for fmt in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    return datetime.strptime(
                        f"{match.group(2)} {match.group(3)}, {match.group(4)}", fmt
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    continue
        match = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b", text)
        if match:
            return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
        return ""

    @staticmethod
    def _extract_raw_date(text: str) -> str:
        match = re.search(
            r"\b(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+)?[A-Z][a-z]+\s+\d{1,2},\s+(?:19|20)\d{2}\b",
            text or "",
        )
        return match.group(0) if match else ""

    @staticmethod
    def _markdown_content(raw: str) -> str:
        marker = "Markdown Content:"
        if marker in raw:
            return raw.split(marker, 1)[1]
        return raw or ""

    @staticmethod
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path.strip("/")
        slug = path.rsplit("/", 1)[-1] if path else url
        return slug or url

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        if "." in tail and len(tail) <= 200:
            return tail
        return None

    @staticmethod
    def _meta_content(soup, *keys) -> str:
        if soup is None:
            return ""
        for key in keys:
            node = soup.find("meta", attrs={"name": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
            node = soup.find("meta", attrs={"property": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
        return ""

    # ------------------------------------------------------------------
    # List parsers
    # ------------------------------------------------------------------

    def _parse_list_items(self, raw: str, source: str) -> list[dict]:
        if source == "reader" or raw.lstrip().startswith("Title: "):
            return self._parse_reader_list_items(raw)
        return self._parse_html_list_items(raw)

    def _parse_reader_list_items(self, raw: str) -> list[dict]:
        text = self._markdown_content(raw)
        if "## Press Releases" in text:
            text = text.split("## Press Releases", 1)[1]
        if "#### Pagination" in text:
            text = text.split("#### Pagination", 1)[0]

        lines = [line.rstrip() for line in text.splitlines()]
        items = []
        seen = set()
        idx = 0
        while idx < len(lines):
            if self._one_line(lines[idx]).lower() != "press release":
                idx += 1
                continue

            idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
            if idx >= len(lines):
                break

            link_line = lines[idx].strip()
            match = re.match(r"#+\s+\[([^\]]+)\]\((https?://[^)]+)\)", link_line)
            if not match:
                continue
            title = self._strip_markdown(match.group(1))
            url = match.group(2).split(" ", 1)[0]
            idx += 1

            while idx < len(lines) and not lines[idx].strip():
                idx += 1
            listed_raw = lines[idx].strip() if idx < len(lines) else ""
            listed_date = self._parse_date(listed_raw)
            idx += 1

            teaser_lines = []
            while idx < len(lines):
                line = lines[idx].strip()
                if self._one_line(line).lower() == "press release" or line.startswith("#### Pagination"):
                    break
                if line:
                    teaser_lines.append(line)
                idx += 1

            if url in seen:
                continue
            seen.add(url)
            slug = self._slug_from_url(url)
            items.append(
                {
                    "title": title,
                    "url": url,
                    "slug": slug,
                    "listed_date": listed_date,
                    "listed_date_raw": listed_raw,
                    "teaser": self._strip_markdown(" ".join(teaser_lines)),
                    "category": "Press Release",
                    "source": "reader_markdown",
                }
            )

        return items

    def _parse_html_list_items(self, raw: str) -> list[dict]:
        soup = self._make_soup(raw)
        if soup is None:
            return []

        items = []
        seen = set()
        for link in soup.select('a[href*="/briefing-room/"]'):
            href = link.get("href", "").strip()
            url = urljoin(self.base_url, href)
            if not url or url in seen:
                continue

            title = self._one_line(link.get_text(" ", strip=True))
            if not title:
                continue

            container = self._best_list_container(link, title)
            container_text = self._clean_text(container.get_text("\n", strip=True) if container else "")
            listed_raw = self._extract_raw_date(container_text)
            listed_date = self._parse_date(listed_raw)
            teaser = self._teaser_from_container_text(container_text, title, listed_raw)

            seen.add(url)
            slug = self._slug_from_url(url)
            items.append(
                {
                    "title": title,
                    "url": url,
                    "slug": slug,
                    "listed_date": listed_date,
                    "listed_date_raw": listed_raw,
                    "teaser": teaser,
                    "category": "Press Release",
                    "source": "native_html",
                }
            )
        return items

    def _best_list_container(self, link, title: str):
        node = link
        for _ in range(6):
            node = node.parent
            if node is None:
                return link.parent
            text = self._clean_text(node.get_text("\n", strip=True))
            if title in text and self._extract_raw_date(text):
                return node
        return link.parent

    def _teaser_from_container_text(self, text: str, title: str, raw_date: str) -> str:
        lines = [self._one_line(line) for line in (text or "").splitlines()]
        skip_values = {"Press Release", title, raw_date}
        kept = [line for line in lines if line and line not in skip_values]
        return " ".join(kept[:4]).strip()

    # ------------------------------------------------------------------
    # Detail parsers
    # ------------------------------------------------------------------

    def _parse_detail(self, raw: str, source: str, item: dict) -> dict:
        if source == "reader" or raw.lstrip().startswith("Title: "):
            return self._parse_reader_detail(raw, item)
        return self._parse_html_detail(raw, item)

    def _parse_reader_detail(self, raw: str, item: dict) -> dict:
        title_match = re.search(r"^Title:\s*(.+)$", raw or "", flags=re.M)
        title = self._strip_markdown(title_match.group(1)) if title_match else item.get("title", "")

        published_match = re.search(r"^Published Time:\s*(.+)$", raw or "", flags=re.M)
        raw_published_time = published_match.group(1).strip() if published_match else ""

        body = self._markdown_content(raw)
        body_lines = []
        in_article = False
        subtitle = ""
        related_links = []
        pdf_url = None
        for line in body.splitlines():
            stripped = line.strip()
            if not in_article:
                article_header = re.match(r"##\s+(.+)$", stripped)
                if article_header and self._strip_markdown(article_header.group(1)) == title:
                    in_article = True
                continue

            if not stripped:
                if body_lines and body_lines[-1] != "":
                    body_lines.append("")
                continue

            if stripped.startswith("## ") and body_lines:
                break

            for label, href in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", stripped):
                href = href.split(" ", 1)[0]
                related_links.append({"label": self._strip_markdown(label), "url": href})
                if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I) and not pdf_url:
                    pdf_url = href

            if stripped.startswith("*   "):
                stripped = "- " + stripped[4:].strip()
            if not subtitle and stripped.startswith("_") and stripped.endswith("_"):
                subtitle = self._strip_markdown(stripped)
            body_lines.append(stripped)

        abstract = self._reader_body_to_text("\n".join(body_lines), title)
        if not abstract:
            abstract = item.get("teaser", "")

        detail_date_raw = self._extract_raw_date(body)
        published_date = self._parse_date(detail_date_raw) or item.get("listed_date") or self._parse_date(raw_published_time)

        slug = item.get("slug") or self._slug_from_url(item.get("url", ""))
        external_id = slug
        post_number = slug

        metadata = {
            "source": "transportation.gov rendered Markdown via r.jina.ai fallback",
            "list_endpoint": _LIST_URL,
            "detail_endpoint": item.get("url"),
            "posted_date": item.get("listed_date_raw") or item.get("listed_date"),
            "listed_date": item.get("listed_date"),
            "listed_teaser": item.get("teaser"),
            "published_time_raw": raw_published_time,
            "detail_date_raw": detail_date_raw,
            "slug": slug,
            "post_number": post_number,
            "node_id": None,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "related_links": related_links[:30],
        }

        original_filename = self._filename_from_url(pdf_url)
        metadata["originalFilename"] = original_filename

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": item.get("listed_date"),
            "posted_date": item.get("listed_date"),
            "authors": "",
            "publisher": "U.S. Department of Transportation",
            "department": "U.S. Department of Transportation",
            "journal": None,
            "url": item.get("url"),
            "pdf_url": pdf_url,
            "keywords": "",
            "category": item.get("category") or "Press Release",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _reader_body_to_text(self, markdown: str, title: str) -> str:
        lines = []
        for raw_line in (markdown or "").splitlines():
            line = raw_line.strip()
            if not line:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            if self._strip_markdown(line) == title:
                continue
            lines.append(self._strip_markdown(line))

        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return self._clean_text(text)

    def _parse_html_detail(self, raw: str, item: dict) -> dict:
        soup = self._make_soup(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML")

        title = (
            self._meta_content(soup, "og:title")
            or self._one_line(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
            or item.get("title", "")
        )
        canonical_url = self._meta_content(soup, "og:url") or item.get("url")

        body = soup.find("body")
        node_id = None
        if body:
            for cls in body.get("class", []):
                match = re.match(r"page-node-(\d+)$", cls)
                if match:
                    node_id = match.group(1)
                    break

        published_raw = self._date_from_detail_soup(soup)
        published_date = self._parse_date(published_raw) or item.get("listed_date")

        container = (
            soup.select_one(".field--name-body")
            or soup.select_one(".node-main-body")
            or soup.find("article")
            or soup.find("main")
        )
        abstract = self._extract_detail_text(container, title)
        if not abstract:
            abstract = self._meta_content(soup, "description", "og:description") or item.get("teaser", "")
        abstract = self._clean_text(abstract)

        pdf_url = None
        related_links = []
        for link in soup.select("a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            label = self._one_line(link.get_text(" ", strip=True))
            if href and label:
                related_links.append({"label": label, "url": href})
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I) and not pdf_url:
                pdf_url = href

        keywords = self._meta_content(soup, "keywords")
        doi = ""
        doi_match = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", raw or "")
        if doi_match:
            doi = doi_match.group(1).rstrip(".,)")

        slug = item.get("slug") or self._slug_from_url(canonical_url or item.get("url", ""))
        external_id = node_id or slug
        post_number = node_id or slug
        original_filename = self._filename_from_url(pdf_url)

        metadata = {
            "source": "transportation.gov native HTML",
            "list_endpoint": _LIST_URL,
            "detail_endpoint": canonical_url,
            "posted_date": item.get("listed_date_raw") or item.get("listed_date"),
            "listed_date": item.get("listed_date"),
            "listed_teaser": item.get("teaser"),
            "detail_date_raw": published_raw,
            "slug": slug,
            "post_number": post_number,
            "node_id": node_id,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "related_links": related_links[:30],
            "originalFilename": original_filename,
        }

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": item.get("listed_date"),
            "posted_date": item.get("listed_date"),
            "authors": "",
            "publisher": "U.S. Department of Transportation",
            "department": "U.S. Department of Transportation",
            "journal": None,
            "url": canonical_url or item.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": item.get("category") or "Press Release",
            "doi": doi or None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _date_from_detail_soup(self, soup) -> str:
        time_node = soup.find("time")
        if time_node:
            return time_node.get("datetime") or time_node.get_text(" ", strip=True)
        main = soup.find("main") or soup.find("article") or soup
        return self._extract_raw_date(main.get_text("\n", strip=True))

    def _extract_detail_text(self, container, title: str) -> str:
        if container is None:
            return ""
        for bad in container.select("script, style, noscript, nav, footer, header, form, iframe"):
            bad.decompose()
        parts = []
        for node in container.find_all(["p", "li"], recursive=True):
            text = self._one_line(node.get_text(" ", strip=True))
            if text and text != title and "Subscribe Now" not in text:
                parts.append(text)
        if not parts:
            text = self._one_line(container.get_text(" ", strip=True))
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "inf"

        for page in range(_MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}, stopping")
                break
            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")
            if page == _MAX_PAGES - 1:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

            page_url = _LIST_URL if page == 0 else f"{_LIST_URL}?page={page}"
            raw, source = self._fetch(page_url, referer=self.base_url)
            if not raw:
                print(f"[{self.site_id}] list page {page}: fetch failed, stopping")
                break

            items = self._parse_list_items(raw, source)
            if not items:
                print(f"[{self.site_id}] list page {page}: no records, stopping")
                break

            new_items = []
            for item in items:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] list page {page}: all records already seen, stopping")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] 25-minute budget reached during page {page}, stopping")
                    return saved

                item_label = item.get("url") or f"page {page} item {idx}"
                try:
                    time.sleep(self.detail_delay)
                    detail_raw, detail_source = self._fetch(item["url"], referer=page_url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_label} failed: empty detail")
                        continue

                    paper = self._parse_detail(detail_raw, detail_source, item)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

        print(f"[{self.site_id}] done: saved {saved} documents")
        return saved
