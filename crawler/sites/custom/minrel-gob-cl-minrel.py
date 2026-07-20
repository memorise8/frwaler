# -*- coding: utf-8 -*-
"""Crawler for Ministerio de Relaciones Exteriores de Chile press room."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment

from crawler.base_crawler import BaseCrawler


class MinrelGobClMinrelCrawler(BaseCrawler):
    site_id = "minrel-gob-cl-minrel"
    site_name = "Custom: minrel-gob-cl-minrel"
    base_url = "https://www.minrel.gob.cl"

    START_URL = "https://www.minrel.gob.cl/minrel/sala-de-prensa/p/1"
    LIST_URL_TEMPLATE = "https://www.minrel.gob.cl/minrel/sala-de-prensa/p/{page}"
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

    MONTHS = {
        "enero": 1,
        "febrero": 2,
        "marzo": 3,
        "abril": 4,
        "mayo": 5,
        "junio": 6,
        "julio": 7,
        "agosto": 8,
        "septiembre": 9,
        "setiembre": 9,
        "octubre": 10,
        "noviembre": 11,
        "diciembre": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self.LIST_URL_TEMPLATE.format(page=page)
            raw = self._curl_get(
                list_url,
                context=f"list page {page}",
                referer=self.base_url + "/",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            print(
                f"[{self.site_id}] page {page}: discovered {len(records)} records "
                "from HTML list endpoint"
            )

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"page {page} item {idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"item {item_label} detail",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(detail_raw)
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, record)
                    abstract = parsed["abstract"]
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": "",
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup, page):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'}",
            "-H",
            "Accept-Language: es-CL,es;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt}/3): {last_error}"
            )
            if attempt < 3:
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\u200b", "")
        text = text.replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @staticmethod
    def _meta_content(soup, *keys):
        for key in keys:
            node = soup.find("meta", attrs={"name": key})
            if node and node.get("content") is not None:
                return node.get("content", "").strip()
            node = soup.find("meta", attrs={"property": key})
            if node and node.get("content") is not None:
                return node.get("content", "").strip()
        return ""

    def _parse_list(self, soup):
        records = []
        for card in soup.select("article.card"):
            link = card.select_one("a.card__auxi[href]") or card.find("a", href=True)
            if not link:
                continue

            href = (link.get("href") or "").strip()
            if not href or href == "#":
                continue

            title_node = card.select_one(".card__title")
            title = self._one_line(title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True))
            if not title:
                continue

            date_node = card.select_one(".card__date")
            list_date_text = self._one_line(date_node.get_text(" ", strip=True) if date_node else "")
            image_node = card.select_one("img[src]")
            image_url = ""
            if image_node:
                image_url = urljoin(self.base_url, (image_node.get("src") or "").strip())

            url = urljoin(self.base_url, href)
            records.append({
                "title": title,
                "url": url,
                "list_date_text": list_date_text,
                "list_published_date": self._parse_spanish_date(list_date_text),
                "list_image_url": image_url,
            })
        return records

    def _parse_detail(self, soup, record):
        ld_json = self._json_ld_article(soup)

        canonical = ""
        canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
        if canonical_node and canonical_node.get("href"):
            canonical = urljoin(self.base_url, canonical_node.get("href", "").strip())
        url = canonical or record.get("url") or ""

        title = self._one_line(self._select_text(soup, ".enc-main__title"))
        if not title:
            title = self._strip_site_suffix(self._meta_content(soup, "og:title", "twitter:title"))
        if not title and ld_json.get("headline"):
            title = self._one_line(self._strip_html(ld_json.get("headline") or ""))
        if not title:
            title = record.get("title") or ""

        lead = self._one_line(self._select_text(soup, ".enc-main__description"))
        if not lead:
            lead = self._one_line(self._meta_content(soup, "description", "og:description", "twitter:description"))

        body = self._article_body_text(soup)
        abstract = self._join_unique_texts([lead, body])

        published_date = ""
        raw_published = ld_json.get("datePublished") or ld_json.get("dateCreated") or ""
        if raw_published:
            published_date = self._parse_iso_date(raw_published)
        if not published_date:
            detail_date_text = self._one_line(self._select_text(soup, ".enc-main__date"))
            published_date = self._parse_spanish_date(detail_date_text)
        else:
            detail_date_text = self._one_line(self._select_text(soup, ".enc-main__date"))
        if not published_date:
            published_date = record.get("list_published_date") or ""

        keywords = [self._one_line(node.get_text(" ", strip=True)) for node in soup.select("a.tags__tag")]
        keywords = [value for value in keywords if value]
        if "Sala de prensa" not in keywords:
            keywords.insert(0, "Sala de prensa")

        image_urls = self._image_urls(soup, record, ld_json)
        pdf_url = self._first_pdf_url(soup)
        article_stamp = self._article_stamp(soup)
        external_id = self._external_id(url, article_stamp)

        author = ld_json.get("author") or {}
        author_name = ""
        if isinstance(author, dict):
            author_name = author.get("name") or ""
        elif isinstance(author, list):
            names = [a.get("name") for a in author if isinstance(a, dict) and a.get("name")]
            author_name = names[0] if names else ""
        author_name = author_name or "Ministerio de Relaciones Exteriores"

        metadata = {
            "source": "HTML list/detail endpoints",
            "listEndpoint": self.START_URL,
            "detailEndpoint": url,
            "listTitle": record.get("title") or "",
            "listDateText": record.get("list_date_text") or "",
            "detailDateText": detail_date_text,
            "lead": lead,
            "articleStamp": article_stamp,
            "imageUrls": image_urls,
            "jsonLd": ld_json,
            "abstractLength": len(abstract),
        }

        return {
            "external_id": external_id,
            "title": title,
            "authors": [author_name],
            "abstract": abstract,
            "category": "Sala de Prensa",
            "keywords": keywords,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "department": "Ministerio de Relaciones Exteriores de Chile",
            "metadata": metadata,
        }

    def _select_text(self, soup, selector):
        node = soup.select_one(selector)
        if not node:
            return ""
        return node.get_text("\n", strip=True)

    def _article_body_text(self, soup):
        body = soup.select_one("div.CUERPO")
        if not body:
            body = soup.select_one("section.art-content")
        if not body:
            return ""

        parts = []
        for node in body.find_all(["p", "li", "h2", "h3"], recursive=True):
            text = self._one_line(node.get_text(" ", strip=True))
            if text:
                parts.append(text)
        if not parts:
            return self._one_line(body.get_text(" ", strip=True))
        return self._clean_text("\n\n".join(parts))

    def _json_ld_article(self, soup):
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text() or ""
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                graph = item.get("@graph")
                if isinstance(graph, list):
                    for graph_item in graph:
                        if self._is_news_article(graph_item):
                            return graph_item
                if self._is_news_article(item):
                    return item
        return {}

    @staticmethod
    def _is_news_article(value):
        if not isinstance(value, dict):
            return False
        kind = value.get("@type")
        if isinstance(kind, list):
            return any(str(item).lower() in {"newsarticle", "article"} for item in kind)
        return str(kind).lower() in {"newsarticle", "article"}

    def _image_urls(self, soup, record, ld_json):
        urls = []
        image = ld_json.get("image")
        if isinstance(image, str):
            urls.append(image)
        elif isinstance(image, list):
            for item in image:
                if isinstance(item, str):
                    urls.append(item)
                elif isinstance(item, dict) and item.get("url"):
                    urls.append(item.get("url"))
        og_image = self._meta_content(soup, "og:image", "twitter:image")
        if og_image:
            urls.append(og_image)
        main_img = soup.select_one(".enc-main__img[src]")
        if main_img:
            urls.append(urljoin(self.base_url, main_img.get("src", "").strip()))
        if record.get("list_image_url"):
            urls.append(record.get("list_image_url"))
        return self._dedupe([urljoin(self.base_url, url) for url in urls if url])

    def _first_pdf_url(self, soup):
        content = soup.select_one("section.art-content") or soup
        for link in content.find_all("a", href=True):
            href = (link.get("href") or "").strip()
            if ".pdf" in href.lower():
                return urljoin(self.base_url, href)
        return ""

    def _article_stamp(self, soup):
        comments = soup.find_all(string=lambda text: isinstance(text, Comment))
        for text in comments:
            stripped = text.strip()
            match = re.fullmatch(r"(\d{14})", stripped)
            if match:
                return match.group(1)
        return ""

    def _external_id(self, url, article_stamp):
        path = urlparse(url).path.strip("/")
        if article_stamp and path:
            return f"{article_stamp}:{path}"
        return path or article_stamp or url

    def _has_next_page(self, soup, page):
        next_link = soup.find("link", rel=lambda value: value and "next" in value)
        if next_link and next_link.get("href"):
            return True
        next_href = f"/minrel/sala-de-prensa/p/{page + 1}"
        if soup.find("a", href=next_href):
            return True
        return bool(soup.find("a", href=urljoin(self.base_url, next_href)))

    def _parse_spanish_date(self, raw):
        text = self._one_line(raw).lower()
        if not text:
            return ""
        match = re.search(
            r"\b(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+((?:19|20)\d{2})\b",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        day = int(match.group(1))
        month_name = self._strip_accents(match.group(2))
        month = self.MONTHS.get(month_name)
        year = int(match.group(3))
        if not month:
            return ""
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return ""

    @staticmethod
    def _parse_iso_date(raw):
        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", str(raw))
        return match.group(0) if match else ""

    @staticmethod
    def _strip_accents(value):
        replacements = str.maketrans({
            "á": "a",
            "é": "e",
            "í": "i",
            "ó": "o",
            "ú": "u",
            "ñ": "n",
        })
        return value.translate(replacements)

    @classmethod
    def _strip_html(cls, value):
        return cls._one_line(re.sub(r"<[^>]+>", " ", str(value or "")))

    @classmethod
    def _strip_site_suffix(cls, value):
        text = cls._one_line(value)
        return re.sub(r"\s*\|\s*Ministerio de Relaciones Exteriores\s*$", "", text).strip()

    @classmethod
    def _join_unique_texts(cls, values):
        parts = []
        seen = set()
        for value in values:
            text = cls._clean_text(value)
            if not text:
                continue
            key = re.sub(r"\s+", " ", text).lower()
            if key in seen:
                continue
            if any(key and key in existing for existing in seen):
                continue
            parts.append(text)
            seen.add(key)
        return cls._clean_text("\n\n".join(parts))

    @staticmethod
    def _dedupe(values):
        result = []
        seen = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result
