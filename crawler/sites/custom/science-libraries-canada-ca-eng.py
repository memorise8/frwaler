# -*- coding: utf-8 -*-
"""Science Libraries Canada (science-libraries.canada.ca) crawler.

Starting URL:
    https://science-libraries.canada.ca/eng/search/?q=*&fc=ContentType%3AConference+Proceeding&sm=1

The federated search result list mixes two kinds of detail links:

- ``nrc-publications.canada.ca/eng/view/object/?id=<uuid>`` — NRC Publications
  Archive (NPARC) records with structured metadata (author/date/abstract/
  DOI/pdf download). These are the only records that carry an abstract.
- ``science-catalogue.canada.ca/record=<id>~S6`` — physical library catalogue
  (Sirsi) records with no abstract field; these are skipped.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(markup):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(markup, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


def _curl(args, timeout=30):
    cmd = ["curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", str(timeout)] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _fetch(url, retries=3, timeout=30):
    """GET url via curl, retrying with 1s/3s/9s backoff. Returns text or None."""
    backoffs = [1, 3, 9]
    for attempt in range(retries):
        raw = _curl([url], timeout=timeout)
        if raw:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("utf-8", errors="replace")
        if attempt < retries - 1:
            time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
    return None


def _fetch_head(url, retries=2, timeout=15):
    """HEAD-style fetch (curl -I) with retries. Returns decoded header text or None."""
    backoffs = [1, 3]
    for attempt in range(retries):
        raw = _curl(["-I", url], timeout=timeout)
        if raw:
            return raw.decode("utf-8", errors="replace")
        if attempt < retries - 1:
            time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
    return None


def _filename_from_headers(headers_text):
    if not headers_text:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', headers_text, re.IGNORECASE)
    if m:
        name = m.group(1).strip().strip('"')
        return name or None
    return None


_DATE_FORMATS = ("%B %d, %Y", "%B %Y", "%b %d, %Y", "%b %Y")


def _parse_date(raw):
    """Best-effort raw date text -> ISO YYYY-MM-DD (or None)."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return raw
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        return f"{raw}-01"
    m = re.match(r"^(\d{4})$", raw)
    if m:
        return f"{raw}-01-01"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"(\d{4})", raw)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _text(node):
    return node.get_text(strip=True) if node is not None else None


class ScienceLibrariesCanadaCaEngCrawler(BaseCrawler):
    """Crawler for science-libraries.canada.ca Conference Proceeding search results."""

    site_id = "science-libraries-canada-ca-eng"
    site_name = "Custom: science-libraries-canada-ca-eng"
    base_url = "https://science-libraries.canada.ca"

    _SEARCH_URL = "https://science-libraries.canada.ca/eng/search/"
    _QUERY = "q=*&fc=ContentType%3AConference+Proceeding&sm=1"
    _MIN_ABSTRACT = 100
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start = time.monotonic()
        saved = 0
        seen_urls = set()
        limit_label = limit if limit is not None else "inf"

        for page in range(1, self._MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start
            if elapsed > self._MAX_WALL:
                print(f"[{self.site_id}] wall-clock budget ({self._MAX_WALL}s) reached at page {page}, exiting")
                break

            list_url = f"{self._SEARCH_URL}?{self._QUERY}&pg={page}"
            time.sleep(self._delay)
            html = _fetch(list_url)
            if not html:
                print(f"[{self.site_id}] page {page}: failed to fetch list page after retries, stopping")
                break

            try:
                soup = _make_soup(html)
            except Exception as exc:
                print(f"[{self.site_id}] page {page}: failed to parse list page: {exc}, stopping")
                break

            items = soup.select("ol.search-results > li article")
            if not items:
                print(f"[{self.site_id}] page {page}: 0 items, stopping")
                break

            new_hrefs = 0
            for article in items:
                if limit is not None and saved >= limit:
                    break

                href = None
                try:
                    link = article.select_one(".metadata-title a")
                    if link is None or not link.get("href"):
                        continue
                    href = urljoin(self.base_url, link["href"])
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)
                    new_hrefs += 1

                    if "nrc-publications.canada.ca" not in href:
                        # Library catalogue records carry no abstract field.
                        continue

                    title = link.get_text(strip=True)
                    if not title:
                        continue

                    if self._crawl_detail(href, title):
                        saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {href or '?'} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            if new_hrefs == 0:
                print(f"[{self.site_id}] page {page}: no new records (pagination repeat), stopping")
                break
        else:
            print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages")

        print(f"[{self.site_id}] done: saved {saved} papers")
        return saved

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _crawl_detail(self, href, list_title):
        time.sleep(self._delay)
        html = _fetch(href)
        if not html:
            print(f"[{self.site_id}] detail fetch failed (all retries): {href}")
            return False

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] detail parse failed: {href}: {exc}")
            return False

        table = soup.select_one("table.table-viewobject")
        if table is None:
            print(f"[{self.site_id}] no metadata table: {href}")
            return False

        fields = {}
        for tr in table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th is None or td is None:
                continue
            fields[th.get_text(strip=True)] = td

        abstract_td = fields.get("Abstract")
        abstract = None
        if abstract_td is not None:
            desc = abstract_td.select_one('[itemprop="description"]')
            abstract = _text(desc) or _text(abstract_td)
        if not abstract or len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] skip (no/short abstract): {href}")
            return False

        title_el = soup.select_one("h1 .citation_title")
        title = _text(title_el) or list_title

        record_id = None
        m = re.search(r"[?&]id=([0-9a-fA-F-]{8,})", href)
        if m:
            record_id = m.group(1)

        nparc_number = _text(fields.get("NPARC number"))
        nrc_number = _text(fields.get("NRC number"))
        post_number = None
        if nparc_number:
            digits = re.sub(r"\D", "", nparc_number)
            post_number = digits or nparc_number
        if not post_number:
            post_number = record_id

        authors_td = fields.get("Author")
        authors = None
        if authors_td is not None:
            names = [_text(s) for s in authors_td.select('[itemprop="name"]')]
            names = [n for n in names if n]
            authors = "; ".join(names) if names else _text(authors_td)

        publisher_td = fields.get("Publisher")
        publisher = None
        if publisher_td is not None:
            names = [_text(s) for s in publisher_td.select('[itemprop="name"]')]
            names = [n for n in names if n]
            publisher = "; ".join(names) if names else _text(publisher_td)

        affiliation_td = fields.get("Affiliation")
        department = None
        if affiliation_td is not None:
            lis = [_text(li) for li in affiliation_td.find_all("li")]
            lis = [i for i in lis if i]
            department = "; ".join(lis) if lis else _text(affiliation_td)

        format_td = fields.get("Format")
        category = None
        if format_td is not None:
            genre = format_td.select_one('[itemprop="genre"]')
            category = _text(genre) or _text(format_td)

        keywords_td = fields.get("Subject")
        keywords = None
        if keywords_td is not None:
            kws = [_text(s) for s in keywords_td.select('[itemprop="keywords"]')]
            kws = [k for k in kws if k]
            keywords = ", ".join(kws) if kws else None

        doi_td = fields.get("DOI")
        doi = None
        if doi_td is not None:
            a = doi_td.find("a")
            doi = a.get_text(strip=True) if a is not None else _text(doi_td)

        published_date_raw = _text(fields.get("Date published"))
        published_date = _parse_date(published_date_raw)

        listed_date_raw = _text(fields.get("Record created"))
        listed_date = _parse_date(listed_date_raw)

        modified_raw = _text(fields.get("Record modified"))
        series_raw = _text(fields.get("Series"))
        conference_raw = _text(fields.get("Conference"))
        physical_desc = _text(fields.get("Physical description"))
        peer_reviewed = _text(fields.get("Peer reviewed"))
        language_raw = _text(fields.get("Language"))

        download_td = fields.get("Download")
        pdf_url = None
        if download_td is not None:
            a = download_td.find("a")
            if a is not None and a.get("href"):
                pdf_url = urljoin(self.base_url, a["href"])

        original_filename = None
        if pdf_url:
            headers_text = _fetch_head(pdf_url)
            original_filename = _filename_from_headers(headers_text)
        if not original_filename and nparc_number:
            digits = re.sub(r"\D", "", nparc_number)
            if digits:
                original_filename = f"{digits}.pdf"

        metadata = {
            "record_identifier": record_id,
            "nrc_number": nrc_number,
            "nparc_number": nparc_number,
            "conference": conference_raw,
            "physical_description": physical_desc,
            "peer_reviewed": peer_reviewed,
            "language": language_raw,
            "department": department,
            "posted_date": listed_date_raw,
            "record_modified": modified_raw,
            "originalFilename": original_filename,
            "journal_raw": series_raw,
            "series": series_raw,
            "volume": None,
            "issue": None,
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}

        paper = {
            "id": record_id,
            "site_id": self.site_id,
            "external_id": post_number,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": href,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

        self._save_paper(paper)
        return True
