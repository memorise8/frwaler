# -*- coding: utf-8 -*-
"""Swiss TPH publications crawler.

List source:
    https://www.swisstph.ch/en/publications

The Swiss TPH page is a TYPO3 publication list. Its "load more" links return
cumulative HTML slices (initial page, load/80, load/120, ...). Publication
details and abstracts are fetched from PubMed eUtils when a PMID is exposed in
the list row.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree as ET

from crawler.base_crawler import BaseCrawler


def _make_soup(raw: str):
    """Build BeautifulSoup with the required parser fallback chain."""
    from bs4 import BeautifulSoup

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception:
            continue
    return None


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _first_text(parent, selector: str) -> str:
    if parent is None:
        return ""
    node = parent.find(selector)
    return _clean_text("".join(node.itertext())) if node is not None else ""


def _month_number(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{1,2}", value):
        number = int(value)
        if 1 <= number <= 12:
            return f"{number:02d}"
        return None
    months = {
        "jan": "01",
        "feb": "02",
        "mar": "03",
        "apr": "04",
        "may": "05",
        "jun": "06",
        "jul": "07",
        "aug": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dec": "12",
    }
    return months.get(value[:3].lower())


def _iso_date(year: str | None, month: str | None = None, day: str | None = None) -> str | None:
    if not year:
        return None
    m = re.search(r"(19|20)\d{2}", str(year))
    if not m:
        return None
    yyyy = m.group(0)
    mm = _month_number(month) or "01"
    dd = "01"
    if day and re.fullmatch(r"\d{1,2}", str(day).strip()):
        day_number = int(str(day).strip())
        if 1 <= day_number <= 31:
            dd = f"{day_number:02d}"
    return f"{yyyy}-{mm}-{dd}"


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


class SwissTPHCrawler(BaseCrawler):
    site_id = "swisstph-ch-en"
    site_name = "Custom: swisstph-ch-en"
    base_url = "https://www.swisstph.ch"

    _LIST_URL = "https://www.swisstph.ch/en/publications"
    _EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _STOP_GRACE_SECONDS = 30
    _MIN_ABSTRACT_CHARS = 100

    def _curl_get(self, url: str, *, max_time: int = 45) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            url,
        ]
        waits = (1, 3, 9)
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                err = result.stderr.decode("utf-8", errors="replace").strip()
                if attempt == len(waits):
                    print(f"[{self.site_id}] curl failed for {url}: {err or 'empty response'}")
                    return None
                print(f"[{self.site_id}] curl retry {attempt}/3 for {url}: {err or 'empty response'}")
            except Exception as exc:
                if attempt == len(waits):
                    print(f"[{self.site_id}] curl failed for {url}: {exc}")
                    return None
                print(f"[{self.site_id}] curl retry {attempt}/3 for {url}: {exc}")
            time.sleep(wait)
        return None

    def _parse_list_page(self, raw: str) -> list[dict]:
        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup failed on list page: {exc}")
            return []
        if soup is None:
            return []

        list_node = soup.find("div", id="publicationList-list")
        if list_node is None:
            return []
        list_group = list_node.find("div", class_="list-group") or list_node

        records: list[dict] = []
        for row in list_group.find_all("div", class_="row", recursive=False):
            try:
                cols = row.find_all("div", class_=re.compile(r"\bcol-sm-\d\b"), recursive=False)
                if len(cols) < 2:
                    continue
                citation_node = cols[0].find("p")
                if citation_node is None:
                    continue

                citation = _clean_text(citation_node.get_text(" "))
                if not citation:
                    continue

                doi_url = None
                doi = None
                pubmed_url = None
                pmid = None
                for link in row.find_all("a", href=True):
                    href = html.unescape(link.get("href", "")).strip()
                    text = _clean_text(link.get_text(" "))
                    if not href:
                        continue
                    if "doi.org" in href or "dx.doi.org" in href:
                        doi = text or href.split("doi.org/")[-1]
                        doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
                        doi_url = f"https://doi.org/{doi}"
                    if "pubmed" in href or "ncbi.nlm.nih.gov" in href:
                        m = re.search(r"/pubmed/(\d+)", href)
                        if not m:
                            m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", href)
                        if m:
                            pmid = m.group(1)
                            pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

                right_lines = [
                    line.strip()
                    for line in cols[1].get_text("\n").splitlines()
                    if line.strip()
                ]
                list_year = next((line for line in right_lines if re.fullmatch(r"(19|20)\d{2}", line)), None)
                category = ""
                for line in right_lines:
                    if line != list_year and line.lower() != "pubmed":
                        category = line
                        break

                parsed = self._parse_citation(citation_node, citation)
                external_id = pmid or doi or ("hash-" + hashlib.sha256(citation.encode("utf-8")).hexdigest()[:20])
                post_number = pmid or doi or external_id
                detail_url = pubmed_url or doi_url or f"{self._LIST_URL}#{external_id}"
                listed_date = _iso_date(list_year)

                records.append(
                    {
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": parsed.get("title") or citation[:240],
                        "authors": parsed.get("authors") or "",
                        "journal": parsed.get("journal") or "",
                        "journal_raw": parsed.get("journal_raw") or "",
                        "series": parsed.get("series"),
                        "volume": parsed.get("volume"),
                        "issue": parsed.get("issue"),
                        "year": list_year,
                        "listed_date": listed_date,
                        "posted_date_raw": list_year,
                        "category": category,
                        "doi": doi,
                        "doi_url": doi_url,
                        "pmid": pmid,
                        "pubmed_url": pubmed_url,
                        "url": detail_url,
                        "full_citation": citation,
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] row parse failed: {exc}")
                continue
        return records

    def _parse_citation(self, citation_node, citation: str) -> dict:
        journal_node = citation_node.find("i")
        journal = _clean_text(journal_node.get_text(" ")) if journal_node is not None else ""

        raw_authors = ""
        title = ""
        if journal_node is not None:
            citation_html = str(citation_node)
            match = re.search(r"<i\b", citation_html, flags=re.IGNORECASE)
            before_journal = citation_html[: match.start()] if match else citation_html
            before_soup = _make_soup(before_journal)
            before_text = _clean_text(before_soup.get_text(" ") if before_soup else re.sub(r"<[^>]+>", " ", before_journal))
            before_text = before_text.rstrip(". ")
            pieces = before_text.rsplit(". ", 1)
            if len(pieces) == 2:
                raw_authors, title = pieces[0].strip(), pieces[1].strip()
            else:
                title = before_text
        else:
            no_doi = re.sub(r"\s+DOI:\s+\S+.*$", "", citation).strip()
            title = no_doi.split(". ", 1)[0].strip() if ". " in no_doi else no_doi

        volume = None
        issue = None
        if journal:
            journal_pattern = re.escape(journal)
            after = re.split(journal_pattern, citation, maxsplit=1)
            tail = after[1] if len(after) > 1 else ""
            vi = re.search(r"\b(19|20)\d{2}(?:\s*\([^)]+\))?;(?P<volume>[^:;.\s(]+)(?:\((?P<issue>[^)]+)\))?", tail)
            if vi:
                volume = vi.group("volume")
                issue = vi.group("issue")

        return {
            "authors": self._normalize_authors(raw_authors),
            "title": title,
            "journal": journal,
            "journal_raw": journal,
            "series": None,
            "volume": volume,
            "issue": issue,
        }

    def _normalize_authors(self, raw: str | None) -> str:
        raw = _clean_text(raw)
        if not raw:
            return ""
        parts = [part.strip().rstrip(".") for part in re.split(r",\s*", raw) if part.strip()]
        return "; ".join(parts)

    def _load_more_url(self, raw: str) -> str | None:
        try:
            soup = _make_soup(raw)
        except Exception:
            soup = None
        if soup is not None:
            link = soup.find("a", id="load-more-publications")
            if link is not None and link.get("href"):
                href = html.unescape(link["href"]).split("#", 1)[0]
                return urljoin(self.base_url, href)

        match = re.search(r'id=["\']load-more-publications["\'][^>]+href=["\']([^"\']+)', raw or "")
        if not match:
            return None
        href = html.unescape(match.group(1)).split("#", 1)[0]
        return urljoin(self.base_url, href)

    def _fetch_pubmed(self, pmid: str) -> dict | None:
        url = f"{self._EFETCH_URL}?db=pubmed&id={pmid}&rettype=xml&retmode=xml"
        raw = self._curl_get(url, max_time=35)
        if not raw:
            return None
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            print(f"[{self.site_id}] PubMed XML parse failed for {pmid}: {exc}")
            return None

        article = root.find(".//Article")
        pubmed_article = root.find(".//PubmedArticle")
        if article is None or pubmed_article is None:
            return None

        title_node = article.find(".//ArticleTitle")
        title = _clean_text("".join(title_node.itertext())) if title_node is not None else ""

        abstract_parts = []
        for node in article.findall(".//AbstractText"):
            label = _clean_text(node.get("Label") or node.get("NlmCategory") or "")
            text = _clean_text("".join(node.itertext()))
            if text:
                abstract_parts.append(f"{label}: {text}" if label else text)
        abstract = " ".join(abstract_parts).strip()

        authors = []
        for author in article.findall(".//Author"):
            collective = _first_text(author, "CollectiveName")
            if collective:
                authors.append(collective)
                continue
            last = _first_text(author, "LastName")
            fore = _first_text(author, "ForeName")
            initials = _first_text(author, "Initials")
            if last:
                name = f"{last} {fore or initials}".strip()
                authors.append(name)

        keywords = []
        for node in pubmed_article.findall(".//Keyword"):
            text = _clean_text("".join(node.itertext()))
            if text and text not in keywords:
                keywords.append(text)
        for node in pubmed_article.findall(".//MeshHeading/DescriptorName"):
            text = _clean_text("".join(node.itertext()))
            if text and text not in keywords:
                keywords.append(text)

        journal_node = article.find(".//Journal")
        journal_title = _first_text(journal_node, "Title")
        journal_iso = _first_text(journal_node, "ISOAbbreviation")
        journal = journal_title or journal_iso

        journal_issue = article.find(".//JournalIssue")
        volume = _first_text(journal_issue, "Volume") if journal_issue is not None else ""
        issue = _first_text(journal_issue, "Issue") if journal_issue is not None else ""

        doi = None
        for article_id in pubmed_article.findall(".//ArticleId"):
            if (article_id.get("IdType") or "").lower() == "doi":
                doi = _clean_text(article_id.text)
                break
        if not doi:
            for eid in article.findall(".//ELocationID"):
                if (eid.get("EIdType") or "").lower() == "doi":
                    doi = _clean_text(eid.text)
                    break

        published_date = self._pubmed_publication_date(article)
        pubmed_posted_raw = self._pubmed_history_date(pubmed_article)

        return {
            "title": title,
            "abstract": abstract,
            "authors": "; ".join(authors) if authors else "",
            "keywords": ", ".join(keywords) if keywords else "",
            "journal": journal,
            "journal_raw": journal_title or journal_iso,
            "volume": volume or None,
            "issue": issue or None,
            "doi": doi,
            "published_date": published_date,
            "pubmed_posted_date": pubmed_posted_raw,
        }

    def _pubmed_publication_date(self, article) -> str | None:
        article_date = article.find(".//ArticleDate")
        if article_date is not None:
            date = _iso_date(_first_text(article_date, "Year"), _first_text(article_date, "Month"), _first_text(article_date, "Day"))
            if date:
                return date

        pub_date = article.find(".//JournalIssue/PubDate")
        if pub_date is not None:
            medline = _first_text(pub_date, "MedlineDate")
            if medline:
                return _iso_date(medline)
            return _iso_date(_first_text(pub_date, "Year"), _first_text(pub_date, "Month"), _first_text(pub_date, "Day"))
        return None

    def _pubmed_history_date(self, pubmed_article) -> str | None:
        for status in ("pubmed", "entrez", "medline"):
            node = pubmed_article.find(f".//PubMedPubDate[@PubStatus='{status}']")
            if node is not None:
                date = _iso_date(_first_text(node, "Year"), _first_text(node, "Month"), _first_text(node, "Day"))
                if date:
                    return date
        return None

    def _paper_from_record(self, record: dict, detail: dict | None) -> dict | None:
        detail = detail or {}

        title = detail.get("title") or record.get("title") or ""
        abstract = detail.get("abstract") or ""
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] short abstract for {record.get('external_id')}: {len(abstract)} chars; skipped")
            return None

        doi = detail.get("doi") or record.get("doi")
        pdf_url = None
        original_filename = _filename_from_url(pdf_url)
        listed_date = record.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        journal = detail.get("journal") or record.get("journal") or None
        journal_raw = detail.get("journal_raw") or record.get("journal_raw") or None
        volume = detail.get("volume") or record.get("volume")
        issue = detail.get("issue") or record.get("issue")
        keywords = detail.get("keywords") or ""

        metadata = {
            "posted_date": record.get("posted_date_raw"),
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": record.get("series"),
            "volume": volume,
            "issue": issue,
            "pmid": record.get("pmid"),
            "doi": doi,
            "doi_url": record.get("doi_url"),
            "pubmed_url": record.get("pubmed_url"),
            "pubmed_posted_date": detail.get("pubmed_posted_date"),
            "post_number": record.get("post_number"),
            "publication_type": record.get("category"),
            "full_citation": record.get("full_citation"),
            "source_list_url": self._LIST_URL,
            "detail_source": "pubmed_eutils" if record.get("pmid") else "swisstph_list_html",
        }
        metadata = {key: value for key, value in metadata.items() if value not in (None, "", [])}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": record.get("external_id"),
            "post_number": record.get("post_number"),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": detail.get("authors") or record.get("authors") or None,
            "publisher": "Swiss Tropical and Public Health Institute",
            "department": None,
            "journal": journal,
            "url": record.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": record.get("category"),
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        start_ts = time.monotonic()
        saved = 0
        page = 0
        current_url = self._LIST_URL
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                break
            elapsed = time.monotonic() - start_ts
            if elapsed >= self._MAX_WALL_SECONDS - self._STOP_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; stopping cleanly")
                break

            raw = self._curl_get(current_url, max_time=60)
            if not raw:
                print(f"[{self.site_id}] list page fetch failed at page {page + 1}; stopping")
                break

            page += 1
            records = self._parse_list_page(raw)
            if not records:
                print(f"[{self.site_id}] page {page}: no records; stopping")
                break

            new_records = []
            for record in records:
                url_key = record.get("url") or record.get("external_id")
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            for index, record in enumerate(new_records, start=1):
                if limit is not None and saved >= limit:
                    break
                elapsed = time.monotonic() - start_ts
                if elapsed >= self._MAX_WALL_SECONDS - self._STOP_GRACE_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; stopping cleanly")
                    return saved

                item_label = record.get("external_id") or f"page-{page}-item-{index}"
                try:
                    detail = None
                    if record.get("pmid"):
                        detail = self._fetch_pubmed(record["pmid"])
                    paper = self._paper_from_record(record, detail)
                    if paper is None:
                        time.sleep(self._delay)
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                    time.sleep(self._delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    time.sleep(self._delay)
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if limit is not None and saved >= limit:
                break

            next_url = self._load_more_url(raw)
            if not next_url:
                print(f"[{self.site_id}] no next page link; stopping")
                break
            if next_url == current_url:
                print(f"[{self.site_id}] next page repeated current URL; stopping")
                break
            current_url = next_url

        print(f"[{self.site_id}] done. saved {saved}")
        return saved
