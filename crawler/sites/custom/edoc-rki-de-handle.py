# -*- coding: utf-8 -*-
"""Crawler for RKI edoc DSpace collection handle 176904/2."""

from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EdocRkiDeHandleCrawler(BaseCrawler):
    site_id = "edoc-rki-de-handle"
    site_name = "Custom: edoc-rki-de-handle"
    base_url = "https://edoc.rki.de"

    START_URL = "https://edoc.rki.de/handle/176904/2"
    START_HANDLE = "176904/2"
    PAGE_SIZE = 100
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100

    XML_NS = {
        "mets": "http://www.loc.gov/METS/",
        "dim": "http://www.dspace.org/xmlns/dspace/dim",
        "xlink": "http://www.w3.org/TR/xlink/",
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None, page_size=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay
        self.page_size = page_size or self.PAGE_SIZE

    def crawl(self, limit=None):
        """Crawl the RKI DSpace collection and save records with real abstracts."""
        saved = 0
        page = 1
        seen = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw = self._curl_get(
                list_url,
                context=f"list page {page}",
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            )
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            soup = self._make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            total = self._parse_total(soup)
            total_msg = f" of {total}" if total else ""
            print(f"[{self.site_id}] page {page}: found {len(records)} records{total_msg}")

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"{page}.{idx}"
                external_id = record.get("external_id") or ""
                if not external_id or external_id in seen:
                    continue
                seen.add(external_id)

                try:
                    time.sleep(self.detail_delay)
                    parsed = self._fetch_and_parse_detail(record, item_label, list_url)

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
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if total and page * self.page_size >= total:
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", accept=None, referer=None):
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
            f"Accept: {accept or '*/*'}",
            "-H",
            "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
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
                wait = self.BACKOFF_SECONDS[attempt - 1]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _list_url(self, page):
        params = {
            "rpp": str(self.page_size),
            "etal": "0",
            "group_by": "none",
            "page": str(page),
            "sort_by": "dc.date.issued_dt",
            "order": "desc",
        }
        return f"{self.START_URL}/discover?{urlencode(params)}"

    def _make_soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        return None

    def _parse_list(self, soup, list_url):
        records = []
        seen = set()
        for item in soup.select(".ds-artifact-item"):
            link = item.find("a", href=re.compile(r"^/handle/176904/[^/?#]+$"))
            if not link:
                continue
            href = link.get("href") or ""
            match = re.search(r"/handle/(176904/[^/?#]+)$", href)
            if not match:
                continue

            external_id = match.group(1)
            if external_id == self.START_HANDLE or external_id in seen:
                continue
            seen.add(external_id)

            title_node = item.select_one(".artifact-title")
            title = self._node_text(title_node) if title_node else ""
            if not title:
                title = self._normalize_text(link.get_text(" ", strip=True))
            if not title:
                continue

            subtitle_node = item.select_one(".artifact-subtitle")
            abstract_node = item.select_one(".artifact-abstract")
            date_node = item.select_one(".badge-date .date")
            type_node = item.select_one(".badge-type .type")

            records.append(
                {
                    "external_id": external_id,
                    "title": title,
                    "subtitle": self._node_text(subtitle_node) if subtitle_node else "",
                    "list_abstract": self._node_text(abstract_node) if abstract_node else "",
                    "published_date": self._normalize_date(
                        self._node_text(date_node) if date_node else ""
                    ),
                    "category": self._node_text(type_node) if type_node else "",
                    "url": urljoin(self.base_url, href),
                    "list_url": list_url,
                }
            )
        return records

    def _parse_total(self, soup):
        node = soup.select_one(".pagination-info")
        text = self._node_text(node) if node else ""
        match = re.search(r"\bvon\s+([\d.]+)", text)
        if not match:
            match = re.search(r"\bof\s+([\d,]+)", text)
        if not match:
            return None
        raw_total = match.group(1).replace(".", "").replace(",", "")
        try:
            return int(raw_total)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _fetch_and_parse_detail(self, record, item_label, list_url):
        mets_url = self._mets_url(record["external_id"])
        raw = self._curl_get(
            mets_url,
            context=f"item {item_label} METS detail",
            accept="application/xml,text/xml,*/*;q=0.8",
            referer=record.get("url") or list_url,
        )
        if raw:
            try:
                return self._parse_mets_detail(raw, record, mets_url)
            except Exception as exc:
                print(f"[{self.site_id}] item {item_label}: METS parse failed: {exc}")

        detail_url = record.get("url") or self._detail_url(record["external_id"])
        html_raw = self._curl_get(
            detail_url,
            context=f"item {item_label} HTML detail",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=list_url,
        )
        if not html_raw:
            raise RuntimeError("detail fetch failed after retries")

        soup = self._make_soup(html_raw)
        if soup is None:
            raise RuntimeError("detail HTML could not be parsed")
        return self._parse_html_detail(soup, record, detail_url, mets_url)

    def _mets_url(self, external_id):
        return f"{self.base_url}/metadata/handle/{external_id}/mets.xml?sections=dmdSec,fileSec"

    def _detail_url(self, external_id):
        return f"{self.base_url}/handle/{external_id}"

    def _parse_mets_detail(self, raw, record, mets_url):
        root = ET.fromstring(raw.encode("utf-8"))
        fields = []
        for field in root.findall(".//dim:field", self.XML_NS):
            value = self._normalize_text(field.text or "")
            if not value:
                continue
            fields.append(
                {
                    "schema": field.attrib.get("mdschema", ""),
                    "element": field.attrib.get("element", ""),
                    "qualifier": field.attrib.get("qualifier", ""),
                    "language": field.attrib.get("language", ""),
                    "value": value,
                }
            )

        title = self._first_value(fields, "title") or record.get("title") or ""
        abstracts = self._values(fields, "description", "abstract")
        if not abstracts:
            abstracts = [
                f["value"]
                for f in fields
                if f["schema"] == "dc"
                and f["element"] == "description"
                and f["qualifier"] not in {"provenance", "sponsorship"}
            ]
        abstract = max(abstracts, key=len) if abstracts else ""

        authors = self._values(fields, "contributor", "author")
        if not authors:
            authors = self._values(fields, "creator")

        published_date = (
            self._first_value(fields, "date", "issued")
            or record.get("published_date")
            or ""
        )
        publisher = self._first_value(fields, "publisher") or ""
        identifiers = self._values(fields, "identifier")
        doi = self._extract_doi(identifiers)
        subjects = self._dedupe(self._values(fields, "subject"))
        keywords = [s for s in subjects if not s.lower().startswith("ddc:")]
        category = (
            record.get("category")
            or self._first_value(fields, "edoc", "type-name", schema="local")
            or self._first_value(fields, "type")
            or ""
        )
        types = self._dedupe(self._values(fields, "type"))
        languages = self._dedupe(self._values(fields, "language"))
        bitstreams = self._parse_bitstreams(root)
        pdf_url = self._choose_pdf_url(bitstreams)

        metadata = {
            "source": "DSpace METS",
            "collection_url": self.START_URL,
            "list_url": record.get("list_url"),
            "detail_mets_url": mets_url,
            "detail_url": record.get("url") or self._detail_url(record["external_id"]),
            "dspace_handle": record["external_id"],
            "list_record": record,
            "identifiers": identifiers,
            "subjects": subjects,
            "types": types,
            "languages": languages,
            "bitstreams": bitstreams,
            "raw_dim_fields": fields,
        }

        return {
            "external_id": record["external_id"],
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": self._normalize_date(published_date),
            "url": record.get("url") or self._detail_url(record["external_id"]),
            "pdf_url": pdf_url,
            "doi": doi,
            "department": publisher,
            "metadata": metadata,
        }

    def _parse_bitstreams(self, root):
        bitstreams = []
        for group in root.findall(".//mets:fileGrp", self.XML_NS):
            use = group.attrib.get("USE", "")
            for file_node in group.findall("mets:file", self.XML_NS):
                loc = file_node.find("mets:FLocat", self.XML_NS)
                if loc is None:
                    continue
                href = loc.attrib.get(f"{{{self.XML_NS['xlink']}}}href", "")
                title = loc.attrib.get(f"{{{self.XML_NS['xlink']}}}title", "")
                if not href:
                    continue
                bitstreams.append(
                    {
                        "use": use,
                        "href": urljoin(self.base_url, href),
                        "title": title,
                        "mime_type": file_node.attrib.get("MIMETYPE", ""),
                        "size": file_node.attrib.get("SIZE", ""),
                        "checksum": file_node.attrib.get("CHECKSUM", ""),
                    }
                )
        return bitstreams

    def _choose_pdf_url(self, bitstreams):
        for bitstream in bitstreams:
            href = bitstream.get("href") or ""
            if (
                bitstream.get("use") == "CONTENT"
                and bitstream.get("mime_type") == "application/pdf"
            ):
                return href
        for bitstream in bitstreams:
            href = bitstream.get("href") or ""
            path = href.split("?", 1)[0].lower()
            if path.endswith(".pdf"):
                return href
        return ""

    def _parse_html_detail(self, soup, record, detail_url, mets_url):
        title = (
            self._first_meta(soup, "citation_title")
            or self._node_text(soup.select_one(".simple-item-view-title .h3"))
            or record.get("title")
            or ""
        )
        abstract = (
            self._first_meta(soup, "DCTERMS.abstract")
            or self._node_text(soup.select_one(".simple-item-view-description"))
            or record.get("list_abstract")
            or ""
        )
        authors = self._meta_all(soup, "citation_author")
        if not authors:
            authors = [
                self._node_text(n)
                for n in soup.select(".simple-item-view-authors div")
                if self._node_text(n)
            ]
        published_date = (
            self._first_meta(soup, "citation_date")
            or self._node_text(soup.select_one(".badge-date .date"))
            or record.get("published_date")
            or ""
        )
        category = self._node_text(soup.select_one(".badge-type .type")) or record.get("category") or ""
        pdf_url = self._first_meta(soup, "citation_pdf_url") or ""
        if not pdf_url:
            pdf_link = soup.select_one(".file-section a[href*='.pdf']")
            if pdf_link:
                pdf_url = urljoin(self.base_url, pdf_link.get("href") or "")
        doi = self._extract_doi(
            [self._node_text(soup.select_one(".badge-doi .doi")), detail_url]
        )
        publisher = self._first_meta(soup, "citation_publisher") or ""

        metadata = {
            "source": "DSpace HTML",
            "collection_url": self.START_URL,
            "list_url": record.get("list_url"),
            "detail_mets_url": mets_url,
            "detail_url": detail_url,
            "dspace_handle": record["external_id"],
            "list_record": record,
        }

        return {
            "external_id": record["external_id"],
            "title": title,
            "authors": self._dedupe(authors),
            "abstract": self._normalize_text(abstract),
            "category": category,
            "keywords": [],
            "published_date": self._normalize_date(published_date),
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": publisher,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Small parsing helpers
    # ------------------------------------------------------------------

    def _first_value(self, fields, element, qualifier=None, schema="dc"):
        values = self._values(fields, element, qualifier, schema)
        return values[0] if values else ""

    def _values(self, fields, element, qualifier=None, schema="dc"):
        values = []
        for field in fields:
            if schema and field["schema"] != schema:
                continue
            if field["element"] != element:
                continue
            if qualifier is not None and field["qualifier"] != qualifier:
                continue
            values.append(field["value"])
        return self._dedupe(values)

    def _node_text(self, node):
        if node is None:
            return ""
        for hidden in node.select(".Z3988"):
            hidden.extract()
        return self._normalize_text(node.get_text(" ", strip=True))

    def _first_meta(self, soup, name):
        values = self._meta_all(soup, name)
        return values[0] if values else ""

    def _meta_all(self, soup, name):
        values = []
        for meta in soup.find_all("meta"):
            meta_name = meta.get("name") or meta.get("property") or ""
            if meta_name.lower() != name.lower():
                continue
            value = self._normalize_text(meta.get("content") or "")
            if value:
                values.append(value)
        return self._dedupe(values)

    def _extract_doi(self, values):
        for value in values:
            text = self._normalize_text(value)
            if not text:
                continue
            text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
            match = re.search(r"\b(10\.\d{4,9}/[^\s<>\"]+)", text)
            if match:
                return match.group(1).rstrip(".,);]")
        return ""

    def _normalize_date(self, value):
        text = self._normalize_text(value)
        match = re.search(r"\d{4}(?:-\d{2})?(?:-\d{2})?", text)
        return match.group(0) if match else text

    @staticmethod
    def _normalize_text(value):
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _dedupe(values):
        seen = set()
        deduped = []
        for value in values:
            value = re.sub(r"\s+", " ", str(value or "")).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped


__all__ = ["EdocRkiDeHandleCrawler"]
