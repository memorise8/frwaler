# -*- coding: utf-8 -*-
"""Crawler for the FiBL English publications archive.

The FiBL page renders the latest Organic Eprints records server-side through a
TYPO3 RSS display block.  The record detail pages and rich XML export live at
orgprints.org (EPrints).  The crawler uses the FiBL-rendered list as the
reliable entry point, enriches from the EPrints XML export when reachable, and
then follows archive/search links exposed on the same page for full-depth
pagination.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from html import unescape
from urllib.parse import unquote, urljoin, urlparse


_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_START_URL = "https://www.fibl.org/en/info-centre/publications-archive-fibl-en"
_ORGPRINTS = "https://orgprints.org"
_PAGE_SIZE = 100
_MAX_PAGES = 200
_MAX_WALL_SECS = 25 * 60
_STOP_SOON_SECS = 30
_MIN_ABSTRACT_CHARS = 100
_BACKOFFS = (1, 3, 9)

_BS_PARSER_CACHE = [None]


def _make_soup(raw):
    """Return BeautifulSoup using the required tolerant parser chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[fibl-org-en] BeautifulSoup unavailable: {exc}")
        return None

    parsers = ["html5lib", "lxml", "html.parser"]
    cached = _BS_PARSER_CACHE[0]
    if cached:
        parsers = [cached] + [p for p in parsers if p != cached]

    for parser in parsers:
        try:
            soup = BeautifulSoup(raw, parser)
            _BS_PARSER_CACHE[0] = parser
            return soup
        except Exception as exc:
            print(f"[fibl-org-en] BeautifulSoup {parser} failed: {exc}")
            continue
    return None


def _clean_text(value) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = _clean_text(item)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _split_people(value: str) -> list[str]:
    value = _clean_text(value)
    if not value:
        return []
    parts = []
    for chunk in value.split(";"):
        chunk = _clean_text(chunk)
        if not chunk:
            continue
        parts.extend(re.split(r"\s+\band\b\s+", chunk))
    return _dedupe(parts)


def _normalize_date(value: str | None) -> str | None:
    raw = _clean_text(value)
    if not raw:
        return None
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})[-/.](\d{1,2})", raw)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01"
    m = re.search(r"\b(19\d{2}|20\d{2})\b", raw)
    if m:
        return f"{int(m.group(1)):04d}-01-01"
    return None


def _eprint_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/id/eprint/(\d+)/?", url)
    return m.group(1) if m else None


def _canonical_orgprints_url(url: str) -> str:
    ep_id = _eprint_id_from_url(url)
    if ep_id:
        return f"{_ORGPRINTS}/id/eprint/{ep_id}/"
    return url


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    name = unquote(path.rsplit("/", 1)[-1])
    if "." in name and len(name) <= 200:
        return name
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(el, name: str):
    return [child for child in list(el) if _local_name(child.tag) == name]


def _first_child(el, name: str):
    for child in list(el):
        if _local_name(child.tag) == name:
            return child
    return None


def _text(el, name: str) -> str:
    child = _first_child(el, name)
    if child is None or child.text is None:
        return ""
    return _clean_text(child.text)


def _first_text(el, names: tuple[str, ...]) -> str:
    for name in names:
        value = _text(el, name)
        if value:
            return value
    return ""


def _xml_people(eprint, container_name: str) -> list[str]:
    people = []
    container = _first_child(eprint, container_name)
    if container is None:
        return people
    for item in list(container):
        name_el = _first_child(item, "name")
        if name_el is not None:
            family = _text(name_el, "family")
            given = _text(name_el, "given")
            name = _clean_text(f"{family}, {given}".strip(", "))
            if name:
                people.append(name)
                continue
        direct = _clean_text(" ".join(item.itertext()))
        if direct:
            people.append(direct)
    return _dedupe(people)


def _parse_xml_detail(xml_text: str, ep_id_hint: str | None = None) -> dict | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(f"[fibl-org-en] XML parse failed: {exc}")
        return None

    eprint = None
    for node in root.iter():
        if _local_name(node.tag) == "eprint":
            eprint = node
            break
    if eprint is None:
        return None

    eprint_id = _first_text(eprint, ("eprintid", "id")) or ep_id_hint
    title = _first_text(eprint, ("title", "addtitle"))
    abstract = _first_text(eprint, ("abstract", "engabstract"))
    date_raw = _first_text(eprint, ("date", "datestamp"))
    published_date = _normalize_date(date_raw)
    category = _first_text(eprint, ("type", "eprint_status"))
    keywords_raw = _first_text(eprint, ("keywords",))
    doi_raw = _first_text(eprint, ("doi", "id_number"))
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_raw).strip() if doi_raw else None
    journal = _first_text(eprint, ("publication",))
    series = _first_text(eprint, ("series",))
    volume = _first_text(eprint, ("volume",))
    issue = _first_text(eprint, ("issue", "number"))
    publisher = _first_text(eprint, ("publisher", "institution", "organisation"))
    department = _first_text(eprint, ("department", "division"))
    authors = _xml_people(eprint, "creators")
    editors = _xml_people(eprint, "editors")

    pdf_url = None
    original_filename = None
    documents = _first_child(eprint, "documents")
    if documents is not None:
        for doc_el in list(documents):
            security = _text(doc_el, "security")
            if security and security.lower() != "public":
                continue
            files_el = _first_child(doc_el, "files")
            if files_el is None:
                continue
            for file_el in list(files_el):
                raw_url = _text(file_el, "url")
                filename = _text(file_el, "filename")
                mime = _text(file_el, "mime_type").lower()
                if not raw_url:
                    continue
                full_url = urljoin(_ORGPRINTS + "/", raw_url)
                if "pdf" in mime or full_url.lower().split("?", 1)[0].endswith(".pdf"):
                    pdf_url = full_url
                    original_filename = filename or _filename_from_url(full_url)
                    break
            if pdf_url:
                break

    keywords = ", ".join(_dedupe(k for k in re.split(r"[,;]", keywords_raw) if k.strip())) if keywords_raw else None

    raw_fields = {}
    for name in (
        "eprintid",
        "type",
        "date",
        "datestamp",
        "ispublished",
        "refereed",
        "publisher",
        "publication",
        "series",
        "volume",
        "issue",
        "number",
        "pagerange",
        "pages",
        "place_of_pub",
        "department",
        "institution",
        "official_url",
        "id_number",
    ):
        value = _text(eprint, name)
        if value:
            raw_fields[name] = value

    return {
        "external_id": str(eprint_id) if eprint_id else None,
        "post_number": str(eprint_id) if eprint_id else None,
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "listed_date": published_date,
        "posted_date_raw": date_raw,
        "authors": "; ".join(authors) if authors else None,
        "editors": "; ".join(editors) if editors else None,
        "publisher": publisher or None,
        "department": department or None,
        "journal": journal or None,
        "pdf_url": pdf_url,
        "keywords": keywords,
        "category": category or None,
        "doi": doi or None,
        "original_filename": original_filename,
        "series": series or None,
        "volume": volume or None,
        "issue": issue or None,
        "url": f"{_ORGPRINTS}/id/eprint/{eprint_id}/" if eprint_id else None,
        "raw_xml_fields": raw_fields,
    }


def _parse_list_description(description: str, title: str) -> dict:
    desc = _clean_text(description)
    title = _clean_text(title)
    year_match = re.search(r"\((19\d{2}|20\d{2})\)", desc)
    year_raw = year_match.group(1) if year_match else None
    listed_date = _normalize_date(year_raw)
    category = None
    series = None
    publisher = None
    authors = []

    creator_match = re.search(r"Creator\(s\):\s*(.*?)(?:\.\s+|$)", desc)
    org_match = re.search(r"Issuing Organisation\(s\):\s*(.*?)(?:\.\s+|$)", desc)
    if creator_match:
        authors = _split_people(creator_match.group(1))
    elif year_match:
        authors = _split_people(desc[:year_match.start()])

    if desc.startswith("{") and "}" in desc[:40]:
        category = desc[1:desc.index("}")].strip() or None

    if org_match:
        publisher = _clean_text(org_match.group(1)) or None
    elif year_match:
        tail = _clean_text(desc[year_match.end():])
        if title and tail.lower().startswith(title.lower()):
            tail = _clean_text(tail[len(title):])
        pieces = [_clean_text(p) for p in re.split(r"\.\s+", tail) if _clean_text(p)]
        if pieces:
            series = pieces[0]
        if len(pieces) >= 2:
            publisher = pieces[-1].rstrip(".") or None

    return {
        "abstract": desc,
        "authors": "; ".join(authors) if authors else None,
        "publisher": publisher,
        "category": category or series,
        "series": series,
        "published_date": listed_date,
        "listed_date": listed_date,
        "posted_date_raw": year_raw,
    }


class FiblOrgEnCrawler(BaseCrawler):
    site_id = "fibl-org-en"
    site_name = "Custom: fibl-org-en"
    base_url = "https://www.fibl.org"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        self._detail_delay = delay

    def _curl_text(
        self,
        url: str,
        context: str,
        *,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        timeout: int = 45,
        retries: int = 3,
    ) -> tuple[str, str]:
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-skL",
                        "--compressed",
                        "--connect-timeout",
                        "15",
                        "--max-time",
                        str(timeout),
                        "-w",
                        "\n__CURL_STATUS__:%{http_code}\n__CURL_EFFECTIVE_URL__:%{url_effective}",
                        "-H",
                        f"User-Agent: {self.USER_AGENT}",
                        "-H",
                        f"Accept: {accept}",
                        url,
                    ],
                    capture_output=True,
                    timeout=timeout + 10,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                body, status, effective_url = self._split_curl_output(text, url)
                status_int = int(status) if status.isdigit() else 0
                if result.returncode == 0 and body.strip() and status_int < 400:
                    return body, effective_url
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                error = stderr or f"curl exit={result.returncode} http={status or 'unknown'}"
                print(f"[{self.site_id}] curl {context} attempt {attempt + 1}/{retries} failed: {error}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl {context} attempt {attempt + 1}/{retries} failed: {exc}")

            if attempt < retries - 1:
                time.sleep(_BACKOFFS[min(attempt, len(_BACKOFFS) - 1)])

        print(f"[{self.site_id}] curl {context} gave up after {retries} attempts: {url}")
        return "", url

    @staticmethod
    def _split_curl_output(text: str, fallback_url: str) -> tuple[str, str, str]:
        status_marker = "\n__CURL_STATUS__:"
        url_marker = "\n__CURL_EFFECTIVE_URL__:"
        if status_marker not in text:
            return text, "", fallback_url
        body, trailer = text.rsplit(status_marker, 1)
        if url_marker in trailer:
            status, effective_url = trailer.split(url_marker, 1)
        else:
            status, effective_url = trailer, fallback_url
        return body, status.strip(), effective_url.strip() or fallback_url

    def _parse_fibl_items(self, raw: str, page_url: str) -> list[dict]:
        soup = _make_soup(raw)
        if soup is None:
            return []
        items = []
        for li in soup.select("li.tx-rssdisplay-item"):
            try:
                link = li.select_one('a[href*="/id/eprint/"]')
                if link is None:
                    continue
                url = _canonical_orgprints_url(urljoin(page_url, link.get("href", "")))
                ep_id = _eprint_id_from_url(url)
                title = _clean_text(link.get_text(" ", strip=True))
                desc_el = li.select_one(".tx-rssdisplay-item-description") or li
                desc_text = _clean_text(desc_el.get_text(" ", strip=True))
                tail = _clean_text(f"orgprints.org: {title}")
                if tail and desc_text.endswith(tail):
                    desc_text = _clean_text(desc_text[: -len(tail)])
                parsed = _parse_list_description(desc_text, title)
                items.append({
                    "external_id": ep_id,
                    "post_number": ep_id,
                    "title": title,
                    "url": url,
                    "source_page_url": page_url,
                    "list_description": desc_text,
                    **parsed,
                })
            except Exception as exc:
                print(f"[fibl-org-en] list item parse failed: {exc}")
                continue
        return items

    def _parse_orgprints_search_items(self, raw: str, page_url: str) -> list[dict]:
        soup = _make_soup(raw)
        if soup is None:
            return []
        items = []
        seen_ids = set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "/id/eprint/" not in href:
                continue
            url = _canonical_orgprints_url(urljoin(page_url, href))
            ep_id = _eprint_id_from_url(url)
            if not ep_id or ep_id in seen_ids:
                continue
            seen_ids.add(ep_id)
            title = _clean_text(link.get_text(" ", strip=True))
            parent_text = _clean_text(link.find_parent().get_text(" ", strip=True) if link.find_parent() else title)
            parsed = _parse_list_description(parent_text, title)
            items.append({
                "external_id": ep_id,
                "post_number": ep_id,
                "title": title,
                "url": url,
                "source_page_url": page_url,
                "list_description": parent_text,
                **parsed,
            })
        return items

    def _extract_archive_urls(self, raw: str, page_url: str) -> list[str]:
        soup = _make_soup(raw)
        if soup is None:
            return []
        urls = []
        for link in soup.find_all("a", href=True):
            text = _clean_text(link.get_text(" ", strip=True))
            href = link.get("href", "")
            if "orgprints.org/cgi/search" not in href:
                continue
            if not (
                text.startswith("All FiBL publications")
                or text.startswith("Peer reviewed")
                or text.startswith("Publications")
                or text.startswith("Publikations")
            ):
                continue
            full_url = urljoin(page_url, href.replace("&amp;", "&"))
            if "n=" not in full_url:
                separator = "&" if "?" in full_url else "?"
                full_url = f"{full_url}{separator}n={_PAGE_SIZE}"
            urls.append(full_url)
        return _dedupe(urls)

    def _fetch_xml_detail(self, ep_id: str) -> dict | None:
        xml_url = f"{_ORGPRINTS}/cgi/export/eprint/{ep_id}/XML/EP3_XML/"
        xml_text, effective_url = self._curl_text(
            xml_url,
            f"detail XML {ep_id}",
            accept="application/xml,text/xml,*/*",
            timeout=40,
            retries=3,
        )
        if not xml_text:
            return None
        detail = _parse_xml_detail(xml_text, ep_id)
        if detail is not None:
            detail["detail_fetch_url"] = xml_url
            detail["detail_effective_url"] = effective_url
        return detail

    def _merge_record(self, item: dict, detail: dict | None) -> dict:
        detail = detail or {}
        merged = dict(item)
        for key, value in detail.items():
            if value not in (None, "", [], {}):
                merged[key] = value
        if detail.get("abstract") and len(detail["abstract"]) < _MIN_ABSTRACT_CHARS:
            merged["abstract"] = item.get("abstract")
        if not merged.get("url"):
            ep_id = merged.get("external_id") or merged.get("post_number")
            if ep_id:
                merged["url"] = f"{_ORGPRINTS}/id/eprint/{ep_id}/"
        return merged

    def _paper_from_record(self, record: dict, detail: dict | None = None) -> dict:
        external_id = str(record.get("external_id") or record.get("post_number") or "")
        url = _canonical_orgprints_url(record.get("url") or f"{_ORGPRINTS}/id/eprint/{external_id}/")
        post_number = str(record.get("post_number") or external_id) if external_id else None
        listed_date = record.get("listed_date") or record.get("published_date")
        posted_raw = record.get("posted_date_raw") or listed_date
        original_filename = record.get("original_filename") or _filename_from_url(record.get("pdf_url"))

        metadata = {
            "posted_date": posted_raw,
            "originalFilename": original_filename,
            "journal_raw": record.get("journal"),
            "series": record.get("series"),
            "volume": record.get("volume"),
            "issue": record.get("issue"),
            "eprint_id": external_id,
            "eprintid": external_id,
            "node_id": external_id,
            "post_number": post_number,
            "listed_date": listed_date,
            "source_page_url": record.get("source_page_url"),
            "list_description": record.get("list_description"),
            "detail_fetch_url": record.get("detail_fetch_url"),
            "detail_effective_url": record.get("detail_effective_url"),
            "raw_xml_fields": record.get("raw_xml_fields"),
            "detail_enriched": bool(detail),
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": f"{self.site_id}-{external_id or abs(hash(url))}",
            "site_id": self.site_id,
            "external_id": external_id or url,
            "post_number": post_number,
            "title": record.get("title"),
            "abstract": record.get("abstract"),
            "published_date": record.get("published_date"),
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": record.get("authors"),
            "publisher": record.get("publisher"),
            "department": record.get("department"),
            "journal": record.get("journal"),
            "url": url,
            "pdf_url": record.get("pdf_url"),
            "keywords": record.get("keywords"),
            "category": record.get("category"),
            "doi": record.get("doi"),
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None) -> int:
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        seen_urls = set()
        start = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        page_urls = [_START_URL]

        for p in range(_MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            elapsed = time.monotonic() - start
            if elapsed >= _MAX_WALL_SECS - _STOP_SOON_SECS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly.")
                break
            if p >= len(page_urls):
                break
            if p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

            page_url = page_urls[p]
            raw, effective_url = self._curl_text(page_url, f"list page {p}", timeout=60, retries=3)
            if not raw:
                print(f"[{self.site_id}] page {p}: no response body; stopping pagination.")
                break

            if p == 0:
                for archive_url in self._extract_archive_urls(raw, effective_url):
                    if archive_url not in page_urls:
                        page_urls.append(archive_url)
                items = self._parse_fibl_items(raw, effective_url)
            else:
                items = self._parse_orgprints_search_items(raw, effective_url)

            if not items:
                print(f"[{self.site_id}] page {p}: 0 records; stopping pagination.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                label = item.get("external_id") or item.get("url") or "unknown"
                try:
                    url = _canonical_orgprints_url(item.get("url") or "")
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)
                    new_on_page += 1

                    detail = None
                    ep_id = item.get("external_id") or _eprint_id_from_url(url)
                    if ep_id:
                        detail = self._fetch_xml_detail(str(ep_id))
                        time.sleep(self._detail_delay)

                    record = self._merge_record(item, detail)
                    abstract = _clean_text(record.get("abstract"))
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(f"[{self.site_id}] item {label}: abstract too short ({len(abstract)} chars); skipping.")
                        continue
                    record["abstract"] = abstract

                    title = _clean_text(record.get("title"))
                    if not title:
                        print(f"[{self.site_id}] item {label}: missing title; skipping.")
                        continue
                    record["title"] = title

                    self._save_paper(self._paper_from_record(record, detail))
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {p}: all records already seen; stopping pagination.")
                break
        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping.")

        print(f"[{self.site_id}] Done. Saved {saved} records total.")
        return saved
