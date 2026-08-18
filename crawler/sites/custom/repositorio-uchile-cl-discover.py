# -*- coding: utf-8 -*-
"""Crawler for Universidad de Chile DSpace journal article discovery results."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class RepositorioUchileClDiscoverCrawler(BaseCrawler):
    site_id = "repositorio-uchile-cl-discover"
    site_name = "Custom: repositorio-uchile-cl-discover"
    base_url = "https://repositorio.uchile.cl"
    DELIVERY_ORDER = "newest_first"

    START_URL = (
        "https://repositorio.uchile.cl/discover?"
        "filtertype=type&filter_relational_operator=equals&filter=Art%C3%ADculo+de+revista"
    )
    PAGE_SIZE = 20
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl DSpace /discover results and save journal article records."""
        saved = 0
        page = (self.delivery_cursor or {}).get("page", 1)
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
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

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            total = self._total_results(soup)
            total_msg = f" of {total}" if total else ""
            print(
                f"[{self.site_id}] page {page}: discovered {len(records)}"
                f"{total_msg} records from DSpace /discover HTML endpoint"
            )

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"{page}.{idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

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
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            self._advance_cursor({"page": page + 1}, items_done=len(records))

            if limit is not None and saved >= limit:
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
            "Accept-Language: es-CL,es;q=0.9,en;q=0.8",
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

    def _fetch_json(self, url, context, referer=None):
        raw = self._curl_get(
            url,
            context=context,
            referer=referer,
            accept="application/json,text/plain,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} returned invalid JSON: {exc}")
            return None

    # ------------------------------------------------------------------
    # List/detail discovery
    # ------------------------------------------------------------------

    def _list_url(self, page):
        parsed = urlparse(self.START_URL)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params["rpp"] = str(self.PAGE_SIZE)
        params["sort_by"] = "dc.date.issued_dt"
        params["order"] = "desc"
        if page > 1:
            params["page"] = str(page)
        return f"{self.base_url}{parsed.path}?{urlencode(params)}"

    def _fetch_and_parse_detail(self, record, item_label, list_url):
        detail_url = record["url"]
        external_id = record.get("external_id") or self._external_id_from_url(detail_url)
        rest_url = self._rest_handle_url(external_id)

        data = self._fetch_json(
            rest_url,
            context=f"item {item_label} REST detail",
            referer=detail_url,
        )
        if isinstance(data, dict) and data.get("type") == "item":
            return self._parse_rest_detail(data, record, rest_url)

        print(f"[{self.site_id}] item {item_label}: REST detail unavailable; using HTML detail")
        html_raw = self._curl_get(
            detail_url,
            context=f"item {item_label} HTML detail",
            referer=list_url,
        )
        if not html_raw:
            raise RuntimeError("detail fetch failed after retries")

        soup = self._make_soup(html_raw)
        if soup is None:
            raise RuntimeError("detail HTML could not be parsed")
        return self._parse_html_detail(soup, detail_url, record, rest_url)

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

    def _parse_list(self, soup, list_url):
        records = []
        for item in soup.select("#aspect_discovery_SimpleSearch_div_search-results div.ds-artifact-item"):
            desc = item.select_one(".artifact-description") or item
            link = desc.select_one('a[href*="/handle/"]') or item.select_one('a[href*="/handle/"]')
            if not link:
                continue

            detail_url = self._normalize_url(urljoin(list_url, link.get("href", "")))
            if not detail_url:
                continue

            title_node = desc.select_one(".discoUch") or desc.select_one(".artifact-title a") or link
            title = self._node_text(title_node)
            authors = [self._one_line(a.get_text(" ", strip=True)) for a in desc.select(".author span")]
            authors = self._dedupe([a.strip(" ;") for a in authors if a.strip(" ;")])
            publisher = self._node_text(desc.select_one(".publisher"))
            date_node = desc.select_one(".date")
            published_date = self._normalize_date(
                self._one_line(date_node.get_text(" ", strip=True)) if date_node else ""
            )

            records.append({
                "title": title,
                "url": detail_url,
                "external_id": self._external_id_from_url(detail_url),
                "authors": authors,
                "publisher": publisher,
                "published_date": published_date,
                "list_url": list_url,
            })
        return records

    def _parse_rest_detail(self, data, record, rest_url):
        metadata = data.get("metadata") or []
        meta = self._metadata_map_from_rest(metadata)

        detail_url = self._first_meta(meta, "dc.identifier.uri") or record.get("url") or ""
        if "/handle/" not in detail_url:
            detail_url = record.get("url") or self._handle_url(data.get("handle") or "")
        detail_url = self._normalize_url(detail_url)

        title = self._first_meta(meta, "dc.title") or data.get("name") or record.get("title") or ""
        title = self._one_line(title)
        if not title:
            raise RuntimeError("detail record has no title")

        authors = self._all_meta(meta, "dc.contributor.author")
        if not authors:
            authors = record.get("authors") or []
        authors = self._dedupe([self._one_line(a).strip(" ;") for a in authors if self._one_line(a).strip(" ;")])

        abstract = "\n\n".join(self._all_meta(meta, "dc.description.abstract"))
        abstract = self._clean_text(abstract)

        keywords = self._all_meta(meta, "dc.subject")
        category = self._first_meta(meta, "dc.type") or "Artículo de revista"
        if category:
            keywords.append(category)
        keywords = self._dedupe([self._one_line(k) for k in keywords if self._one_line(k)])

        published_date = self._normalize_date(
            self._first_meta(meta, "dc.date.issued") or record.get("published_date") or ""
        )
        publisher = self._first_meta(meta, "dc.publisher") or record.get("publisher") or ""

        bitstreams = self._bitstream_records(data)
        pdf_url = self._pdf_from_bitstreams(bitstreams)
        doi = self._doi_from_values(
            self._all_meta(meta, "dc.identifier.doi", "dc.identifier.other", "dc.identifier")
        )
        collection = self._collection_from_rest(data)
        communities = self._communities_from_rest(data)
        department = self._department(publisher, collection, communities)

        citation = self._first_meta(meta, "dc.identifier.citation")
        source = self._first_meta(meta, "dc.source")
        descriptions = self._all_meta(meta, "dc.description")
        rights = self._all_meta(meta, "dc.rights", "dc.rights.uri")

        return {
            "external_id": data.get("handle") or record.get("external_id") or self._external_id_from_url(detail_url),
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": department,
            "metadata": {
                "handle": data.get("handle") or record.get("external_id"),
                "uuid": data.get("uuid"),
                "documentType": category,
                "publisher": publisher,
                "collection": collection,
                "communities": communities,
                "journal": source,
                "citation": citation,
                "issn": self._all_meta(meta, "dc.identifier.issn"),
                "language": self._first_meta(meta, "dc.language.iso", "dc.language"),
                "dateAccessioned": self._first_meta(meta, "dc.date.accessioned"),
                "dateAvailable": self._first_meta(meta, "dc.date.available"),
                "descriptions": descriptions,
                "rights": rights,
                "version": self._all_meta(meta, "dc.description.version"),
                "access": self._all_meta(meta, "dc.rights.accessRights"),
                "bitstreams": bitstreams,
                "lastModified": data.get("lastModified"),
                "listEndpoint": record.get("list_url") or self.START_URL,
                "detailEndpoint": detail_url,
                "restDetailEndpoint": rest_url,
                "metadataEndpoint": self._metadata_endpoint(data.get("handle") or record.get("external_id")),
                "source": "DSpace 6.3 XMLUI /discover HTML list + /rest/handle JSON detail",
                "abstractLength": len(abstract),
            },
        }

    def _parse_html_detail(self, soup, detail_url, record, rest_url):
        meta = self._metadata_map_from_html(soup)
        title = self._first_meta(meta, "DC.title", "citation_title") or record.get("title") or ""
        title = self._one_line(title)
        if not title:
            h1 = soup.select_one(".item-summary-view-metadata h1, .item-summary-view-metadata h2, h1, h2")
            title = self._node_text(h1)
        if not title:
            raise RuntimeError("detail page has no title")

        authors = self._all_meta(meta, "DC.creator", "citation_author")
        if not authors:
            authors = self._authors_from_body(soup)
        if not authors:
            authors = record.get("authors") or []
        authors = self._dedupe([self._one_line(a).strip(" ;") for a in authors if self._one_line(a).strip(" ;")])

        abstract = self._first_meta(meta, "DCTERMS.abstract", "DC.description.abstract")
        if not abstract:
            abstract = self._abstract_from_body(soup)
        abstract = self._clean_text(abstract)

        keywords = self._all_meta(meta, "DC.subject")
        citation_keywords = self._first_meta(meta, "citation_keywords")
        if citation_keywords:
            keywords.extend([part.strip() for part in citation_keywords.split(";")])
        category = self._first_meta(meta, "DC.type") or "Artículo de revista"
        if category:
            keywords.append(category)
        keywords = self._dedupe([self._one_line(k) for k in keywords if self._one_line(k)])

        published_date = self._normalize_date(
            self._first_meta(meta, "DCTERMS.issued", "citation_date")
            or record.get("published_date")
            or ""
        )

        pdf_url = self._first_meta(meta, "citation_pdf_url")
        if not pdf_url:
            pdf_url = self._pdf_from_body(soup, detail_url)
        pdf_url = self._normalize_url(urljoin(detail_url, pdf_url)) if pdf_url else ""

        doi = self._first_meta(meta, "citation_doi", "DC.identifier.doi")
        if not doi:
            doi = self._doi_from_values(self._all_meta(meta, "DC.identifier"))

        publisher = self._first_meta(meta, "DC.publisher", "citation_publisher") or record.get("publisher") or ""
        collection = self._collection_from_body(soup)
        contributors = self._all_meta(meta, "DC.contributor")
        descriptions = self._all_meta(meta, "DC.description")
        department = self._department(publisher, collection, contributors + descriptions)

        external_id = record.get("external_id") or self._external_id_from_url(detail_url)
        return {
            "external_id": external_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": doi or "",
            "department": department,
            "metadata": {
                "handle": external_id,
                "documentType": category,
                "publisher": publisher,
                "collection": collection,
                "contributors": contributors,
                "descriptions": descriptions,
                "language": self._first_meta(meta, "DC.language", "citation_language"),
                "dateAccepted": self._first_meta(meta, "DCTERMS.dateAccepted"),
                "dateAvailable": self._first_meta(meta, "DCTERMS.available"),
                "rights": self._all_meta(meta, "DC.rights"),
                "listEndpoint": record.get("list_url") or self.START_URL,
                "detailEndpoint": detail_url,
                "restDetailEndpoint": rest_url,
                "metadataEndpoint": self._metadata_endpoint(external_id),
                "source": "DSpace 6.3 XMLUI /discover list and /handle detail HTML meta tags",
                "abstractLength": len(abstract),
            },
        }

    def _metadata_map_from_rest(self, metadata):
        meta = {}
        for item in metadata:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            value = item.get("value")
            if not key or value is None:
                continue
            cleaned = self._clean_text(value)
            if cleaned:
                meta.setdefault(key, []).append(cleaned)
        return meta

    def _metadata_map_from_html(self, soup):
        meta = {}
        for node in soup.find_all("meta"):
            name = node.get("name") or node.get("property")
            content = node.get("content")
            if not name or content is None:
                continue
            name = self._one_line(name)
            value = self._clean_text(content)
            if value:
                meta.setdefault(name, []).append(value)
        return meta

    def _all_meta(self, meta, *names):
        values = []
        for name in names:
            values.extend(meta.get(name, []))
        return self._dedupe(values)

    def _first_meta(self, meta, *names):
        for name in names:
            for value in meta.get(name) or []:
                if value:
                    return value
        return ""

    @classmethod
    def _clean_text(cls, value):
        if value is None:
            return ""
        text = unescape(str(value))
        for _ in range(2):
            updated = unescape(text)
            if updated == text:
                break
            text = updated
        text = text.replace("\xa0", " ").replace("\u200b", "")
        text = text.replace("&#xD;", "\n").replace("&#xA;", "\n")
        text = text.replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    def _node_text(self, node):
        if node is None:
            return ""
        clone = self._make_soup(str(node))
        if clone is None:
            return self._one_line(node.get_text(" ", strip=True))
        for junk in clone.select(".Z3988, script, style"):
            junk.decompose()
        return self._one_line(clone.get_text(" ", strip=True))

    def _authors_from_body(self, soup):
        wrapper = soup.select_one(".simple-item-view-authors")
        if not wrapper:
            return []
        label = self._node_text(wrapper.select_one(".boldc")).lower()
        if label and "author" not in label:
            return []
        return [
            self._one_line(a.get_text(" ", strip=True)).strip(" ;")
            for a in wrapper.select("li, a")
            if self._one_line(a.get_text(" ", strip=True)).strip(" ;")
        ]

    def _abstract_from_body(self, soup):
        for wrapper in soup.select(".simple-item-view-description"):
            label = self._node_text(wrapper.select_one(".boldc")).lower()
            if label == "abstract":
                content = wrapper.select_one('[itemprop="description"]')
                if content:
                    return self._node_text(content)
                clone = self._make_soup(str(wrapper))
                if clone is None:
                    return self._node_text(wrapper)
                bold = clone.select_one(".boldc")
                if bold:
                    bold.decompose()
                return self._node_text(clone)
        for row in soup.select("tr"):
            cells = row.find_all("td")
            if len(cells) >= 3 and "description.abstract" in self._one_line(cells[1].get_text(" ", strip=True)):
                return self._node_text(cells[2])
        return ""

    def _pdf_from_body(self, soup, detail_url):
        for link in soup.select('a[href*="/bitstream/handle/"]'):
            href = link.get("href") or ""
            lower = href.lower()
            if ".pdf" in lower and ".pdf.txt" not in lower and "license" not in lower:
                return self._normalize_url(urljoin(detail_url, href))
        return ""

    def _pdf_from_bitstreams(self, bitstreams):
        for bitstream in bitstreams:
            name = (bitstream.get("name") or "").lower()
            mime = (bitstream.get("mimeType") or "").lower()
            bundle = (bitstream.get("bundleName") or "").upper()
            if "pdf" not in mime and not name.endswith(".pdf"):
                continue
            if name.endswith(".pdf.txt") or "license" in name:
                continue
            if bundle not in ("ORIGINAL", ""):
                continue
            link = bitstream.get("retrieveLink") or ""
            if link:
                return self._normalize_url(urljoin(self.base_url, link))
        return ""

    def _bitstream_records(self, data):
        records = []
        for bitstream in data.get("bitstreams") or []:
            if not isinstance(bitstream, dict):
                continue
            records.append({
                "uuid": bitstream.get("uuid"),
                "name": bitstream.get("name"),
                "bundleName": bitstream.get("bundleName"),
                "mimeType": bitstream.get("mimeType"),
                "format": bitstream.get("format"),
                "sizeBytes": bitstream.get("sizeBytes"),
                "retrieveLink": urljoin(self.base_url, bitstream.get("retrieveLink") or "")
                if bitstream.get("retrieveLink") else "",
                "checksum": (bitstream.get("checkSum") or {}).get("value"),
            })
        return records

    def _collection_from_body(self, soup):
        collections = []
        for link in soup.select(".simple-item-view-collections a[href*='/handle/']"):
            text = self._node_text(link)
            if text:
                collections.append(text)
        if not collections:
            for link in soup.select(".breadcrumb a[href*='/handle/']"):
                text = self._node_text(link)
                if text:
                    collections.append(text)
        return "; ".join(self._dedupe(collections))

    def _collection_from_rest(self, data):
        collection = data.get("parentCollection") or {}
        if isinstance(collection, dict):
            return self._one_line(collection.get("name") or "")
        return ""

    def _communities_from_rest(self, data):
        names = []
        for community in data.get("parentCommunityList") or []:
            if isinstance(community, dict) and community.get("name"):
                names.append(self._one_line(community.get("name")))
        return self._dedupe(names)

    def _department(self, publisher, collection, extra_values):
        parts = []
        for value in [publisher, collection]:
            value = self._one_line(value)
            if value:
                parts.append(value)
        for value in extra_values:
            value = self._one_line(value)
            if self._looks_institutional(value):
                parts.append(value)
        parts = self._dedupe(parts)
        return "; ".join(parts) if parts else "Universidad de Chile"

    @staticmethod
    def _looks_institutional(value):
        lowered = value.lower()
        markers = (
            "universidad",
            "facultad",
            "departamento",
            "instituto",
            "escuela",
            "centro",
        )
        return any(marker in lowered for marker in markers)

    def _doi_from_values(self, values):
        for value in values:
            candidate = self._one_line(value)
            if "doi.org/" in candidate:
                return candidate.split("doi.org/", 1)[1].strip().rstrip(".")
            match = re.search(r"(10\.\d{4,9}/[^\s\"<>]+)", candidate)
            if match:
                return match.group(1).strip().rstrip(".")
        return ""

    def _total_results(self, soup):
        node = soup.select_one(".pagination-info")
        text = self._one_line(node.get_text(" ", strip=True)) if node else ""
        match = re.search(r"of\s+([\d,]+)", text, flags=re.I)
        return match.group(1) if match else ""

    def _external_id_from_url(self, url):
        path = urlparse(url).path
        match = re.search(r"/handle/([^/]+/[^/?#]+)", path)
        if match:
            return match.group(1)
        return self._one_line(url)

    def _rest_handle_url(self, external_id):
        if not external_id:
            return ""
        return (
            f"{self.base_url}/rest/handle/{external_id}"
            "?expand=metadata,bitstreams,parentCollection,parentCommunityList"
        )

    def _handle_url(self, external_id):
        return f"{self.base_url}/handle/{external_id}" if external_id else ""

    def _metadata_endpoint(self, external_id):
        if not external_id or "/" not in external_id:
            return ""
        return f"{self.base_url}/metadata/handle/{external_id}/mets.xml"

    @staticmethod
    def _normalize_url(url):
        if not url:
            return ""
        return url.replace(" ", "%20")

    @staticmethod
    def _normalize_date(value):
        value = (value or "").strip()
        match = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", value)
        if not match:
            return value
        if match.group(3):
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if match.group(2):
            return f"{match.group(1)}-{match.group(2)}"
        return match.group(1)

    @staticmethod
    def _dedupe(values):
        seen = set()
        out = []
        for value in values:
            if value is None:
                continue
            key = str(value).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out
