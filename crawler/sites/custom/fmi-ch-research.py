# -*- coding: utf-8 -*-
"""Crawler for FMI scientific publications."""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class FMIChResearchCrawler(BaseCrawler):
    site_id = "fmi-ch-research"
    site_name = "Custom: fmi-ch-research"
    base_url = "https://www.fmi.ch"

    START_URL = "https://www.fmi.ch/research/publications/"
    LIST_ENDPOINT = START_URL
    PUBMED_ESEARCH_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    PUBMED_EFETCH_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    CROSSREF_WORKS_ENDPOINT = "https://api.crossref.org/works"

    MAX_PAGES = 200
    MAX_WALL_SECONDS = 25 * 60
    WALL_MARGIN_SECONDS = 60
    MIN_ABSTRACT_CHARS = 50
    CURL_TIMEOUT = 35
    BACKOFF_SECONDS = (1, 3, 9)
    _CURL_META_MARKER = "__FMI_CH_RESEARCH_CURL_META__:"
    _DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
    _YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\b")

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl FMI's year-filtered publication list and article details."""
        if limit is not None and limit <= 0:
            return 0

        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        saved = 0
        page = 1
        seen_urls = set()

        landing_html, landing_url = self._curl_get(
            self.START_URL,
            context="landing page",
            referer=self.base_url,
        )
        start_year = self._discover_start_year(landing_html)
        print(
            f"[{self.site_id}] using HTML POST list endpoint {self.LIST_ENDPOINT} "
            f"with pubYear=<year>; starting at {start_year}"
        )

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if self._near_deadline(start_time):
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                break

            year = start_year - (page - 1)
            raw, effective_url = self._fetch_year_page(year)
            if not raw:
                print(f"[{self.site_id}] page {page}: empty list response for year {year}; stopping")
                break

            records = self._parse_list(raw, effective_url or self.LIST_ENDPOINT, page, year)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records for year {year}; stopping")
                break

            new_on_page = 0
            for item_no, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._near_deadline(start_time):
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                    return saved

                item_label = f"page {page} item {item_no}"
                dedupe_url = self._dedupe_url(record.get("url") or "")
                if not dedupe_url:
                    print(f"[{self.site_id}] item {item_label} skipped: missing URL")
                    continue
                if dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                new_on_page += 1

                try:
                    time.sleep(self.detail_delay)
                    paper = self._build_paper(record, item_label)
                    if not paper:
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
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

            if page % 10 == 0:
                p = page
                print(f"[fmi-ch-research] page {p}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            page += 1
        else:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List discovery and parsing
    # ------------------------------------------------------------------

    def _discover_start_year(self, raw):
        years = []
        soup = self._soup(raw, context="landing page") if raw else None
        if soup is not None:
            for option in soup.select("select#pubYear option[value]"):
                value = (option.get("value") or "").strip()
                if self._YEAR_RE.fullmatch(value):
                    years.append(int(value))
            for row in self._parse_list(raw, self.START_URL, page=0, year=None):
                if row.get("published_date"):
                    years.append(int(row["published_date"][:4]))
        if years:
            return max(years)
        return _dt.datetime.utcnow().year

    def _fetch_year_page(self, year):
        return self._curl_get(
            self.LIST_ENDPOINT,
            context=f"list page for year {year}",
            referer=self.START_URL,
            data={
                "pubYear": str(year),
                "SubmitButton": "Search",
            },
        )

    def _parse_list(self, raw, list_url, page, year):
        soup = self._soup(raw, context=f"list page {page}")
        if soup is None:
            return []

        records = []
        for row in soup.select("table.table-striped tbody tr"):
            group_cell = row.select_one("td.w-20")
            authors_cell = row.select_one("td.w-45")
            title_cell = row.select_one("td.w-35")
            link = title_cell.select_one("a[href]") if title_cell else row.select_one("a[href]")
            if not group_cell or not authors_cell or not title_cell or not link:
                continue

            title = self._clean_text(link.get_text(" "))
            href = (link.get("href") or "").strip()
            if not title or not href:
                continue

            date_text, groups = self._parse_group_date_cell(group_cell)
            published_date = self._parse_date(date_text)
            authors_text = self._clean_text(authors_cell.get_text(" "))
            detail_url = urljoin(list_url, href)
            doi = self._extract_doi(detail_url)
            citation = self._extract_citation(title_cell, title)

            records.append({
                "title": title,
                "authors": self._split_authors(authors_text),
                "authors_text": authors_text,
                "category": groups,
                "published_date": published_date,
                "listed_date": published_date,
                "posted_date_raw": date_text,
                "date_text": date_text,
                "url": detail_url,
                "doi": doi,
                "citation": citation,
                "pdf_url": detail_url if self._looks_like_pdf(detail_url) else "",
                "list_url": list_url,
                "list_page": page,
                "list_year": year,
            })
        return records

    def _parse_group_date_cell(self, cell):
        lines = [self._clean_text(line) for line in cell.get_text("\n").splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            return "", ""
        date_text = lines[-1]
        groups = "; ".join(lines[:-1])
        return date_text, groups

    def _extract_citation(self, title_cell, title):
        lines = [self._clean_text(line) for line in title_cell.get_text("\n").splitlines()]
        lines = [line for line in lines if line]
        filtered = []
        title_key = self._clean_text(title).rstrip(".")
        for line in lines:
            if self._clean_text(line).rstrip(".") == title_key:
                continue
            filtered.append(line)
        return filtered[-1] if filtered else ""

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _build_paper(self, record, item_label):
        detail = self._fetch_pubmed_detail(record, item_label)
        if not detail or len(detail.get("abstract") or "") < self.MIN_ABSTRACT_CHARS:
            crossref = self._fetch_crossref_detail(record, item_label)
            if crossref and len(crossref.get("abstract") or "") > len(detail.get("abstract") or ""):
                detail.update(crossref)
        if not detail or len(detail.get("abstract") or "") < self.MIN_ABSTRACT_CHARS:
            publisher = self._fetch_publisher_html_detail(record, item_label)
            if publisher and len(publisher.get("abstract") or "") > len(detail.get("abstract") or ""):
                detail.update(publisher)

        title = self._clean_text(detail.get("title") or record.get("title") or "")
        abstract = self._clean_text(detail.get("abstract") or "")
        if not title:
            print(f"[{self.site_id}] item {item_label} skipped: missing title")
            return None

        # Prefer the DOI extracted straight from FMI's own listing page — it is
        # authoritative. ``detail["doi"]`` (from PubMed/CrossRef/publisher-HTML
        # fallbacks) is only a backstop for entries FMI didn't link with a DOI.
        doi = self._clean_doi(record.get("doi") or detail.get("doi") or "")
        url = record.get("url") or (f"https://doi.org/{doi}" if doi else self.START_URL)
        external_id = doi or detail.get("pubmed_id") or self._hash_id(url)
        authors = detail.get("authors") or record.get("authors") or []
        keywords = detail.get("keywords") or []
        published_date = record.get("published_date") or detail.get("published_date") or ""
        listed_date = record.get("listed_date") or record.get("published_date") or ""
        journal = detail.get("journal") or self._journal_from_citation(record.get("citation") or "")
        pdf_url = detail.get("pdf_url") or record.get("pdf_url") or ""
        citation_parts = self._citation_parts(record.get("citation") or "")
        original_filename = self._filename_from_url(pdf_url)
        post_number = doi or detail.get("pubmed_id") or self._slug_from_url(url)

        metadata = {
            "source": "FMI publications HTML year list plus article detail metadata",
            "list_endpoint": self.LIST_ENDPOINT,
            "list_method": "POST",
            "list_form_fields": {"pubYear": record.get("list_year"), "SubmitButton": "Search"},
            "list_page": record.get("list_page"),
            "list_year": record.get("list_year"),
            "list_url": record.get("list_url"),
            "detail_endpoint": detail.get("detail_endpoint"),
            "detail_source": detail.get("source"),
            "detail_effective_url": detail.get("effective_url"),
            "pubmed_esearch_endpoint": self.PUBMED_ESEARCH_ENDPOINT,
            "pubmed_efetch_endpoint": self.PUBMED_EFETCH_ENDPOINT,
            "crossref_endpoint": self.CROSSREF_WORKS_ENDPOINT,
            "pubmed_id": detail.get("pubmed_id"),
            "doi": doi,
            "post_number": post_number,
            "journal": journal,
            "journal_raw": detail.get("journal_raw") or record.get("citation"),
            "series": detail.get("series") or citation_parts.get("series"),
            "volume": detail.get("volume") or citation_parts.get("volume"),
            "issue": detail.get("issue") or citation_parts.get("issue"),
            "citation": record.get("citation"),
            "record": record,
            "date_text": record.get("date_text"),
            "posted_date": record.get("posted_date_raw") or record.get("date_text"),
            "listed_date": listed_date,
            "authors_from_list": record.get("authors_text"),
            "publisher": "Friedrich Miescher Institute for Biomedical Research",
            "department": record.get("category") or "",
            "originalFilename": original_filename,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "authors": "; ".join(self._dedupe_keep_order(authors)) if authors else None,
            "abstract": abstract,
            "category": record.get("category") or "",
            "keywords": ", ".join(self._dedupe_keep_order(keywords)) if keywords else None,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "url": url,
            "pdf_url": pdf_url or None,
            "doi": doi,
            "publisher": "Friedrich Miescher Institute for Biomedical Research",
            "department": "Friedrich Miescher Institute for Biomedical Research",
            "journal": journal,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _fetch_pubmed_detail(self, record, item_label):
        pmid = self._pubmed_id_for_record(record, item_label)
        if not pmid:
            return {}

        url = (
            f"{self.PUBMED_EFETCH_ENDPOINT}"
            f"?db=pubmed&retmode=xml&id={quote(str(pmid))}"
        )
        raw, effective_url = self._curl_get(
            url,
            context=f"item {item_label} PubMed efetch",
            accept="application/xml,text/xml,*/*",
            referer=self.START_URL,
        )
        if not raw:
            return {}

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            print(f"[{self.site_id}] item {item_label} PubMed XML parse failed: {exc}")
            return {}

        article = self._first_desc(root, "Article")
        pubmed_data = self._first_desc(root, "PubmedData")
        medline = self._first_desc(root, "MedlineCitation")
        ids = self._pubmed_article_ids(pubmed_data or root)
        if not ids.get("pubmed"):
            pmid_el = self._first_desc(medline or root, "PMID")
            ids["pubmed"] = self._text(pmid_el) or str(pmid)

        journal_el = self._first_desc(article, "Journal") if article is not None else None
        title = self._text(self._first_desc(article, "ArticleTitle")) if article is not None else ""
        abstract = self._pubmed_abstract(article)
        authors = self._pubmed_authors(article)
        keywords = self._texts(root, "Keyword")
        journal = (
            self._text(self._first_desc(journal_el, "ISOAbbreviation"))
            or self._text(self._first_desc(journal_el, "Title"))
        ) if journal_el is not None else ""
        volume, issue = self._pubmed_volume_issue(journal_el)
        published_date = self._pubmed_article_date(article) or self._pubmed_journal_date(article)
        doi = ids.get("doi") or record.get("doi") or ""

        return {
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "keywords": keywords,
            "journal": journal,
            "journal_raw": journal,
            "volume": volume,
            "issue": issue,
            "published_date": published_date,
            "doi": doi,
            "pubmed_id": ids.get("pubmed") or str(pmid),
            "detail_endpoint": self.PUBMED_EFETCH_ENDPOINT,
            "effective_url": effective_url,
            "source": "pubmed-eutils",
        }

    def _pubmed_id_for_record(self, record, item_label):
        doi = record.get("doi") or ""
        if doi:
            term = f"{doi}[AID]"
        else:
            title = record.get("title") or ""
            if not title:
                return ""
            term = f"{title}[Title]"

        url = (
            f"{self.PUBMED_ESEARCH_ENDPOINT}"
            f"?db=pubmed&retmode=json&retmax=1&term={quote(term)}"
        )
        raw, _effective_url = self._curl_get(
            url,
            context=f"item {item_label} PubMed esearch",
            accept="application/json,*/*",
            referer=self.START_URL,
        )
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] item {item_label} PubMed JSON parse failed: {exc}")
            return ""
        ids = ((data.get("esearchresult") or {}).get("idlist") or [])
        return str(ids[0]) if ids else ""

    def _fetch_crossref_detail(self, record, item_label):
        doi = record.get("doi") or ""
        if not doi:
            return {}
        url = f"{self.CROSSREF_WORKS_ENDPOINT}/{quote(doi, safe='')}"
        raw, effective_url = self._curl_get(
            url,
            context=f"item {item_label} Crossref",
            accept="application/json,*/*",
            referer=self.START_URL,
        )
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] item {item_label} Crossref JSON parse failed: {exc}")
            return {}

        msg = data.get("message") or {}
        abstract = self._clean_html_fragment(msg.get("abstract") or "")
        authors = []
        for author in msg.get("author") or []:
            name = self._clean_text(
                " ".join(part for part in (author.get("given"), author.get("family")) if part)
            )
            if name:
                authors.append(name)
        date_parts = (
            ((msg.get("published-print") or {}).get("date-parts") or [])
            or ((msg.get("published-online") or {}).get("date-parts") or [])
            or ((msg.get("published") or {}).get("date-parts") or [])
            or ((msg.get("issued") or {}).get("date-parts") or [])
        )
        published_date = self._date_from_parts(date_parts[0]) if date_parts else ""
        links = msg.get("link") or []
        pdf_url = ""
        for link in links:
            candidate = link.get("URL") or ""
            if self._looks_like_pdf(candidate):
                pdf_url = candidate
                break

        return {
            "title": self._first_list_value(msg.get("title")),
            "abstract": abstract,
            "authors": authors,
            "keywords": msg.get("subject") or [],
            "journal": self._first_list_value(msg.get("container-title")),
            "journal_raw": self._first_list_value(msg.get("container-title")),
            "series": self._first_list_value(msg.get("short-container-title")),
            "volume": msg.get("volume") or "",
            "issue": msg.get("issue") or "",
            "published_date": published_date,
            "doi": msg.get("DOI") or doi,
            "pdf_url": pdf_url,
            "detail_endpoint": self.CROSSREF_WORKS_ENDPOINT,
            "effective_url": effective_url,
            "source": "crossref-api",
        }

    def _fetch_publisher_html_detail(self, record, item_label):
        url = record.get("url") or ""
        if not url:
            return {}
        raw, effective_url = self._curl_get(
            url,
            context=f"item {item_label} publisher detail",
            referer=self.START_URL,
        )
        if not raw:
            return {}
        soup = self._soup(raw, context=f"item {item_label} publisher detail")
        if soup is None:
            return {}

        title = (
            self._meta_content(soup, "citation_title")
            or self._meta_content(soup, "dc.Title")
            or self._meta_content(soup, "og:title")
        )
        abstract = self._extract_html_abstract(soup)
        authors = [
            self._clean_text(tag.get("content") or "")
            for tag in soup.select("meta[name='citation_author']")
        ]
        authors = [a for a in authors if a]
        keywords = self._meta_keywords(soup)
        journal = (
            self._meta_content(soup, "citation_journal_title")
            or self._meta_content(soup, "citation_conference_title")
            or self._meta_content(soup, "citation_publisher")
        )
        published_date = (
            self._meta_content(soup, "citation_publication_date")
            or self._meta_content(soup, "citation_online_date")
            or self._meta_content(soup, "article:published_time")
        )
        doi = self._meta_content(soup, "citation_doi") or self._extract_doi(effective_url or url)
        pdf_url = self._meta_content(soup, "citation_pdf_url")
        if pdf_url:
            pdf_url = urljoin(effective_url or url, pdf_url)

        return {
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "keywords": keywords,
            "journal": journal,
            "journal_raw": journal,
            "published_date": self._normalize_date_value(published_date),
            "doi": self._clean_doi(doi),
            "pdf_url": pdf_url,
            "detail_endpoint": effective_url or url,
            "effective_url": effective_url,
            "source": "publisher-html",
        }

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", accept=None, referer=None, data=None):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'}",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-w", "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if data is not None:
            cmd.extend(["-X", "POST"])
            for key, value in data.items():
                cmd.extend(["--data-urlencode", f"{key}={value}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                    check=False,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                body, http_code, effective_url = self._split_curl_output(stdout, url)
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except KeyboardInterrupt:
                raise
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
        return "", url

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
    # Generic parsing helpers
    # ------------------------------------------------------------------

    def _soup(self, raw, context="html"):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _pubmed_abstract(self, article):
        if article is None:
            return ""
        parts = []
        for elem in article.iter():
            if self._local_name(elem.tag) != "AbstractText":
                continue
            text = self._clean_text(" ".join(elem.itertext()))
            if not text:
                continue
            label = self._clean_text(elem.attrib.get("Label") or "")
            if label and not text.lower().startswith(label.lower()):
                text = f"{label}: {text}"
            parts.append(text)
        return self._clean_text(" ".join(parts))

    def _pubmed_authors(self, article):
        authors = []
        if article is None:
            return authors
        author_list = self._first_desc(article, "AuthorList")
        if author_list is None:
            return authors
        for author in author_list:
            if self._local_name(author.tag) != "Author":
                continue
            collective = self._text(self._first_desc(author, "CollectiveName"))
            if collective:
                authors.append(collective)
                continue
            last = self._text(self._first_desc(author, "LastName"))
            fore = self._text(self._first_desc(author, "ForeName"))
            initials = self._text(self._first_desc(author, "Initials"))
            name = self._clean_text(" ".join(part for part in (fore or initials, last) if part))
            if name:
                authors.append(name)
        return authors

    def _pubmed_article_ids(self, root):
        """Extract the article's own IDs from PubmedData/ArticleIdList.

        Must NOT use ``root.iter()`` over the whole subtree: PubmedData also
        contains ReferenceList/Reference/ArticleIdList entries for every
        *cited* reference, each of which can carry its own ``IdType="doi"``
        ArticleId. Walking unrestricted would let a cited reference's DOI
        silently overwrite the article's own DOI.
        """
        ids = {}
        if root is None:
            return ids
        article_id_list = None
        for child in list(root):
            if self._local_name(child.tag) == "ArticleIdList":
                article_id_list = child
                break
        if article_id_list is None:
            return ids
        for elem in article_id_list:
            if self._local_name(elem.tag) != "ArticleId":
                continue
            id_type = (elem.attrib.get("IdType") or "").lower()
            value = self._clean_text(" ".join(elem.itertext()))
            if id_type and value:
                ids[id_type] = value
        return ids

    def _pubmed_article_date(self, article):
        if article is None:
            return ""
        for elem in article.iter():
            if self._local_name(elem.tag) == "ArticleDate":
                value = self._date_from_xml_date(elem)
                if value:
                    return value
        return ""

    def _pubmed_journal_date(self, article):
        if article is None:
            return ""
        journal = self._first_desc(article, "Journal")
        pubdate = self._first_desc(journal, "PubDate") if journal is not None else None
        return self._date_from_xml_date(pubdate)

    def _pubmed_volume_issue(self, journal):
        if journal is None:
            return "", ""
        issue_el = self._first_desc(journal, "JournalIssue")
        if issue_el is None:
            return "", ""
        volume = self._text(self._first_desc(issue_el, "Volume"))
        issue = self._text(self._first_desc(issue_el, "Issue"))
        return volume, issue

    def _date_from_xml_date(self, elem):
        if elem is None:
            return ""
        year = self._text(self._first_desc(elem, "Year"))
        month = self._text(self._first_desc(elem, "Month"))
        day = self._text(self._first_desc(elem, "Day")) or "01"
        if not year:
            medline = self._text(self._first_desc(elem, "MedlineDate"))
            match = self._YEAR_RE.search(medline or "")
            return match.group(1) if match else ""
        month_num = self._month_number(month) or "01"
        try:
            return f"{int(year):04d}-{int(month_num):02d}-{int(day):02d}"
        except (TypeError, ValueError):
            return f"{year}-{month_num}-01"

    def _extract_html_abstract(self, soup):
        for name in (
            "citation_abstract",
            "dc.Description",
            "description",
            "og:description",
            "twitter:description",
        ):
            value = self._meta_content(soup, name)
            if value:
                return value
        for selector in (
            "#Abs1-content",
            "section[aria-labelledby^='Abs']",
            "section[data-title='Abstract']",
            ".abstract",
            "[class*='abstract']",
            "[id*='abstract']",
        ):
            tag = soup.select_one(selector)
            if not tag:
                continue
            text = self._clean_text(tag.get_text(" "))
            text = re.sub(r"^abstract\s*", "", text, flags=re.I).strip()
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                return text
        return ""

    def _meta_content(self, soup, name):
        selectors = [
            f"meta[name='{name}']",
            f"meta[property='{name}']",
            f"meta[name='{name.lower()}']",
            f"meta[property='{name.lower()}']",
        ]
        for selector in selectors:
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                return self._clean_text(tag.get("content"))
        return ""

    def _meta_keywords(self, soup):
        values = []
        for name in ("citation_keywords", "keywords", "dc.Subject"):
            text = self._meta_content(soup, name)
            if text:
                values.extend([p.strip() for p in re.split(r"[;,]", text) if p.strip()])
        return self._dedupe_keep_order(values)

    @staticmethod
    def _first_desc(root, name):
        if root is None:
            return None
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1] if isinstance(elem.tag, str) else elem.tag
            if tag == name:
                return elem
        return None

    @classmethod
    def _texts(cls, root, name):
        values = []
        if root is None:
            return values
        for elem in root.iter():
            if cls._local_name(elem.tag) == name:
                text = cls._clean_text(" ".join(elem.itertext()))
                if text:
                    values.append(text)
        return cls._dedupe_keep_order(values)

    @classmethod
    def _text(cls, elem):
        if elem is None:
            return ""
        return cls._clean_text(" ".join(elem.itertext()))

    @staticmethod
    def _local_name(tag):
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag

    @classmethod
    def _clean_html_fragment(cls, value):
        if not value:
            return ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                soup = BeautifulSoup(str(value), parser)
                return cls._clean_text(soup.get_text(" "))
            except Exception as exc:
                last_exc = exc
                print(f"[{cls.site_id}] BeautifulSoup({parser}) failed for HTML fragment: {exc}")
        print(f"[{cls.site_id}] all BeautifulSoup parsers failed for HTML fragment: {last_exc}")
        return ""

    @classmethod
    def _clean_text(cls, value):
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = text.replace("\u00a0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _split_authors(cls, text):
        cleaned = cls._clean_text(text)
        if not cleaned:
            return []
        if ";" in cleaned:
            parts = cleaned.split(";")
        else:
            parts = cleaned.split(",")
        authors = [cls._clean_text(part) for part in parts]
        return [author for author in authors if author]

    @classmethod
    def _extract_doi(cls, value):
        text = unquote(value or "")
        match = cls._DOI_RE.search(text)
        if not match:
            return ""
        return cls._clean_doi(match.group(1))

    @classmethod
    def _clean_doi(cls, value):
        doi = cls._clean_text(value)
        if not doi:
            return ""
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
        doi = doi.split("?")[0].split("#")[0]
        return doi.rstrip(".,;)")

    @staticmethod
    def _looks_like_pdf(url):
        return bool(re.search(r"\.pdf(?:[?#].*)?$", urlparse(url or "").path, re.I))

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
        if tail and "." in tail and len(tail) <= 240:
            return tail
        return None

    @staticmethod
    def _slug_from_url(url):
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
        return tail or None

    @classmethod
    def _parse_date(cls, text):
        value = cls._clean_text(text)
        if not value:
            return ""
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return _dt.datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        match = re.search(r"([A-Za-z]{3,12})\s+(\d{1,2}),?\s+(\d{4})", value)
        if match:
            return cls._parse_date(f"{match.group(1)} {match.group(2)}, {match.group(3)}")
        return ""

    @classmethod
    def _normalize_date_value(cls, value):
        text = cls._clean_text(value)
        if not text:
            return ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return f"{text}-01"
        if re.fullmatch(r"\d{4}", text):
            return text
        return cls._parse_date(text)

    @staticmethod
    def _date_from_parts(parts):
        if not parts:
            return ""
        try:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return f"{year:04d}-{month:02d}-{day:02d}"
        except (TypeError, ValueError):
            return ""

    @classmethod
    def _month_number(cls, value):
        text = cls._clean_text(value).lower()
        if not text:
            return ""
        if text.isdigit():
            return f"{int(text):02d}"
        months = {
            "jan": "01", "january": "01",
            "feb": "02", "february": "02",
            "mar": "03", "march": "03",
            "apr": "04", "april": "04",
            "may": "05",
            "jun": "06", "june": "06",
            "jul": "07", "july": "07",
            "aug": "08", "august": "08",
            "sep": "09", "sept": "09", "september": "09",
            "oct": "10", "october": "10",
            "nov": "11", "november": "11",
            "dec": "12", "december": "12",
        }
        return months.get(text[:3], "")

    @staticmethod
    def _first_list_value(value):
        if isinstance(value, list):
            return value[0] if value else ""
        return value or ""

    @classmethod
    def _journal_from_citation(cls, citation):
        text = cls._clean_text(citation)
        if not text:
            return ""
        if "." in text:
            return text.split(".", 1)[0].strip()
        if "," in text:
            return text.split(",", 1)[0].strip()
        return text

    @classmethod
    def _citation_parts(cls, citation):
        text = cls._clean_text(citation)
        parts = {"series": "", "volume": "", "issue": ""}
        if not text:
            return parts
        match = re.search(r"\b(?:19|20)\d{2}\b[^;]*;([^:.,\s]+)(?:\(([^)]+)\))?", text)
        if match:
            parts["volume"] = match.group(1) or ""
            parts["issue"] = match.group(2) or ""
        return parts

    @staticmethod
    def _dedupe_keep_order(values):
        seen = set()
        result = []
        for value in values:
            key = str(value).casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _dedupe_url(url):
        cleaned = (url or "").strip()
        if not cleaned:
            return ""
        parsed = urlparse(cleaned)
        if not parsed.scheme or not parsed.netloc:
            return cleaned.rstrip("/")
        return parsed._replace(fragment="").geturl().rstrip("/")

    @staticmethod
    def _hash_id(value):
        return hashlib.sha1((value or "").encode("utf-8", errors="replace")).hexdigest()

    def _near_deadline(self, start_time):
        elapsed = time.monotonic() - start_time
        return elapsed >= (self.MAX_WALL_SECONDS - self.WALL_MARGIN_SECONDS)
