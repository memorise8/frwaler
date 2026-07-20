# -*- coding: utf-8 -*-
"""Crawler for the TNO Repository conference-paper DocList."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class RepositoryTnoNlDoclistCrawler(BaseCrawler):
    site_id = "repository-tno-nl-doclist"
    site_name = "Custom: repository-tno-nl-doclist"
    base_url = "https://repository.tno.nl"

    _START_PATH = "/DocList"
    _FIND = "ID > 0,refine_MATREP:CONFERENCE PAPER"
    _SORT = "JVP:DESC:MATREP:DESC"
    _PAGE_SIZE = 10
    _MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network and parser helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[repository-tno-nl-doclist] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s*\n\s*", "\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @classmethod
    def _tag_text(cls, tag, separator=" "):
        if not tag:
            return ""
        return cls._one_line(tag.get_text(separator, strip=True))

    @staticmethod
    def _dedupe(items):
        seen = set()
        result = []
        for item in items:
            value = str(item or "").strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @staticmethod
    def _parse_date(raw):
        text = str(raw or "").strip()
        if not text:
            return ""
        match = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", text)
        if match:
            return match.group(0)
        match = re.search(r"\b(19|20)\d{2}\b", text)
        return match.group(0) if match else ""

    @staticmethod
    def _doc_id_from_url(url):
        parsed = urlparse(url)
        doc_id = (parse_qs(parsed.query).get("docId") or [""])[0]
        if doc_id:
            return doc_id
        match = re.search(r"docId=(\d+)", url)
        return match.group(1) if match else ""

    def _list_url(self, page):
        params = {
            "find": self._FIND,
            "sort": self._SORT,
        }
        if page and page > 1:
            params["page"] = str(page)
        return urljoin(self.base_url, self._START_PATH) + "?" + urlencode(params)

    def _detail_url(self, doc_id):
        return urljoin(self.base_url, f"/SingleDoc?docId={doc_id}")

    # ------------------------------------------------------------------
    # List/detail parsing
    # ------------------------------------------------------------------

    def _parse_list(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            return []

        docs = []
        containers = soup.select(".doclist div.doc")
        if not containers:
            containers = [
                node for node in soup.select("div.doc")
                if node.select_one('a[href*="SingleDoc?docId="]')
            ]

        for container in containers:
            link = container.select_one('a[href*="SingleDoc?docId="]')
            if not link:
                continue
            href = link.get("href") or ""
            url = urljoin(self.base_url, href)
            doc_id = self._doc_id_from_url(url)
            if not doc_id:
                continue
            item = {
                "external_id": doc_id,
                "url": url,
                "title": self._tag_text(container.select_one(".xref-TTZ")),
                "authors_text": self._tag_text(container.select_one(".xref-AUTREPET")),
                "published_date": self._parse_date(self._tag_text(container.select_one(".xref-JVP"))),
                "category": self._tag_text(container.select_one(".xref-MATREP")),
                "abstract": self._tag_text(container.select_one(".xref-ABS"), separator="\n"),
            }
            docs.append(item)

        deduped = []
        seen = set()
        for item in docs:
            if item["external_id"] not in seen:
                seen.add(item["external_id"])
                deduped.append(item)
        return deduped

    def _field(self, soup, code):
        node = soup.select_one(f".singledoc .field.xref-{code}")
        if node is None:
            node = soup.select_one(f".field.xref-{code}")
        return node

    def _field_value(self, soup, code, separator=" "):
        node = self._field(soup, code)
        if node is None:
            return ""
        value = node.select_one(".value")
        return self._tag_text(value or node, separator=separator)

    def _field_links(self, soup, code):
        node = self._field(soup, code)
        if node is None:
            return []
        links = []
        for link in node.select("a[href]"):
            href = link.get("href") or ""
            if href:
                links.append(urljoin(self.base_url, href))
        return self._dedupe(links)

    def _meta_contents(self, soup, *, name=None, itemprop=None):
        selector = "meta"
        if name:
            selector += f'[name="{name}"]'
        if itemprop:
            selector += f'[itemprop="{itemprop}"]'
        return self._dedupe(meta.get("content", "").strip() for meta in soup.select(selector))

    def _parse_authors(self, soup, fallback):
        authors = self._meta_contents(soup, name="citation_author")
        if authors:
            return authors

        node = self._field(soup, "AUTREP")
        if node:
            authors = self._dedupe(meta.get("content", "").strip() for meta in node.select('meta[itemprop="name"]'))
            if authors:
                return authors
            authors = self._dedupe(self._tag_text(link) for link in node.select('a[href*="find=AUT"]'))
            if authors:
                return authors

        return self._dedupe(re.split(r"\s*;\s*", fallback or ""))

    def _parse_keywords(self, soup):
        node = self._field(soup, "TRCREP")
        keywords = []
        if node:
            keywords.extend(meta.get("content", "").strip() for meta in node.select('meta[itemprop="about"]'))
            if not keywords:
                keywords.extend(self._tag_text(link) for link in node.select('a[href*="find=TRC"]'))
        return self._dedupe(keywords)

    def _parse_pdf_url(self, soup):
        links = self._field_links(soup, "URRREP")
        for href in links:
            lowered = href.lower()
            if lowered.startswith("mailto:"):
                continue
            if ".pdf" in lowered:
                return href
        for href in links:
            if not href.lower().startswith("mailto:"):
                return href
        return ""

    def _parse_doi(self, soup):
        doi_links = self._field_links(soup, "DOIREP")
        for href in doi_links:
            match = re.search(r"(10\.\d{4,9}/\S+)", href, flags=re.I)
            if match:
                return match.group(1).rstrip(".,;)")
        raw = self._field_value(soup, "DOIREP")
        match = re.search(r"(10\.\d{4,9}/\S+)", raw, flags=re.I)
        return match.group(1).rstrip(".,;)") if match else ""

    def _parse_repository_link(self, soup):
        links = self._field_links(soup, "UID")
        return links[0] if links else ""

    def _parse_detail(self, raw, list_item):
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("detail page could not be parsed")

        doc_id = list_item.get("external_id") or ""
        title = (
            self._field_value(soup, "TTZ")
            or self._one_line((soup.select_one('meta[name="citation_title"]') or {}).get("content", ""))
            or list_item.get("title", "")
        )
        abstract = self._field_value(soup, "ABS", separator="\n") or list_item.get("abstract", "")
        abstract = self._clean_text(abstract)
        published_date = (
            self._parse_date(self._field_value(soup, "JVPREP"))
            or self._parse_date((soup.select_one('meta[name="citation_date"]') or {}).get("content", ""))
            or list_item.get("published_date", "")
        )
        category = self._field_value(soup, "MATREP") or list_item.get("category", "")
        authors = self._parse_authors(soup, list_item.get("authors_text", ""))
        keywords = self._parse_keywords(soup)
        pdf_url = self._parse_pdf_url(soup)
        doi = self._parse_doi(soup)
        repository_link = self._parse_repository_link(soup)
        tno_identifier = self._field_value(soup, "TID")

        metadata = {
            "listEndpoint": self._START_PATH,
            "detailEndpoint": "/SingleDoc",
            "sourceListUrl": self._list_url(1),
            "docId": doc_id,
            "tnoIdentifier": tno_identifier,
            "repositoryLink": repository_link,
            "sourceTitle": self._field_value(soup, "BRT"),
            "collation": self._field_value(soup, "PAG"),
            "placeOfPublication": self._field_value(soup, "PLA"),
            "pages": self._field_value(soup, "BLZ"),
            "language": (soup.select_one(".singledoc [lang]") or {}).get("lang", ""),
            "rawListItem": list_item,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": doc_id,
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": list_item.get("url") or self._detail_url(doc_id),
            "pdf_url": pdf_url,
            "doi": doi,
            "department": "Netherlands Organisation for Applied Scientific Research",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        empty_pages = 0

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw = self._curl(list_url)
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            items = self._parse_list(raw)
            if not items:
                empty_pages += 1
                print(f"[{self.site_id}] no records on list page {page}")
                if empty_pages >= 2:
                    break
                page += 1
                continue
            empty_pages = 0

            print(f"[{self.site_id}] page {page}: discovered {len(items)} detail links")

            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                try:
                    if self.detail_delay:
                        time.sleep(self.detail_delay)

                    detail_url = item.get("url") or self._detail_url(item.get("external_id", ""))
                    detail_raw = self._curl(detail_url)
                    if not detail_raw:
                        print(
                            f"[{self.site_id}] item {idx} failed: detail fetch failed "
                            f"for {item.get('external_id', '')}"
                        )
                        continue

                    paper = self._parse_detail(detail_raw, item)
                    abstract = paper.get("abstract") or ""
                    if len(abstract.strip()) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {idx} skipped: abstract too short "
                            f"({len(abstract.strip())} chars) for {paper.get('external_id', '')}"
                        )
                        continue

                    if not paper.get("external_id"):
                        print(f"[{self.site_id}] item {idx} skipped: missing external_id")
                        continue
                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {idx} skipped: missing title")
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")

                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
