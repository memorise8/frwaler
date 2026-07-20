# -*- coding: utf-8 -*-
"""Crawler for ir.arcnl.nl — ARCNL Institutional Repository.

Search API: POST https://ir.arcnl.nl/search/query
  Headers: Accept: application/json, X-Requested-With: XMLHttpRequest
  Body: JSON with facets filter (type: article|dissertation|masterThesis|bachelorThesis)
  Returns: {hits: [...], paging: {next: {from, disabled}}, ...}

Detail pages: https://ir.arcnl.nl/pub/{id}
  - <p class="abstract"> for full abstract
  - <meta name="citation_*"> for structured metadata
"""

import copy
import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SEARCH_URL = "https://ir.arcnl.nl/search/query"
_PAGE_SIZE = 10
_PAGE_CAP = 200
_MAX_SECS = 25 * 60

_SEARCH_PAYLOAD_BASE = {
    "query": {
        "filters": {
            "options": [],
            "values": [{"field_id": "all", "query": ""}],
        },
        "facets": [
            {
                "title": "Type",
                "field_id": "type",
                "api_only": False,
                "users_only": False,
                "type": "default",
                "max_terms": 100,
                "max_display_terms": 5,
                "sort_by": "count",
                "sort_order": "descending",
                "filters": [
                    {"term": "article"},
                    {"term": "dissertation"},
                    {"term": "masterThesis"},
                    {"term": "bachelorThesis"},
                ],
            }
        ],
        "from": 0,
        "sort": "year_desc",
    }
}

_SEARCH_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-tolerant flags; returns raw text or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", url],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=35,
            )
            if result.stdout.strip():
                return result.stdout
        except Exception as exc:
            print(f"[ir-arcnl-nl] curl error attempt {attempt + 1}: {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt
            time.sleep(wait)
    return None


def _make_soup(html: str):
    """BeautifulSoup with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str:
    """Normalise YYYY/MM/DD or YYYY-MM-DD to YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4})[/\-](\d{2})[/\-](\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Partial dates: YYYY/MM or YYYY
    m = re.match(r"(\d{4})[/\-](\d{2})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = re.match(r"^(\d{4})$", raw)
    if m:
        return f"{m.group(1)}-01-01"
    return raw


class IrArcnlNlCrawler(BaseCrawler):

    site_id = "ir-arcnl-nl"
    site_name = "Custom: ir-arcnl-nl"
    base_url = "https://ir.arcnl.nl"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_search_page(self, from_offset: int) -> dict | None:
        """POST search API; returns parsed JSON or None on error."""
        payload = copy.deepcopy(_SEARCH_PAYLOAD_BASE)
        payload["query"]["from"] = from_offset
        body = json.dumps(payload)
        for attempt in range(3):
            try:
                resp = self._session.post(
                    _SEARCH_URL,
                    data=body,
                    headers=_SEARCH_HEADERS,
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                print(f"[ir-arcnl-nl] search page fetch error attempt {attempt + 1}: {exc}")
                if attempt < 2:
                    time.sleep(3 ** attempt)
        return None

    def _parse_detail(self, url: str, hit: dict) -> dict | None:
        """Fetch detail page and extract metadata. Returns dict or None."""
        raw = _curl_get(url)
        if not raw:
            return None
        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[ir-arcnl-nl] BS4 error for {url}: {exc}")
            return None
        if soup is None:
            return None

        def get_meta(name: str) -> str:
            tags = soup.find_all("meta", attrs={"name": name})
            return "; ".join(
                t.get("content", "") for t in tags if t.get("content")
            )

        def get_metas(name: str) -> list:
            return [
                t.get("content", "")
                for t in soup.find_all("meta", attrs={"name": name})
                if t.get("content")
            ]

        # Abstract: paragraph first, fallback to meta
        p_abs = soup.find("p", class_="abstract")
        if p_abs:
            abstract = p_abs.get_text(separator=" ", strip=True)
        else:
            abstract = get_meta("citation_abstract")
        abstract = re.sub(r"\s{2,}", " ", abstract).strip()

        title = get_meta("citation_title") or hit.get("title", "")

        # Authors: multiple citation_author tags
        authors_list = get_metas("citation_author")
        authors = "; ".join(authors_list) if authors_list else ""

        # Dates
        pub_date_raw = (
            get_meta("citation_publication_date") or get_meta("citation_date") or ""
        )
        published_date = _parse_date(pub_date_raw)
        listed_date = _parse_date(hit.get("issued", "")) or published_date

        # Journal
        journal = get_meta("citation_journal_title")
        volume = get_meta("citation_volume")
        issue_num = get_meta("citation_issue")
        issn = get_meta("citation_issn")
        firstpage = get_meta("citation_firstpage")
        lastpage = get_meta("citation_lastpage")

        # DOI / PDF
        doi = get_meta("citation_doi") or hit.get("doi", "") or None
        pdf_url = get_meta("citation_pdf_url") or None
        if not pdf_url:
            for a in soup.find_all("a", href=True):
                if a["href"].lower().endswith(".pdf"):
                    pdf_url = a["href"]
                    break

        original_filename = None
        if pdf_url:
            seg = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if seg.lower().endswith(".pdf"):
                original_filename = seg

        # Publisher / department from page text
        publisher = ""
        department = ""
        main_div = soup.find(id="main") or soup.find("div", class_="span10")
        if main_div:
            text = main_div.get_text(separator="\n", strip=True)
            m = re.search(r"Degree Grantor\s*\n([^\n]+)", text)
            if m:
                publisher = m.group(1).strip()
            m_pub = re.search(r"Publisher\s*\n([^\n]+)", text)
            if not publisher and m_pub:
                publisher = m_pub.group(1).strip()
            m_org = re.search(r"Organisation\s*\n([^\n]+)", text)
            if m_org:
                department = m_org.group(1).strip()

        if not publisher:
            publisher = hit.get("affiliation", "") or "ARCNL"

        # Category
        category = hit.get("type", "")

        # Keywords: no citation_keywords standard, try page
        kw_list = get_metas("citation_keywords")
        if not kw_list:
            kw_tag = soup.find(class_=lambda c: c and "keyword" in " ".join(c).lower())
            if kw_tag:
                kw_list = [kw_tag.get_text(strip=True)]
        keywords = ", ".join(kw_list) if kw_list else None

        metadata = {
            "posted_date": hit.get("issued", ""),
            "pub_type": category,
            "open_access": hit.get("open_access", False),
            "affiliation": hit.get("affiliation", ""),
            "issn": issn or None,
            "journal_raw": journal or None,
            "volume": volume or None,
            "issue": issue_num or None,
            "firstpage": firstpage or None,
            "lastpage": lastpage or None,
            "originalFilename": original_filename,
        }

        return {
            "abstract": abstract,
            "title": title,
            "authors": authors,
            "published_date": published_date,
            "listed_date": listed_date,
            "journal": journal or None,
            "doi": doi,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": publisher,
            "department": department,
            "category": category,
            "keywords": keywords,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"
        from_offset = 0

        for page in range(1, _PAGE_CAP + 1):
            if time.time() - start_time > _MAX_SECS:
                print("[ir-arcnl-nl] 25-min budget reached, stopping.")
                break
            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[ir-arcnl-nl] page {page}: saved {saved}/{limit_str}")
            if page == _PAGE_CAP:
                print(f"[ir-arcnl-nl] Safety cap of {_PAGE_CAP} pages reached.")

            data = self._fetch_search_page(from_offset)
            if not data:
                print(f"[ir-arcnl-nl] page {page}: search fetch failed. Done.")
                break

            hits = data.get("hits", [])
            if not hits:
                print(f"[ir-arcnl-nl] page {page}: empty hits. Done.")
                break

            new_hits = [
                h for h in hits
                if h.get("url") and h["url"] not in seen_urls
            ]
            if not new_hits:
                print(f"[ir-arcnl-nl] page {page}: all hits already seen. Done.")
                break

            for hit in new_hits:
                if limit is not None and saved >= limit:
                    break

                item_url = hit.get("url", "")
                if not item_url:
                    continue
                seen_urls.add(item_url)

                try:
                    time.sleep(self._delay)
                    detail = self._parse_detail(item_url, hit)
                    if not detail:
                        print(f"[ir-arcnl-nl] detail fetch failed: {item_url}")
                        continue

                    abstract = detail.get("abstract", "")
                    title = detail.get("title", "")

                    if not title:
                        print(f"[ir-arcnl-nl] no title, skipping: {item_url}")
                        continue
                    if len(abstract) < 50:
                        print(
                            f"[ir-arcnl-nl] abstract too short "
                            f"({len(abstract)} chars), skipping: {item_url}"
                        )
                        continue

                    pub_id = str(hit.get("id", ""))
                    paper = {
                        "site_id": self.site_id,
                        "external_id": pub_id,
                        "post_number": pub_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": detail.get("published_date", ""),
                        "listed_date": detail.get("listed_date", ""),
                        "authors": detail.get("authors", ""),
                        "publisher": detail.get("publisher", ""),
                        "department": detail.get("department", ""),
                        "journal": detail.get("journal"),
                        "url": item_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "keywords": detail.get("keywords"),
                        "category": detail.get("category", ""),
                        "doi": detail.get("doi"),
                        "metadata": detail.get("metadata"),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(f"[ir-arcnl-nl] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ir-arcnl-nl] item failed ({item_url}): {exc}")
                    continue

            # Advance to next page
            paging = data.get("paging", {})
            next_p = paging.get("next", {})
            if next_p.get("disabled", True):
                print(f"[ir-arcnl-nl] No more pages after page {page}.")
                break
            from_offset = next_p.get("from", from_offset + _PAGE_SIZE)

        print(f"[ir-arcnl-nl] Done. Total saved: {saved}")
        return saved
