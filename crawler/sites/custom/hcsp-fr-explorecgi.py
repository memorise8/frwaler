# -*- coding: utf-8 -*-
"""Crawler for HCSP Explore.cgi avis/rapports communiques."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class HCSPFrExploreCGICrawler(BaseCrawler):
    site_id = "hcsp-fr-explorecgi"
    site_name = "Custom: hcsp-fr-explorecgi"
    base_url = "https://www.hcsp.fr"

    START_URL = (
        "https://www.hcsp.fr/Explore.cgi/avisrapports?"
        "Annee=&Langue=&Type=c&MC0=0&MC1="
    )
    LIST_ENDPOINT = "/Explore.cgi/avisrapports"
    DETAIL_ENDPOINT = "/Explore.cgi/avisrapportsdomaine?clefr=<id>"
    DOWNLOAD_ENDPOINT = "/Explore.cgi/Telecharger?NomFichier=<file>"

    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__HCSP_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the HCSP rendered list page and each linked detail page."""
        saved = 0
        seen_ids = set()

        raw, list_effective_url = self._curl_get(
            self.START_URL,
            context="list HTML endpoint",
            referer=self.base_url + "/",
        )
        if not raw:
            print(f"[{self.site_id}] list endpoint failed; stopping")
            return saved

        soup = self._make_soup(raw, context="list HTML endpoint")
        if soup is None:
            print(f"[{self.site_id}] list HTML could not be parsed; stopping")
            return saved

        records = self._parse_list(soup, list_effective_url or self.START_URL)
        if not records:
            print(f"[{self.site_id}] no records found on list endpoint; stopping")
            return saved

        print(
            f"[{self.site_id}] discovered {len(records)} records from "
            f"{self.LIST_ENDPOINT}"
        )

        for idx, record in enumerate(records, start=1):
            if limit is not None and saved >= limit:
                break

            item_label = str(idx)
            try:
                external_id = record.get("external_id") or ""
                detail_url = record.get("url") or ""
                if not external_id:
                    raise RuntimeError("record has no clefr id")
                if external_id in seen_ids:
                    continue
                seen_ids.add(external_id)
                if not detail_url:
                    raise RuntimeError("record has no detail URL")

                time.sleep(self.detail_delay)
                detail_raw, effective_url = self._curl_get(
                    detail_url,
                    context=f"item {item_label} detail",
                    referer=list_effective_url or self.START_URL,
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
                    effective_url or detail_url,
                )
                abstract = parsed.get("abstract") or ""
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
                    "doi": parsed["doi"],
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
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            accept
            or (
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
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
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                body, http_code, effective_url = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
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
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
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

    def _parse_list(self, soup, list_url):
        table = soup.select_one("table#resultats")
        if table is None:
            return []

        records = []
        seen_urls = set()
        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            link = cells[2].find("a", href=True)
            if link is None:
                continue

            detail_url = urljoin(list_url, link.get("href", "").strip())
            if not detail_url or detail_url in seen_urls:
                continue

            external_id = self._external_id_from_url(detail_url)
            title = self._node_text(link)
            if not external_id or not title:
                continue

            seen_urls.add(detail_url)
            records.append(
                {
                    "external_id": external_id,
                    "title": title,
                    "url": detail_url,
                    "published_date": self._parse_date(self._node_text(cells[0])),
                    "document_type": self._node_text(cells[1]),
                    "list_endpoint": list_url,
                }
            )
        return records

    def _parse_detail(self, soup, raw_html, record, effective_url):
        content = soup.select_one(".row.principal .col-sm-12") or soup
        doc_data = content.select_one("#doc-donnees")

        title = (
            self._node_text(content.find("h2"))
            or self._meta_content(soup, "og: titre", "og:title")
            or record.get("title")
            or ""
        )
        title = self._clean_text(title)
        if not title:
            raise RuntimeError("detail page has no title")

        canonical_url = self._meta_content(soup, "og: url") or effective_url
        detail_url = urljoin(self.base_url, canonical_url)
        external_id = (
            self._external_id_from_url(detail_url)
            or record.get("external_id")
            or ""
        )
        if not external_id:
            raise RuntimeError("detail page has no clefr id")

        doc_text = self._node_multiline_text(doc_data)
        published_date = (
            self._extract_labeled_date(doc_text, "Date du document")
            or record.get("published_date")
            or ""
        )
        posted_date = self._extract_labeled_date(doc_text, "Date de mise en ligne")

        pdf_links = self._extract_pdf_links(doc_data or content)
        pdf_url = pdf_links[0]["url"] if pdf_links else ""
        original_filename = ""
        if pdf_url:
            original_filename = (
                parse_qs(urlparse(pdf_url).query).get("NomFichier", [""])[0]
            )

        keywords = self._dedupe(
            self._node_text(node)
            for node in content.select("#doc-donnees .motclef a, .motclef a")
        )

        group_links = []
        if doc_data is not None:
            for link in doc_data.select("a[href]"):
                label = self._node_text(link)
                href = urljoin(detail_url, link.get("href", "").strip())
                if label and "groupe" in href.lower():
                    group_links.append({"label": label, "url": href})

        abstract = self._extract_abstract(content)
        if not abstract:
            abstract = self._meta_content(
                soup,
                "description",
                "og:description",
                "twitter:description",
            )
        abstract = self._clean_multiline(abstract)

        related_links = []
        for link in content.select("a[href]"):
            label = self._node_text(link)
            href = urljoin(detail_url, link.get("href", "").strip())
            if not label or not href:
                continue
            if "Telecharger" in href:
                continue
            if href == detail_url:
                continue
            if "/Explore.cgi/" in href or urlparse(href).netloc != urlparse(self.base_url).netloc:
                related_links.append({"label": label, "url": href})
        related_links = self._dedupe_dicts(related_links, "url")

        metadata = {
            "source": "HCSP Explore.cgi server-rendered HTML",
            "list_endpoint": record.get("list_endpoint") or self.START_URL,
            "detail_endpoint": self.DETAIL_ENDPOINT,
            "download_endpoint": self.DOWNLOAD_ENDPOINT,
            "list_url": record.get("list_endpoint") or self.START_URL,
            "canonical_url": detail_url,
            "clefr": external_id,
            "document_type": record.get("document_type", ""),
            "posted_date": posted_date,
            "original_filename": original_filename,
            "pdf_links": pdf_links,
            "keywords": keywords,
            "group_links": group_links,
            "related_links": related_links[:20],
        }

        return {
            "external_id": external_id,
            "title": title,
            "authors": [],
            "abstract": abstract,
            "category": record.get("document_type", "") or "Avis et rapports",
            "keywords": keywords,
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": self._extract_doi(raw_html),
            "department": "Haut Conseil de la sante publique",
            "metadata": metadata,
        }

    def _extract_pdf_links(self, container):
        if container is None:
            return []
        links = []
        for link in container.select("a[href]"):
            href = link.get("href", "").strip()
            url = urljoin(self.base_url, href)
            if "Telecharger" not in url and not re.search(r"\.pdf(?:[?#]|$)", url, re.I):
                continue
            label = self._node_text(link)
            filename = parse_qs(urlparse(url).query).get("NomFichier", [""])[0]
            links.append({"label": label, "url": url, "filename": filename})
        return self._dedupe_dicts(links, "url")

    def _extract_abstract(self, content):
        parts = []
        for node in content.select(".avistexte"):
            text = self._node_multiline_text(node)
            if text:
                parts.append(text)
        if not parts:
            return ""
        return self._clean_multiline("\n\n".join(self._dedupe(parts)))

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\ufeff", " ")
        text = text.replace("\xa0", " ")
        text = text.replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return text.strip()

    @classmethod
    def _clean_multiline(cls, value):
        text = cls._clean_text(value)
        text = re.sub(r" *\n+ *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _node_text(cls, node):
        if node is None:
            return ""
        return cls._clean_text(node.get_text(" ", strip=True))

    @classmethod
    def _node_multiline_text(cls, node):
        if node is None:
            return ""
        text = node.get_text("\n", strip=True)
        return cls._clean_multiline(text)

    @staticmethod
    def _dedupe(values):
        result = []
        seen = set()
        for value in values:
            clean = re.sub(r"\s+", " ", str(value or "")).strip()
            key = clean.lower()
            if clean and key not in seen:
                result.append(clean)
                seen.add(key)
        return result

    @staticmethod
    def _dedupe_dicts(values, key):
        result = []
        seen = set()
        for item in values:
            value = item.get(key)
            if not value or value in seen:
                continue
            result.append(item)
            seen.add(value)
        return result

    @classmethod
    def _parse_date(cls, value):
        text = cls._clean_text(value)
        if not text:
            return ""
        match = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b", text)
        if match:
            day = int(match.group(1))
            month = int(match.group(2))
            year = int(match.group(3))
            return f"{year:04d}-{month:02d}-{day:02d}"
        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text)
        if match:
            return match.group(0)
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    @classmethod
    def _extract_labeled_date(cls, text, label):
        if not text:
            return ""
        pattern = rf"{re.escape(label)}\s*:\s*([0-9]{{1,2}}/[0-9]{{1,2}}/[0-9]{{4}})"
        match = re.search(pattern, text, flags=re.I)
        if not match:
            return ""
        return cls._parse_date(match.group(1))

    @staticmethod
    def _external_id_from_url(url):
        parsed = urlparse(url or "")
        query = parse_qs(parsed.query)
        for key, values in query.items():
            if key.lower() == "clefr" and values:
                return str(values[0]).strip()
        return ""

    @classmethod
    def _meta_content(cls, soup, *names):
        wanted = {cls._clean_text(name).lower() for name in names}
        for node in soup.find_all("meta"):
            key = node.get("property") or node.get("name") or ""
            if cls._clean_text(key).lower() in wanted and node.get("content"):
                return cls._clean_text(node.get("content"))
        return ""

    @staticmethod
    def _extract_doi(raw_html):
        if not raw_html:
            return ""
        match = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", raw_html)
        if not match:
            match = re.search(r"\bdoi\s*:\s*(10\.\S+)", raw_html, flags=re.I)
        if not match:
            return ""
        return match.group(1).rstrip(".,);")
