# -*- coding: utf-8 -*-
"""STAMI English academic article crawler.

List pages are the FacetWP-backed WordPress archive:
https://stami.no/en/publication/?fwp_publikasjontype=academic-article

Detail pages are WordPress publication pages. WordPress also exposes a native
detail JSON endpoint at /en/wp-json/wp/v2/publication/{post_id}; the public
detail page contains the article abstract, author list, journal reference, and
the DOI/original-publication link.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from copy import copy
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_START_URL = "https://stami.no/en/publication/?fwp_publikasjontype=academic-article"
_ARCHIVE_BASE = "https://stami.no/en/publication/"
_ACADEMIC_FILTER = "fwp_publikasjontype=academic-article"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_WAITS = (1, 3, 9)
_BS_PARSERS = ("html5lib", "lxml", "html.parser")


def _make_soup(raw: str):
    """Build BeautifulSoup with the required parser fallback chain."""
    from bs4 import BeautifulSoup

    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(str(value))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _strip_tags(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return _clean_text(value)


def _iso_date(value: str | None) -> str | None:
    """Return YYYY-MM-DD from common HTML/WP date strings."""
    if not value:
        return None
    value = str(value).strip()
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    match = re.search(r"\b(\d{4})\b", value)
    if match:
        return f"{match.group(1)}-01-01"
    return None


def _year_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", str(value))
    if match:
        return f"{match.group(0)}-01-01"
    return _iso_date(value)


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    return unquote(path.split("/")[-1]) or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    name = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    if name and "." in name and len(name) <= 240:
        return name
    return None


def _doi_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"doi\.org/(10\.\d{4,9}/\S+)", url, re.I)
    if match:
        return unquote(match.group(1)).rstrip(").,;")
    match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", url, re.I)
    if match:
        return unquote(match.group(1)).rstrip(").,;")
    return None


def _is_probable_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    return path.endswith(".pdf") or "format=pdf" in query or "download=pdf" in query


class StamiNoEnCrawler(BaseCrawler):
    site_id = "stami-no-en"
    site_name = "Custom: stami-no-en"
    base_url = "https://stami.no"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_bytes(
        self,
        url: str,
        *,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        referer: str | None = None,
        method: str = "GET",
        data: str | None = None,
        content_type: str | None = None,
    ) -> bytes | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            "30",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if content_type:
            cmd.extend(["-H", f"Content-Type: {content_type}"])
        if method.upper() != "GET":
            cmd.extend(["-X", method.upper()])
        if data is not None:
            cmd.extend(["--data", data])
        cmd.append(url)

        last_error = ""
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=40)
                body = result.stdout or b""
                if (
                    result.returncode == 0
                    and body.strip()
                    and b"Attention Required! | Cloudflare" not in body[:2000]
                    and b"cf-error-code" not in body[:5000]
                ):
                    return body
                err = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = err or f"empty/blocked response (curl rc={result.returncode})"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(_RETRY_WAITS):
                print(f"[stami-no-en] curl failed for {url} (attempt {attempt}/3): {last_error}; retry in {wait}s")
                time.sleep(wait)

        print(f"[stami-no-en] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _get_text(self, url: str, *, referer: str | None = None) -> str | None:
        raw = self._curl_bytes(url, referer=referer)
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")

    def _get_json(self, url: str, *, referer: str | None = None):
        raw = self._curl_bytes(url, accept="application/json,*/*;q=0.8", referer=referer)
        if raw is None:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    def _head_filename(self, url: str) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skLI",
            "--max-time",
            "30",
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: */*",
            url,
        ]
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=40)
                headers = result.stdout.decode("utf-8", errors="replace")
                match = re.search(r"content-disposition:.*?filename\*?=(?:UTF-8''|\"?)([^\";\r\n]+)", headers, re.I)
                if match:
                    return unquote(match.group(1).strip())
                if result.returncode == 0:
                    return None
            except Exception:
                pass
            if attempt < len(_RETRY_WAITS):
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return _START_URL
        return f"{_ARCHIVE_BASE}page/{page}/?{_ACADEMIC_FILTER}"

    def _parse_fwp_settings(self, soup) -> dict:
        for script in soup.find_all("script"):
            text = script.string or script.get_text() or ""
            if "window.FWP_JSON" not in text:
                continue
            match = re.search(r"window\.FWP_JSON\s*=\s*(\{.*?\});", text, re.S)
            if not match:
                continue
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict):
                    return data
            except (TypeError, ValueError):
                continue
        return {}

    def _parse_list(self, html: str) -> tuple[list[dict], bool]:
        soup = _make_soup(html)
        if soup is None:
            return self._parse_list_regex(html), False

        items: list[dict] = []
        for article in soup.select("article.post-list-item.publication"):
            post_id_raw = article.get("id") or ""
            post_match = re.search(r"post-(\d+)", post_id_raw)
            post_id = post_match.group(1) if post_match else None

            link = article.select_one("h2.post-list-item-title a[href]")
            if not link:
                continue
            url = urljoin(self.base_url, link.get("href", "").strip())
            title = _clean_text(link.get_text(" ", strip=True))
            if not url or not title:
                continue

            time_el = article.select_one("time[datetime]")
            published_raw = time_el.get("datetime", "").strip() if time_el else ""
            published_text = time_el.get_text(" ", strip=True) if time_el else ""

            excerpts = article.select("p.post-list-item-excerpt")
            list_abstract = ""
            list_authors: list[str] = []
            for excerpt in excerpts:
                spans = excerpt.find_all("span")
                if spans:
                    list_authors.extend(_clean_text(s.get_text(" ", strip=True)) for s in spans)
                elif not list_abstract:
                    list_abstract = _clean_text(excerpt.get_text(" ", strip=True))

            items.append({
                "post_id": post_id,
                "post_number": post_id,
                "url": url,
                "title": title,
                "published_raw": published_raw or published_text,
                "published_date": _year_date(published_raw or published_text),
                "list_abstract": list_abstract,
                "list_authors": [a for a in list_authors if a],
            })

        has_next = soup.select_one(".facetwp-page.next[data-page]") is not None
        if not has_next:
            settings = self._parse_fwp_settings(soup).get("preload_data", {}).get("settings", {})
            pager = settings.get("pager", {}) if isinstance(settings, dict) else {}
            try:
                has_next = int(pager.get("page", 0)) < int(pager.get("total_pages", 0))
            except (TypeError, ValueError):
                has_next = False
        return items, has_next

    def _parse_list_regex(self, html: str) -> list[dict]:
        items: list[dict] = []
        pattern = re.compile(
            r'<article[^>]+id="post-(\d+)"[^>]*>.*?'
            r'<time[^>]+datetime="([^"]+)"[^>]*>.*?</time>.*?'
            r'<h2 class="post-list-item-title">\s*<a href="([^"]+)">(.*?)</a>',
            re.S,
        )
        for match in pattern.finditer(html):
            post_id, raw_date, url, title_html = match.groups()
            items.append({
                "post_id": post_id,
                "post_number": post_id,
                "url": urljoin(self.base_url, url),
                "title": _strip_tags(title_html),
                "published_raw": raw_date,
                "published_date": _year_date(raw_date),
                "list_abstract": "",
                "list_authors": [],
            })
        return items

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _jsonld_date_published(self, soup) -> str | None:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    value = node.get("datePublished")
                    if value:
                        return str(value)
                    graph = node.get("@graph")
                    if isinstance(graph, list):
                        stack.extend(graph)
                    for child in node.values():
                        if isinstance(child, (dict, list)):
                            stack.append(child)
                elif isinstance(node, list):
                    stack.extend(node)
        return None

    def _parse_reference(self, reference_raw: str | None) -> dict:
        reference = _clean_text(reference_raw)
        if not reference:
            return {"journal": None, "journal_raw": None, "series": None, "volume": None, "issue": None}

        cleaned = reference.rstrip(".")
        journal = cleaned
        series = None
        volume = None
        issue = None

        if "," in cleaned:
            journal_part, rest = cleaned.split(",", 1)
            journal = journal_part.strip()
            rest = rest.strip()
            match = re.search(r"([A-Za-z]*\s*)?(\d+[A-Za-z0-9.\-]*)\s*(?:\(([^)]+)\))?", rest)
            if match:
                series = (match.group(1) or "").strip() or None
                volume = match.group(2)
                issue = match.group(3)
            elif rest:
                series = rest

        return {
            "journal": journal or None,
            "journal_raw": reference,
            "series": series,
            "volume": volume,
            "issue": issue,
        }

    def _parse_detail_html(self, html: str, url: str) -> dict:
        soup = _make_soup(html)
        if soup is None:
            return self._parse_detail_regex(html, url)

        title_el = soup.select_one("h1.header-title")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else None

        category_el = soup.select_one(".publication-type")
        category = _clean_text(category_el.get_text(" ", strip=True)) if category_el else "Academic Article"

        published_raw = None
        published_block = soup.select_one(".meta-block.published")
        if published_block:
            published_raw = _clean_text(published_block.get_text(" ", strip=True))

        people = soup.select_one(".publication-container .references.people")
        authors = []
        if people:
            authors = [_clean_text(s.get_text(" ", strip=True)) for s in people.find_all("span")]
            if not authors:
                authors = [_clean_text(people.get_text(" ", strip=True))]
        authors = [a for a in authors if a]

        reference_el = soup.select_one(".publication-container .references-secondary .reference")
        reference_raw = _clean_text(reference_el.get_text(" ", strip=True)) if reference_el else None
        ref_data = self._parse_reference(reference_raw)

        publication_url = None
        for link in soup.select(".publication-container .references-secondary a[href]"):
            href = link.get("href", "").strip()
            if href:
                publication_url = urljoin(url, href)
                break

        doi = _doi_from_url(publication_url) or _doi_from_url(reference_raw)
        pdf_url = publication_url if _is_probable_pdf_url(publication_url) else None
        original_filename = _filename_from_url(pdf_url)
        if pdf_url and not original_filename:
            original_filename = self._head_filename(pdf_url)

        container = soup.select_one(".publication-container")
        abstract = ""
        if container:
            container_copy = copy(container)
            for ref in container_copy.select(".references"):
                ref.extract()
            abstract = _clean_text(container_copy.get_text(" ", strip=True))

        listed_raw = self._jsonld_date_published(soup)
        listed_date = _iso_date(listed_raw)

        wp_json_url = None
        post_id = None
        rest_link = soup.find("link", attrs={"rel": "alternate", "type": "application/json"})
        if rest_link and rest_link.get("href"):
            wp_json_url = rest_link.get("href")
            match = re.search(r"/publication/(\d+)", wp_json_url)
            if match:
                post_id = match.group(1)
        if not post_id:
            body = soup.find("body")
            classes = body.get("class", []) if body else []
            for cls in classes:
                match = re.search(r"postid-(\d+)", str(cls))
                if match:
                    post_id = match.group(1)
                    break

        return {
            "post_id": post_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "category": category,
            "published_raw": published_raw,
            "published_date": _year_date(published_raw),
            "listed_date": listed_date,
            "listed_date_raw": listed_raw,
            "publication_url": publication_url,
            "doi": doi,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "wp_json_url": wp_json_url,
            **ref_data,
        }

    def _parse_detail_regex(self, html: str, url: str) -> dict:
        title = None
        match = re.search(r'<h1 class="header-title">(.*?)</h1>', html, re.S)
        if match:
            title = _strip_tags(match.group(1))

        abstract = ""
        match = re.search(
            r'<div class="content-entry intro-text-container publication-container">\s*(.*?)'
            r'<div class="references people">',
            html,
            re.S,
        )
        if match:
            abstract = _strip_tags(match.group(1))

        authors = []
        match = re.search(r'<div class="references people">(.*?)</div>', html, re.S)
        if match:
            authors = [_strip_tags(a) for a in re.findall(r"<span[^>]*>(.*?)</span>", match.group(1), re.S)]
            authors = [a for a in authors if a]

        reference_raw = None
        match = re.search(r'<span class="reference"[^>]*>(.*?)</span>', html, re.S)
        if match:
            reference_raw = _strip_tags(match.group(1))

        publication_url = None
        match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>\s*Read publication\s*</a>', html, re.I)
        if match:
            publication_url = urljoin(url, match.group(1))

        listed_raw = None
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
        if match:
            listed_raw = match.group(1)

        post_id = None
        match = re.search(r"postid-(\d+)", html)
        if match:
            post_id = match.group(1)

        ref_data = self._parse_reference(reference_raw)
        pdf_url = publication_url if _is_probable_pdf_url(publication_url) else None
        return {
            "post_id": post_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "category": "Academic Article",
            "published_raw": None,
            "published_date": None,
            "listed_date": _iso_date(listed_raw),
            "listed_date_raw": listed_raw,
            "publication_url": publication_url,
            "doi": _doi_from_url(publication_url) or _doi_from_url(reference_raw),
            "pdf_url": pdf_url,
            "original_filename": _filename_from_url(pdf_url),
            "wp_json_url": None,
            **ref_data,
        }

    def _fetch_wp_native(self, post_id: str | None) -> dict:
        if not post_id:
            return {}
        url = f"{self.base_url}/en/wp-json/wp/v2/publication/{post_id}"
        data = self._get_json(url, referer=_START_URL)
        if not isinstance(data, dict):
            return {}
        return {
            "wp_id": data.get("id"),
            "wp_date": data.get("date"),
            "wp_date_gmt": data.get("date_gmt"),
            "wp_modified": data.get("modified"),
            "wp_modified_gmt": data.get("modified_gmt"),
            "wp_slug": data.get("slug"),
            "wp_status": data.get("status"),
            "wp_type": data.get("type"),
            "wp_link": data.get("link"),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        while page <= _MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time >= _WALL_BUDGET_SECONDS:
                print(f"[stami-no-en] 25-minute budget reached at page {page}; stopping cleanly.")
                break
            if page % 10 == 0:
                print(f"[stami-no-en] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            html = self._get_text(list_url, referer=_START_URL if page > 1 else None)
            if not html:
                print(f"[stami-no-en] list page {page} failed or was empty; stopping.")
                break

            items, has_next = self._parse_list(html)
            if not items:
                print(f"[stami-no-en] page {page} returned 0 records; stopping.")
                break

            new_items = []
            for item in items:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[stami-no-en] page {page} had 0 new records; stopping.")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _WALL_BUDGET_SECONDS:
                    print(f"[stami-no-en] 25-minute budget reached while processing page {page}; stopping cleanly.")
                    break

                item_label = item.get("post_id") or item.get("url") or f"page {page} item {idx}"
                try:
                    time.sleep(self._delay)
                    detail_html = self._get_text(item["url"], referer=list_url)
                    if not detail_html:
                        print(f"[stami-no-en] item {item_label} failed: empty detail response")
                        continue

                    detail = self._parse_detail_html(detail_html, item["url"])
                    post_id = detail.get("post_id") or item.get("post_id")
                    native = self._fetch_wp_native(post_id)

                    title = detail.get("title") or item.get("title") or "(untitled)"
                    abstract = detail.get("abstract") or item.get("list_abstract") or ""
                    abstract = _clean_text(abstract)
                    if len(abstract) < 50:
                        print(f"[stami-no-en] item {item_label} skipped: abstract too short ({len(abstract)} chars)")
                        continue

                    authors = detail.get("authors") or item.get("list_authors") or []
                    if isinstance(authors, str):
                        authors_text = authors
                    else:
                        authors_text = "; ".join(a for a in authors if a)

                    published_date = detail.get("published_date") or item.get("published_date")
                    listed_date = detail.get("listed_date") or _iso_date(native.get("wp_date")) or published_date
                    listed_raw = detail.get("listed_date_raw") or native.get("wp_date") or item.get("published_raw")

                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
                    doi = detail.get("doi")

                    metadata = {
                        "posted_date": listed_raw,
                        "originalFilename": original_filename,
                        "journal_raw": detail.get("journal_raw"),
                        "series": detail.get("series"),
                        "volume": detail.get("volume"),
                        "issue": detail.get("issue"),
                        "post_id": post_id,
                        "post_number": post_id or item.get("post_number"),
                        "slug": native.get("wp_slug") or _slug_from_url(item.get("url")),
                        "wp_rest_url": detail.get("wp_json_url") or (
                            f"{self.base_url}/en/wp-json/wp/v2/publication/{post_id}" if post_id else None
                        ),
                        "wp_date": native.get("wp_date"),
                        "wp_date_gmt": native.get("wp_date_gmt"),
                        "wp_modified": native.get("wp_modified"),
                        "wp_modified_gmt": native.get("wp_modified_gmt"),
                        "wp_status": native.get("wp_status"),
                        "wp_type": native.get("wp_type"),
                        "published_raw": detail.get("published_raw") or item.get("published_raw"),
                        "published_date_precision": "year" if (detail.get("published_raw") or item.get("published_raw")) else None,
                        "list_endpoint": list_url,
                        "detail_endpoint": item.get("url"),
                        "detail_api_endpoint": detail.get("wp_json_url"),
                        "publication_url": detail.get("publication_url"),
                        "list_abstract": item.get("list_abstract"),
                        "list_authors": item.get("list_authors"),
                    }
                    metadata = {k: v for k, v in metadata.items() if v not in (None, "", [])}

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": str(post_id or item.get("post_id") or item.get("url")),
                        "post_number": str(post_id or item.get("post_number")) if (post_id or item.get("post_number")) else None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": authors_text or None,
                        "publisher": "STAMI - National Institute of Occupational Health",
                        "department": None,
                        "journal": detail.get("journal"),
                        "url": item.get("url"),
                        "pdf_url": pdf_url,
                        "keywords": None,
                        "category": detail.get("category") or "Academic Article",
                        "doi": doi,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    suffix = f"/{limit}" if limit is not None else ""
                    print(f"[stami-no-en] saved {saved}{suffix}: {title[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[stami-no-en] item {item_label} failed: {exc}")
                    continue

            if time.time() - start_time >= _WALL_BUDGET_SECONDS:
                break
            if not has_next:
                print(f"[stami-no-en] no next page after page {page}; stopping.")
                break
            page += 1

        if page > _MAX_PAGES:
            print(f"[stami-no-en] safety cap of {_MAX_PAGES} pages reached; stopping.")

        print(f"[stami-no-en] Done. Total saved: {saved}")
        return saved
