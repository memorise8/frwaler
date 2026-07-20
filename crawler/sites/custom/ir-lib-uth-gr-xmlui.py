# -*- coding: utf-8 -*-
"""Crawler for UTH Institutional Repository — ΤΕΦΑΑ Masters Theses (DSpace 6.3).

Target collection: https://ir.lib.uth.gr/xmlui/handle/11615/17762
Starting URL:
  https://ir.lib.uth.gr/xmlui/handle/11615/17762/discover
  ?filtertype=has_content_in_original_bundle&filter_relational_operator=equals&filter=true
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment]

from crawler.base_crawler import BaseCrawler

_SITE_ID = "ir-lib-uth-gr-xmlui"
_BASE_URL = "https://ir.lib.uth.gr"
_COLLECTION_HANDLE = "11615/17762"
_COMMUNITY_HANDLE = "11615/17710"

# Paginated discover URL for the ΤΕΦΑΑ masters theses collection.
# rpp=50 keeps page count low; page= is 1-indexed.
_LIST_TMPL = (
    f"{_BASE_URL}/xmlui/handle/{_COLLECTION_HANDLE}/discover"
    "?rpp=50&page={page}"
    "&filtertype_0=has_content_in_original_bundle"
    "&filter_relational_operator_0=equals"
    "&filter_0=true"
)

_ABSTRACT_MIN = 50      # skip items with abstract shorter than this (per spec)
_MAX_PAGES = 200        # safety cap — log when reached
_WALL_SECONDS = 25 * 60  # 25-minute per-run budget


class IrLibUthGrXmluiCrawler(BaseCrawler):
    """Crawls the UTH DSpace 6.3 ΤΕΦΑΑ masters-theses collection."""

    site_id = _SITE_ID
    site_name = "Custom: ir-lib-uth-gr-xmlui"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_ts = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_ts > _WALL_SECONDS:
                print(f"[{_SITE_ID}] 25-min wall-clock budget reached, stopping")
                break

            list_url = _LIST_TMPL.format(page=page)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{_SITE_ID}] page {page}: fetch failed, stopping")
                break

            handles = self._parse_list(raw, seen_urls)
            if not handles:
                print(f"[{_SITE_ID}] page {page}: no new items, done")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            if page == _MAX_PAGES:
                print(
                    f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; "
                    "logging and stopping"
                )

            for handle_path in handles:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_ts > _WALL_SECONDS:
                    print(f"[{_SITE_ID}] 25-min budget reached mid-page, stopping")
                    break

                detail_url = f"{_BASE_URL}/xmlui/handle/{handle_path}"
                try:
                    paper = self._fetch_detail(detail_url, handle_path)
                    if paper is None:
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN:
                        print(
                            f"[{_SITE_ID}] {detail_url}: abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {detail_url} failed: {exc}")
                    continue

                time.sleep(1.0)

        print(f"[{_SITE_ID}] done: saved {saved}/{limit_label}")
        return saved

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, html: str, seen_urls: set) -> list[str]:
        """Return new handle paths (e.g. '11615/1540') not yet in seen_urls."""
        soup = _make_soup(html)
        if soup is None:
            return []

        _skip_hrefs = {
            f"/xmlui/handle/{_COLLECTION_HANDLE}",
            f"/xmlui/handle/{_COMMUNITY_HANDLE}",
        }
        handles: list[str] = []
        for a in soup.find_all("a", href=re.compile(r"^/xmlui/handle/11615/\d+$")):
            href = a["href"]
            if href in _skip_hrefs or href in seen_urls:
                continue
            seen_urls.add(href)
            handles.append(href.replace("/xmlui/handle/", ""))

        return handles

    # ------------------------------------------------------------------
    # Detail-page fetch + parse
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str, handle_path: str) -> dict | None:
        """Fetch and parse a detail page; return paper dict or None."""
        raw = self._curl_get(url, context=f"detail {handle_path}")
        if not raw:
            return None

        soup = _make_soup(raw)
        if soup is None:
            return None

        def get_meta(name: str) -> str | None:
            tag = soup.find("meta", {"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
            return None

        def get_all_meta(name: str) -> list[str]:
            return [
                t["content"].strip()
                for t in soup.find_all("meta", {"name": name})
                if t.get("content") and t["content"].strip()
            ]

        title = get_meta("DC.title") or get_meta("citation_title") or ""
        abstract = get_meta("DCTERMS.abstract") or ""

        # Dates
        issued = get_meta("DCTERMS.issued") or get_meta("citation_date") or ""
        date_accepted = (
            get_meta("DCTERMS.dateAccepted") or get_meta("DCTERMS.available") or ""
        )
        published_date = _parse_date(issued)
        posted_date = _parse_date(date_accepted)  # becomes listed_date via adapter

        # Authors / contributors
        creators = get_all_meta("DC.creator")
        contributors = get_all_meta("DC.contributor")
        authors = "; ".join(creators) if creators else None

        # Subjects → keywords
        subjects = get_all_meta("DC.subject")
        keywords = ", ".join(subjects) if subjects else None

        # Identifiers
        identifiers = get_all_meta("DC.identifier")
        numeric_id: str | None = None
        doi: str | None = None
        hdl_url: str | None = None
        for idf in identifiers:
            if re.match(r"^\d+$", idf):
                numeric_id = idf
            elif "doi.org" in idf:
                doi = re.sub(r"https?://(?:dx\.)?doi\.org/", "", idf).strip()
            elif "hdl.handle.net" in idf:
                hdl_url = idf

        external_id = handle_path   # e.g. "11615/1540"
        post_number = numeric_id    # DSpace internal numeric string

        # PDF
        pdf_url = get_meta("citation_pdf_url") or None
        if not pdf_url:
            # fallback: first .pdf bitstream link in body
            for a in soup.find_all("a", href=re.compile(r"/bitstream/handle/.+\.pdf", re.I)):
                href = a.get("href", "")
                if href and "license" not in href.lower():
                    pdf_url = urljoin(_BASE_URL, href.split("?")[0])
                    break

        original_filename: str | None = None
        if pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if "." in tail:
                original_filename = tail

        publisher = get_meta("DC.publisher") or None
        lang = get_meta("DC.language") or None
        doc_type = get_meta("DC.type") or None
        rights = get_all_meta("DC.rights")

        metadata: dict = {
            "handle_path": handle_path,
            "hdl_url": hdl_url,
            "posted_date": date_accepted,   # raw form for parser
            "contributors": contributors,
            "doc_type": doc_type,
            "language": lang,
            "rights": rights,
        }
        if doi:
            metadata["doi"] = doi
        if numeric_id:
            metadata["numeric_id"] = numeric_id

        return {
            "site_id": _SITE_ID,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": posted_date,
            "authors": authors,
            "publisher": publisher,
            "keywords": keywords,
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "original_filename": original_filename,
            "category": doc_type,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Network helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request", retries: int = 3) -> str | None:
        """Fetch URL via curl with retry + exponential backoff (1 s, 3 s, 9 s)."""
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-skL",
                        "--connect-timeout", "15",
                        "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                        "-H", "Accept-Language: en-US,en;q=0.9,el;q=0.7",
                        url,
                    ],
                    capture_output=True,
                    timeout=40,
                )
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{_SITE_ID}] {context} curl exception "
                    f"attempt {attempt + 1}/{retries}: {last_error}"
                )
                if attempt < retries - 1:
                    time.sleep(waits[attempt])
                continue

            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")

            stderr = (
                result.stderr.decode("utf-8", errors="replace").strip()
                if result.stderr
                else ""
            )
            last_error = f"exit={result.returncode} {stderr}"
            print(
                f"[{_SITE_ID}] {context} curl attempt {attempt + 1}/{retries} "
                f"failed for {url}: {last_error}"
            )
            if attempt < retries - 1:
                time.sleep(waits[attempt])

        print(f"[{_SITE_ID}] {context} curl failed after {retries} attempts for {url}")
        return None


# ------------------------------------------------------------------
# Module-level helpers (no package context needed)
# ------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML trying html5lib → lxml → html.parser."""
    if BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str | None:
    """Convert various date representations to YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    m = re.match(r"^(\d{4})$", raw)
    if m:
        return f"{m.group(1)}-01-01"
    return raw
