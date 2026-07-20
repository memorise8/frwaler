# -*- coding: utf-8 -*-
"""Crawler for HFML-FELIX English publications."""

from __future__ import annotations

import html
import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except Exception:  # pragma: no cover - dependency availability is environment-specific.
    _BeautifulSoup = None


def _make_soup(raw):
    """Build BeautifulSoup with a parser fallback chain."""
    if not raw or _BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[hfml-felix-nl-en] BeautifulSoup({parser}) failed: {exc}")
    return None


def _clean(value):
    if value is None:
        return ""
    value = html.unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


class HfmlFelixNlEnCrawler(BaseCrawler):
    site_id = "hfml-felix-nl-en"
    site_name = "Custom: hfml-felix-nl-en"
    base_url = "https://www.hfml-felix.nl"

    _PUBLICATIONS_URL = "https://www.hfml-felix.nl/en/publications/"
    _S2_API = (
        "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
        "?fields=abstract,title,authors,year,publicationDate,openAccessPdf"
    )
    _CROSSREF_API = "https://api.crossref.org/works/{doi}"
    _MIN_ABSTRACT_CHARS = 100
    _PAGE_CAP = 200
    _WALL_CLOCK_SECS = 25 * 60

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url, *, method="GET", data=None, timeout=45, headers=None):
        """Fetch via curl with the requested TLS flags and 1/3/9s retry backoff."""
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
            "Accept-Language: en-US,en;q=0.9",
        ]
        for key, value in (headers or {}).items():
            cmd.extend(["-H", f"{key}: {value}"])
        if method.upper() == "POST":
            cmd.extend(["-X", "POST", "-H", "Content-Type: application/json", "--data-raw", data or "{}"])
        cmd.append(url)

        last_err = "unknown"
        for attempt, wait in enumerate((1, 3, 9), start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                last_err = f"rc={result.returncode}"
            except Exception as exc:
                last_err = str(exc)
            if attempt < 3:
                print(f"[{self.site_id}] curl retry {attempt}/3 for {url}: {last_err}; wait {wait}s")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    def _curl_head(self, url, *, timeout=20):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skIL",
            "--connect-timeout",
            "10",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
            if result.returncode == 0:
                return (result.stdout or b"").decode("utf-8", errors="replace")
        except Exception:
            return ""
        return ""

    # ------------------------------------------------------------------
    # List API
    # ------------------------------------------------------------------

    def _facetwp_payload(self, page):
        return {
            "action": "facetwp_refresh",
            "data": {
                "facets": {
                    "search_publications": "",
                    "research_group": [],
                    "research_line": [],
                    "publication_year_sorted": [],
                    "pagination": [],
                },
                "frozen_facets": {},
                "http_params": {"get": {}, "uri": "en/publications", "url_vars": []},
                "template": "wp",
                "extras": {"selections": True},
                "soft_refresh": 1 if page > 1 else 0,
                "is_bfcache": 1 if page > 1 else 0,
                "first_load": 0 if page > 1 else 1,
                "paged": page,
            },
        }

    def _fetch_list_page(self, page):
        if page == 1:
            return self._curl(
                self._PUBLICATIONS_URL,
                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            )
        payload = json.dumps(self._facetwp_payload(page), ensure_ascii=False)
        return self._curl(
            self._PUBLICATIONS_URL,
            method="POST",
            data=payload,
            headers={"Accept": "text/plain,application/json,*/*;q=0.8"},
        )

    def _parse_page_response(self, raw, page):
        """Return (items, pager_info, raw_payload) for HTML or FacetWP JSON."""
        if not raw:
            return [], {}, {}

        payload = {}
        template_html = raw
        pager_html = ""
        if raw.lstrip().startswith("{"):
            try:
                payload = json.loads(raw)
                template_html = payload.get("template") or ""
                pager_html = (payload.get("facets") or {}).get("pagination") or ""
            except Exception as exc:
                print(f"[{self.site_id}] page {page}: JSON parse failed: {exc}")
                template_html = raw
        else:
            fwp_json = self._extract_fwp_json(raw)
            if fwp_json:
                payload = {"preload_data": fwp_json.get("preload_data"), "settings": fwp_json.get("preload_data", {}).get("settings")}
                pager_html = (
                    (fwp_json.get("preload_data") or {}).get("facets") or {}
                ).get("pagination") or ""

        soup = _make_soup(template_html)
        items = self._parse_publication_items(soup) if soup else []

        pager_info = self._parse_pager(pager_html)
        settings = payload.get("settings") or ((payload.get("preload_data") or {}).get("settings") if payload else None)
        if isinstance(settings, dict):
            pager_settings = settings.get("pager") or {}
            if pager_settings:
                pager_info.setdefault("page", pager_settings.get("page"))
                pager_info.setdefault("total_pages", pager_settings.get("total_pages"))
                pager_info.setdefault("total_rows", pager_settings.get("total_rows"))

        return items, pager_info, payload

    def _extract_fwp_json(self, raw):
        marker = "window.FWP_JSON = "
        idx = raw.find(marker)
        if idx < 0:
            return {}
        idx += len(marker)
        end = raw.find(";</script>", idx)
        if end < 0:
            end = raw.find(";\n", idx)
        if end < 0:
            return {}
        try:
            return json.loads(raw[idx:end])
        except Exception:
            return {}

    def _parse_pager(self, pager_html):
        info = {"has_next": False}
        if not pager_html:
            return info
        soup = _make_soup(pager_html)
        if soup is None:
            return info
        next_link = soup.select_one(".facetwp-page.next[data-page]")
        if next_link and next_link.get("data-page"):
            info["has_next"] = True
            info["next_page"] = next_link.get("data-page")
        active = soup.select_one(".facetwp-page.active[data-page]")
        if active and active.get("data-page"):
            info["page"] = active.get("data-page")
        last = soup.select_one(".facetwp-page.last[data-page]")
        if last and last.get("data-page"):
            info["total_pages"] = last.get("data-page")
        return info

    def _parse_publication_items(self, soup):
        if soup is None:
            return []
        template = soup.find(class_="facetwp-template")
        if template is None:
            template = soup

        records = []
        for details in template.find_all("details"):
            try:
                record = self._parse_details(details)
                if record and record.get("title"):
                    records.append(record)
            except Exception as exc:
                print(f"[{self.site_id}] list item parse failed: {exc}")
        return records

    def _parse_details(self, details):
        h2 = details.find("h2")
        title = _clean(h2.get_text(" ", strip=True)) if h2 else ""
        if not title:
            return None

        fields = {}
        for tr in details.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not th or not td:
                continue
            key = _clean(th.get_text(" ", strip=True))
            val = _clean(td.get_text(" ", strip=True))
            if key and key.lower() not in ("label", "content"):
                fields[key] = val

        external_url = ""
        for link in details.find_all("a", href=True):
            href = urljoin(self.base_url, link.get("href"))
            if href.startswith("http") and "hfml-felix.nl" not in href:
                external_url = href
                break

        year_raw = fields.get("Year") or fields.get("Publication Year") or ""
        year_match = re.search(r"\b(19|20)\d{2}\b", year_raw)
        year = year_match.group(0) if year_match else ""
        journal_raw = fields.get("Journal") or ""
        doi = self._extract_doi(external_url) or self._extract_doi(fields.get("DOI") or "")
        post_number = self._post_number_from_record(doi, external_url, title)

        return {
            "title": title,
            "authors": self._format_authors(fields.get("Author(s)") or fields.get("Authors") or fields.get("Author") or ""),
            "year": year,
            "listed_date_raw": year_raw,
            "listed_date": f"{year}-01-01" if year else None,
            "journal": self._journal_name(journal_raw),
            "journal_raw": journal_raw,
            "category": fields.get("Research group") or fields.get("Category") or "",
            "url": external_url,
            "doi": doi,
            "post_number": post_number,
            "fields": fields,
        }

    # ------------------------------------------------------------------
    # Detail enrichment
    # ------------------------------------------------------------------

    def _fetch_detail(self, pub):
        detail = {
            "abstract": "",
            "authors": pub.get("authors") or "",
            "published_date": None,
            "publisher": "",
            "department": "",
            "journal": pub.get("journal") or "",
            "journal_raw": pub.get("journal_raw") or "",
            "keywords": "",
            "pdf_url": None,
            "original_filename": None,
            "series": None,
            "volume": None,
            "issue": None,
            "doi": pub.get("doi"),
            "detail_raw": {},
        }

        url = pub.get("url") or ""
        if url:
            raw = self._curl(url, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
            if raw:
                html_detail = self._parse_publisher_html(raw)
                self._merge_detail(detail, html_detail)

        doi = detail.get("doi") or pub.get("doi")
        if doi:
            crossref = self._fetch_crossref(doi)
            self._merge_detail(detail, crossref, fill_only=True)
            s2 = self._fetch_semantic_scholar(doi)
            self._merge_detail(detail, s2, fill_only=True)

        if detail.get("pdf_url") and not detail.get("original_filename"):
            detail["original_filename"] = (
                self._filename_from_url(detail.get("pdf_url"))
                or self._filename_from_content_disposition(detail.get("pdf_url"))
            )

        detail["abstract"] = _clean(detail.get("abstract"))
        return detail

    def _parse_publisher_html(self, raw):
        soup = _make_soup(raw)
        if soup is None:
            return {}

        meta = {}
        for tag in soup.find_all("meta"):
            key = tag.get("name") or tag.get("property")
            content = tag.get("content")
            if key and content:
                meta.setdefault(key, [])
                meta[key].append(_clean(content))

        def first(*keys):
            for key in keys:
                values = meta.get(key)
                if values:
                    return values[0]
            return ""

        authors = meta.get("citation_author") or meta.get("dc.creator") or []
        keywords = meta.get("citation_keywords") or meta.get("dc.subject") or []
        json_ld = self._extract_json_ld(soup)

        abstract = (
            first("citation_abstract", "dc.description", "description", "og:description", "twitter:description")
            or json_ld.get("description")
        )
        doi = self._normalize_doi(first("citation_doi", "DOI", "prism.doi", "dc.identifier")) or json_ld.get("doi")
        published_date = self._normalize_date(
            first("citation_publication_date", "citation_online_date", "prism.publicationDate", "dc.date")
            or json_ld.get("datePublished")
        )
        journal = first("citation_journal_title", "prism.publicationName") or json_ld.get("journal")
        publisher = first("citation_publisher", "dc.publisher") or json_ld.get("publisher")
        pdf_url = first("citation_pdf_url")

        return {
            "abstract": abstract,
            "authors": self._format_authors(authors),
            "published_date": published_date,
            "publisher": publisher,
            "journal": journal,
            "keywords": ", ".join([_clean(k) for k in keywords if _clean(k)]),
            "pdf_url": pdf_url,
            "original_filename": self._filename_from_url(pdf_url),
            "doi": doi,
            "volume": first("citation_volume", "prism.volume"),
            "issue": first("citation_issue", "prism.number"),
            "detail_raw": {"publisher_meta": {k: v[:10] for k, v in meta.items()}},
        }

    def _extract_json_ld(self, soup):
        found = {}
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            text = script.string or script.get_text()
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                item = stack.pop(0)
                if not isinstance(item, dict):
                    continue
                main = item.get("mainEntity")
                if isinstance(main, dict):
                    stack.append(main)
                if item.get("description") and not found.get("description"):
                    found["description"] = _clean(item.get("description"))
                if item.get("datePublished") and not found.get("datePublished"):
                    found["datePublished"] = item.get("datePublished")
                if item.get("sameAs") and not found.get("doi"):
                    found["doi"] = self._extract_doi(item.get("sameAs"))
                if item.get("publisher") and not found.get("publisher"):
                    pub = item.get("publisher")
                    found["publisher"] = _clean(pub.get("name")) if isinstance(pub, dict) else _clean(pub)
                if item.get("isPartOf") and not found.get("journal"):
                    part = item.get("isPartOf")
                    found["journal"] = _clean(part.get("name")) if isinstance(part, dict) else _clean(part)
        return found

    def _fetch_crossref(self, doi):
        raw = self._curl(
            self._CROSSREF_API.format(doi=doi),
            timeout=35,
            headers={"Accept": "application/json"},
        )
        if not raw:
            return {}
        try:
            message = json.loads(raw).get("message") or {}
        except Exception:
            return {}

        abstract = _clean(re.sub(r"<[^>]+>", " ", message.get("abstract") or ""))
        authors = []
        for author in message.get("author") or []:
            given = author.get("given") or ""
            family = author.get("family") or ""
            name = _clean(f"{given} {family}") or _clean(author.get("name"))
            if name:
                authors.append(name)

        link_pdf = None
        for link in message.get("link") or []:
            candidate = link.get("URL")
            if candidate and ("pdf" in (link.get("content-type") or "").lower() or candidate.lower().endswith(".pdf")):
                link_pdf = candidate
                break

        published_date = self._date_from_crossref(message)
        journal = ""
        containers = message.get("container-title") or []
        if containers:
            journal = _clean(containers[0])

        return {
            "abstract": abstract,
            "authors": "; ".join(authors),
            "published_date": published_date,
            "publisher": _clean(message.get("publisher")),
            "journal": journal,
            "pdf_url": link_pdf,
            "doi": self._normalize_doi(message.get("DOI")),
            "volume": _clean(message.get("volume")),
            "issue": _clean(message.get("issue")),
            "detail_raw": {"crossref": message},
        }

    def _fetch_semantic_scholar(self, doi):
        raw = self._curl(
            self._S2_API.format(doi=doi),
            timeout=35,
            headers={"Accept": "application/json"},
        )
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if data.get("error") or data.get("message"):
            return {"detail_raw": {"semantic_scholar": data}}

        authors = []
        for author in data.get("authors") or []:
            name = _clean(author.get("name")) if isinstance(author, dict) else _clean(author)
            if name:
                authors.append(name)
        pdf_url = None
        open_pdf = data.get("openAccessPdf")
        if isinstance(open_pdf, dict):
            pdf_url = open_pdf.get("url") or None

        return {
            "abstract": data.get("abstract") or "",
            "authors": "; ".join(authors),
            "published_date": self._normalize_date(data.get("publicationDate") or str(data.get("year") or "")),
            "pdf_url": pdf_url,
            "detail_raw": {"semantic_scholar": data},
        }

    def _merge_detail(self, target, source, *, fill_only=False):
        if not source:
            return
        for key, value in source.items():
            if key == "detail_raw":
                raw = target.setdefault("detail_raw", {})
                if isinstance(value, dict):
                    raw.update(value)
                continue
            if value in (None, "", [], {}):
                continue
            if fill_only and target.get(key) not in (None, "", [], {}):
                continue
            target[key] = value

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _extract_doi(self, value):
        if not value:
            return None
        value = unquote(str(value))
        match = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", value, re.I)
        if match:
            return self._normalize_doi(match.group(1))
        nature = re.search(r"nature\.com/articles/([a-z0-9-]+)", value, re.I)
        if nature:
            return self._normalize_doi(f"10.1038/{nature.group(1)}")
        return None

    def _normalize_doi(self, doi):
        if not doi:
            return None
        doi = str(doi).strip()
        doi = re.sub(r"^(doi:|https?://(dx\.)?doi\.org/)", "", doi, flags=re.I)
        doi = doi.rstrip(" .;,)\"'")
        return doi or None

    def _normalize_date(self, value):
        value = _clean(value)
        if not value:
            return None
        match = re.match(r"(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?", value)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day or 1):02d}"
        match = re.match(r"(\d{4})$", value)
        if match:
            return f"{match.group(1)}-01-01"
        return value[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value[:10]) else None

    def _date_from_crossref(self, message):
        for key in ("published-print", "published-online", "published", "issued", "created"):
            parts = ((message.get(key) or {}).get("date-parts") or [[]])[0]
            if parts:
                year = int(parts[0])
                month = int(parts[1]) if len(parts) > 1 else 1
                day = int(parts[2]) if len(parts) > 2 else 1
                return f"{year:04d}-{month:02d}-{day:02d}"
        return None

    def _journal_name(self, journal_raw):
        journal_raw = _clean(journal_raw)
        if not journal_raw:
            return ""
        return re.sub(r"\s*,?\s*(Volume|Volumes|Vol\.?|Issue)\s+.*$", "", journal_raw, flags=re.I).strip(" ,")

    def _format_authors(self, value):
        if not value:
            return ""
        if isinstance(value, list):
            parts = [_clean(v) for v in value]
        else:
            text = _clean(value)
            text = re.sub(r"\s+and\s+", ", ", text, flags=re.I)
            text = text.replace(" & ", ", ")
            parts = [_clean(p) for p in re.split(r"\s*;\s*|\s*,\s*", text)]
        parts = [p for p in parts if p]
        return "; ".join(parts)

    def _post_number_from_record(self, doi, url, title):
        if doi:
            return doi
        if url:
            path = urlparse(url).path.rstrip("/")
            tail = path.split("/")[-1] if path else ""
            if tail:
                return unquote(tail)
            return url
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return slug or None

    def _filename_from_url(self, url):
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
        if tail and "." in tail and len(tail) <= 220:
            return tail
        return None

    def _filename_from_content_disposition(self, url):
        if not url:
            return None
        headers = self._curl_head(url)
        match = re.search(r'Content-Disposition:.*?filename\*?=(?:UTF-8\'\')?"?([^"\r\n;]+)', headers, re.I)
        if match:
            return unquote(match.group(1).strip())
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"
        last_total_pages = None

        for page in range(1, self._PAGE_CAP + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time >= self._WALL_CLOCK_SECS:
                print(f"[{self.site_id}] wall-clock budget reached; stopping cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            raw = self._fetch_list_page(page)
            if not raw:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break

            publications, pager, payload = self._parse_page_response(raw, page)
            if not publications:
                print(f"[{self.site_id}] page {page}: no records; stopping")
                break

            if pager.get("total_pages"):
                try:
                    last_total_pages = int(pager["total_pages"])
                except Exception:
                    last_total_pages = None

            new_publications = []
            for pub in publications:
                key = pub.get("url") or pub.get("doi") or pub.get("post_number") or pub.get("title")
                if not key:
                    continue
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                new_publications.append(pub)

            if not new_publications:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                break

            for idx, pub in enumerate(new_publications, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= self._WALL_CLOCK_SECS:
                    print(f"[{self.site_id}] wall-clock budget reached during item loop; stopping cleanly")
                    return saved

                try:
                    if self.detail_delay:
                        time.sleep(self.detail_delay)
                    detail = self._fetch_detail(pub)
                    abstract = _clean(detail.get("abstract"))
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {page}-{idx} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    doi = detail.get("doi") or pub.get("doi")
                    external_id = doi or pub.get("url") or pub.get("post_number")
                    listed_date = pub.get("listed_date")
                    published_date = detail.get("published_date") or listed_date
                    metadata = self._metadata(pub, detail, payload, page, listed_date)

                    paper = {
                        "id": f"{self.site_id}:{external_id}",
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": pub.get("post_number"),
                        "title": pub.get("title"),
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": detail.get("authors") or pub.get("authors"),
                        "publisher": detail.get("publisher") or "HFML-FELIX",
                        "department": detail.get("department") or None,
                        "journal": detail.get("journal") or pub.get("journal"),
                        "url": pub.get("url") or self._PUBLICATIONS_URL,
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords") or None,
                        "category": pub.get("category") or None,
                        "doi": doi,
                        "original_filename": detail.get("original_filename"),
                        "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {pub.get('title')[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page}-{idx} failed: {exc}")
                    continue

            if page == self._PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {self._PAGE_CAP} pages reached")
                break
            if last_total_pages is not None and page >= last_total_pages:
                break
            if pager and not pager.get("has_next") and page > 1:
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def _metadata(self, pub, detail, payload, page, listed_date):
        metadata = {
            "posted_date": pub.get("listed_date_raw") or listed_date,
            "originalFilename": detail.get("original_filename"),
            "journal_raw": pub.get("journal_raw") or detail.get("journal_raw"),
            "series": detail.get("series"),
            "volume": detail.get("volume"),
            "issue": detail.get("issue"),
            "doi": detail.get("doi") or pub.get("doi"),
            "external_url": pub.get("url"),
            "post_number": pub.get("post_number"),
            "facetwp_page": page,
            "facetwp_pager": (payload.get("settings") or {}).get("pager") if isinstance(payload, dict) else None,
            "list_fields": pub.get("fields"),
        }
        raw = detail.get("detail_raw")
        if raw:
            metadata["detail_raw"] = raw
        return {k: v for k, v in metadata.items() if v not in (None, "", [], {})}
