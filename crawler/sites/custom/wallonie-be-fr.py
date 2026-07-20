# -*- coding: utf-8 -*-
"""Crawler for wallonie.be French publications."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class WallonieBeFrCrawler(BaseCrawler):
    site_id = "wallonie-be-fr"
    site_name = "Custom: wallonie-be-fr"
    base_url = "https://www.wallonie.be"

    START_URL = "https://www.wallonie.be/fr/publications"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MAX_PAGES = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__WALLONIE_BE_FR_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl Wallonie's Drupal publication list and detail pages."""
        saved = 0
        page = 0
        seen_urls = set()
        started_at = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page < self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at >= self.WALL_CLOCK_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25 minute crawl budget; stopping cleanly")
                break

            list_url = self._list_url(page)
            raw, effective_url = self._curl_get_text(
                list_url,
                context=f"list page {page}",
                referer=self.base_url + "/fr",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_records = [
                record for record in records
                if record.get("url") and record["url"] not in seen_urls
            ]
            if not new_records:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            page_number = page + 1
            if page_number == 1 or page_number % 10 == 0:
                print(f"[{self.site_id}] page {page_number}: saved {saved}/{limit_or_inf}")

            for idx, record in enumerate(new_records, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - started_at >= self.WALL_CLOCK_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; stopping cleanly")
                    return saved

                item_label = f"page {page} item {idx}"
                try:
                    detail_url = record.get("url")
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self.detail_delay)
                    detail_raw, detail_effective_url = self._curl_get_text(
                        detail_url,
                        context=f"item {item_label} detail",
                        referer=effective_url or list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_label} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(
                        detail_soup,
                        detail_raw,
                        record,
                        detail_effective_url or detail_url,
                    )
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(self._to_paper(parsed))
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup, page):
                break
            page += 1

        if page >= self.MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")
        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        body, effective_url = self._curl_get_bytes(
            url,
            context=context,
            referer=referer,
            accept=accept
            or (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
        )
        if body is None:
            return None, effective_url
        return body.decode("utf-8", errors="replace"), effective_url

    def _curl_get_bytes(self, url, context="request", referer=None, accept=None):
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
            f"Accept: {accept or '*/*'}",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
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
                body, http_code, effective_url = self._split_curl_output(
                    result.stdout or b"",
                    url,
                )
                stderr = (result.stderr or b"").decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and http_code.isdigit() and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body:
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker = ("\n" + self._CURL_META_MARKER).encode("ascii")
        marker_pos = raw.rfind(marker)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + len(marker):].decode(
            "utf-8",
            errors="replace",
        ).strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
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
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _parse_list(self, soup):
        records = []
        seen = set()
        for card in soup.select("spw-mosaic spw-card[href]"):
            href = (card.get("href") or "").strip()
            url = urljoin(self.base_url, href)
            if not self._is_publication_detail_url(url) or url in seen:
                continue

            content = card.select_one("spw-card-content")
            title_tag = card.select_one("spw-card-title")
            excerpt_tag = card.select_one("spw-card-excerpt")
            image_tag = card.select_one("img")

            listed_date = ""
            if content is not None:
                listed_date = self._parse_date(content.get("date") or "")

            title = self._clean_text(title_tag.get_text(" ", strip=True) if title_tag else "")
            excerpt = self._clean_text(excerpt_tag.get_text(" ", strip=True) if excerpt_tag else "")
            image_url = None
            if image_tag is not None and image_tag.get("src"):
                image_url = urljoin(self.base_url, image_tag.get("src"))

            records.append({
                "url": url,
                "slug": self._slug_from_url(url),
                "title": title,
                "listed_date": listed_date,
                "posted_date_raw": content.get("date") if content is not None else "",
                "list_excerpt": excerpt,
                "image_url": image_url,
            })
            seen.add(url)
        return records

    def _parse_detail(self, soup, raw_html, record, effective_url):
        article = soup.select_one("article.node--type-publication") or soup.select_one("article")
        if article is None:
            raise RuntimeError("publication article not found")

        title = self._extract_title(soup) or record.get("title") or "(untitled)"
        node_id = (article.get("data-history-node-id") or "").strip()
        settings_node_id = self._node_id_from_drupal_settings(soup)
        if not node_id:
            node_id = settings_node_id or ""

        time_tag = article.select_one("time[datetime]") or article.select_one("time")
        published_date = ""
        raw_published_date = ""
        if time_tag is not None:
            raw_published_date = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
            published_date = self._parse_date(raw_published_date)
        if not published_date:
            published_date = record.get("listed_date") or ""

        content = article.select_one(".node__content") or article
        for tag in content.select("script, style, img, time"):
            tag.decompose()

        link_data = self._extract_links(article)
        pdf_url = link_data.get("pdf_url")
        original_filename = self._original_filename(pdf_url)

        abstract = self._clean_text(content.get_text(" ", strip=True))
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = self._clean_text(
                (record.get("list_excerpt") or "") + " " + self._meta_description(soup)
            )

        external_id = node_id or record.get("slug") or self._slug_from_url(effective_url)
        post_number = node_id or record.get("slug")
        listed_date = record.get("listed_date") or published_date
        posted_date_raw = record.get("posted_date_raw") or listed_date

        metadata = {
            "posted_date": posted_date_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id or None,
            "drupal_current_path": f"node/{node_id}" if node_id else None,
            "slug": record.get("slug"),
            "list_url": record.get("url"),
            "list_title": record.get("title"),
            "list_excerpt": record.get("list_excerpt"),
            "list_image_url": record.get("image_url"),
            "raw_published_date": raw_published_date,
            "detail_effective_url": effective_url,
            "links": link_data.get("links", []),
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "",
            "publisher": "Service public de Wallonie",
            "department": "",
            "journal": "",
            "url": record.get("url") or effective_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": "Publications",
            "doi": "",
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _extract_links(self, article):
        links = []
        pdf_url = None
        for link in article.select("a[href]"):
            href = (link.get("href") or "").strip()
            if not href or href.startswith("#") or href.lower().startswith("mailto:"):
                continue
            url = urljoin(self.base_url, href)
            text = self._clean_text(link.get_text(" ", strip=True))
            is_pdf = self._looks_like_pdf_url(url)
            links.append({
                "url": url,
                "text": text,
                "is_pdf": is_pdf,
            })
            if is_pdf and pdf_url is None:
                pdf_url = url
        return {"pdf_url": pdf_url, "links": links}

    def _to_paper(self, parsed):
        metadata = dict(parsed["metadata"])
        metadata.update({
            "post_number": parsed.get("post_number"),
            "publisher": parsed.get("publisher"),
            "listed_date": parsed.get("listed_date"),
            "journal_raw": metadata.get("journal_raw"),
            "series": metadata.get("series"),
            "volume": metadata.get("volume"),
            "issue": metadata.get("issue"),
        })
        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": parsed["external_id"],
            "post_number": parsed.get("post_number"),
            "title": parsed["title"],
            "abstract": parsed["abstract"],
            "published_date": parsed["published_date"],
            "listed_date": parsed["listed_date"],
            "posted_date": parsed["posted_date"],
            "authors": parsed["authors"],
            "publisher": parsed["publisher"],
            "department": parsed["department"],
            "journal": parsed["journal"],
            "url": parsed["url"],
            "pdf_url": parsed["pdf_url"],
            "keywords": parsed["keywords"],
            "category": parsed["category"],
            "doi": parsed["doi"],
            "original_filename": parsed["original_filename"],
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 0:
            return self.START_URL
        return f"{self.START_URL}?page={page}"

    def _has_next_page(self, soup, page):
        if soup.select_one(".pager__item--next a[href]"):
            return True

        pager = soup.select_one("spw-pagination")
        if pager is not None:
            try:
                total = int(pager.get("total-items") or 0)
                per_page = int(pager.get("items-per-page") or 0)
                current = int(pager.get("current-page") or page + 1)
                if total > 0 and per_page > 0:
                    return current * per_page < total
            except (TypeError, ValueError):
                return False
        return False

    def _extract_title(self, soup):
        h1 = soup.select_one("h1")
        if h1 is not None:
            text = self._clean_text(h1.get_text(" ", strip=True))
            if text:
                return text
        meta = soup.select_one('meta[property="og:title"], meta[name="title"]')
        if meta is not None:
            return self._clean_text(meta.get("content") or "")
        return ""

    def _node_id_from_drupal_settings(self, soup):
        settings = soup.select_one('script[type="application/json"][data-drupal-selector="drupal-settings-json"]')
        if settings is None:
            return ""
        try:
            data = json.loads(settings.string or settings.get_text() or "{}")
        except (TypeError, ValueError):
            return ""
        current_path = ((data.get("path") or {}).get("currentPath") or "").strip()
        match = re.search(r"node/(\d+)", current_path)
        return match.group(1) if match else ""

    def _meta_description(self, soup):
        tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        if tag is None:
            return ""
        return self._clean_text(tag.get("content") or "")

    def _original_filename(self, pdf_url):
        if not pdf_url:
            return None

        nested = parse_qs(urlparse(pdf_url).query).get("url")
        if nested:
            nested_url = unquote(nested[0])
            filename = self._filename_from_url(nested_url)
            if filename:
                return filename

        return self._filename_from_url(pdf_url)

    def _filename_from_url(self, url):
        path = unquote(urlparse(url).path or "")
        tail = path.rstrip("/").split("/")[-1]
        if "." in tail and len(tail) <= 220:
            return tail
        return None

    def _looks_like_pdf_url(self, url):
        lowered = unquote(url).lower()
        if ".pdf" in lowered:
            return True
        nested = parse_qs(urlparse(url).query).get("url")
        if nested and ".pdf" in unquote(nested[0]).lower():
            return True
        return False

    def _is_publication_detail_url(self, url):
        parsed = urlparse(url)
        return (
            parsed.netloc.endswith("wallonie.be")
            and parsed.path.startswith("/fr/publications/")
            and parsed.path.rstrip("/") != "/fr/publications"
        )

    def _slug_from_url(self, url):
        return unquote(urlparse(url).path.rstrip("/").split("/")[-1])

    def _parse_date(self, raw):
        if not raw:
            return ""
        text = self._clean_text(str(raw))
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if match:
            return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
        return text[:10] if len(text) >= 10 else text

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value)).replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()
