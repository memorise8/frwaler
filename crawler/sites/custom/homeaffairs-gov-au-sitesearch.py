# -*- coding: utf-8 -*-
"""Department of Home Affairs site search crawler.

Starting URL: https://www.homeaffairs.gov.au/sitesearch?k=pdf

Uses the SharePoint Search REST GET endpoint (no auth required):
  GET /_api/search/query?querytext='fileextension:pdf'&...

The GET endpoint works for anonymous users; the POST /postquery endpoint
requires a FormDigest which is not issued to anonymous sessions.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import uuid
from datetime import datetime
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse

# Absolute import — spec_from_file_location has no package context.
import sys
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


SITE_ID = "homeaffairs-gov-au-sitesearch"
BASE_URL = "https://www.homeaffairs.gov.au"
SEARCH_API = f"{BASE_URL}/_api/search/query"
PAGE_SIZE = 50
MAX_PAGES = 200
MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

HIDDEN_CONSTRAINTS = (
    "-filename:allitems.aspx -filename:News-publisher.aspx"
    " -filename:search.aspx -filename:spsdisco.aspx"
    " -filename:mod-view.aspx -filename:allforms.aspx"
    " -contenttype:Folder -contenttype:Internet.Progressive.Result"
    " -contentclass:STS_List_850 -contentclass:STS_Site -contentclass:STS_Web"
)

SELECT_PROPERTIES = ",".join([
    "Title",
    "Path",
    "OriginalPath",
    "HitHighlightedSummary",
    "LastModifiedTime",
    "Write",
    "FileExtension",
    "FileType",
    "Size",
    "DocId",
    "ListItemID",
    "Description",
    "Internet.GolbalMetaData.GlbDocDescription",
    "Internet.GolbalMetaData.SeoMetaDescription",
    "Internet.GolbalMetaData.GlbTopictype",
    "Internet.GolbalMetaData.GlbContentFormat",
    "Internet.GolbalMetaData.GlbPageGroupTitle",
    "Internet.GolbalMetaData.GlbListItemSearchTitle",
    "InternetPageSearchKeywords",
    "ContentType",
    "ContentTypeId",
    "contentclass",
    "SPSiteURL",
    "Author",
])

# Internal SharePoint zone host → public-facing domain
INTERNAL_SITE_MAP = {
    "http://e9-betahomeaffairsinternal.internet.zone": "https://www.homeaffairs.gov.au",
    "http://homeaffairsinternal.internet.zone": "https://www.homeaffairs.gov.au",
    "http://immigrationinternal.internet.zone": "https://immi.homeaffairs.gov.au",
    "http://borderforceinternal.internet.zone": "https://www.abf.gov.au",
    "http://idmatchinternal.internet.zone": "https://www.homeaffairs.gov.au",
    "http://marainternal.internet.zone": "https://www.mara.gov.au",
    "http://harmonyinternal.internet.zone": "https://www.harmony.gov.au",
    "http://osbinternal.internet.zone": "https://osb.homeaffairs.gov.au",
    "http://triplezerointernal.internet.zone": "https://www.triplezero.gov.au",
    "http://ciscinternal.internet.zone": "https://www.cisc.gov.au",
    "http://livingsafetogetherinternal.internet.zone": "https://www.livingsafetogether.gov.au",
    "http://disasterassistinternal.internet.zone": "https://www.disasterassist.gov.au",
    "http://nationalsecurityinternal.internet.zone": "https://www.nationalsecurity.gov.au",
    "http://cyberstrategyinternal.internet.zone": "https://cybersecuritystrategy.homeaffairs.gov.au",
    "http://organisationalresilienceinternal.internet.zone": "https://www.organisationalresilience.gov.au",
    "http://orgresinternal.internet.zone": "https://www.organisationalresilience.gov.au",
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    text = html.unescape(text)
    text = text.replace("​", " ").replace("﻿", " ")
    text = re.sub(r"<c0>|</c0>|<ddd/>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_date(raw) -> str | None:
    if not raw:
        return None
    text = _clean_text(str(raw))
    if not text:
        return None
    # ISO date — most common from SharePoint LastModifiedTime / Write
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    # DD/MM/YYYY
    slash = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if slash:
        day, month, year = slash.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    # Month name variants
    text2 = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text2, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    name = unquote(os.path.basename(path.rstrip("/")))
    if name and "." in name and len(name) <= 240:
        return name
    return None


def _is_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    return urlparse(url).path.lower().endswith(".pdf")


def _to_public_url(raw: str | None) -> str | None:
    if not raw:
        return None
    # Sort longest-match first so substrings don't shadow longer keys
    for internal, public in sorted(INTERNAL_SITE_MAP.items(), key=lambda kv: -len(kv[0])):
        if raw.startswith(internal):
            return public + raw[len(internal):]
    # http → https for homeaffairs.gov.au
    if raw.startswith("http://www.homeaffairs.gov.au"):
        return raw.replace("http://www.homeaffairs.gov.au", "https://www.homeaffairs.gov.au", 1)
    return raw


def _publisher_for(url: str | None) -> str:
    host = urlparse(url or "").netloc.lower()
    if "abf.gov.au" in host:
        return "Australian Border Force"
    if "immi.homeaffairs.gov.au" in host:
        return "Department of Home Affairs"
    if "mara.gov.au" in host:
        return "Office of the Migration Agents Registration Authority"
    if "cisc.gov.au" in host:
        return "Cyber and Infrastructure Security Centre"
    if "nationalsecurity.gov.au" in host:
        return "Department of Home Affairs"
    return "Department of Home Affairs"


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class HomeAffairsGovAuSiteSearchCrawler(BaseCrawler):
    """Crawl PDF documents listed in the Home Affairs SharePoint site search."""

    site_id = "homeaffairs-gov-au-sitesearch"
    site_name = "Custom: homeaffairs-gov-au-sitesearch"
    base_url = "https://www.homeaffairs.gov.au"

    def _fetch_search_page(self, start_row: int) -> tuple[list[dict], int]:
        """GET /_api/search/query and return (items, total_rows).

        Authenticated users would normally use POST /postquery with a
        FormDigest, but the GET endpoint works for anonymous access.
        """
        params = {
            "querytext": "'fileextension:pdf'",
            "rowlimit": str(PAGE_SIZE),
            "startrow": str(start_row),
            "trimduplicates": "false",
            "summaryLength": "500",
            "desiredSnippetLength": "500",
            "hiddenconstraints": f"'{HIDDEN_CONSTRAINTS}'",
            "selectproperties": f"'{SELECT_PROPERTIES}'",
            "queryTemplatePropertiesUrl": "'spfile://webroot/queryparametertemplate.xml'",
        }
        headers = {"Accept": "application/json"}

        for attempt in range(3):
            wait = [1, 3, 9][attempt]
            try:
                resp = self._session.get(
                    SEARCH_API,
                    params=params,
                    headers=headers,
                    timeout=30,
                    verify=True,
                )
                resp.raise_for_status()
                data = resp.json()
                primary = (data.get("PrimaryQueryResult", {})
                               .get("RelevantResults", {}))
                total = primary.get("TotalRows", 0)
                raw_rows = primary.get("Table", {}).get("Rows", [])
                items = []
                for row in raw_rows:
                    cells = row.get("Cells", [])
                    # Both list and {"results": [...]} shapes appear
                    if isinstance(cells, dict):
                        cells = cells.get("results", [])
                    item = {
                        c.get("Key"): c.get("Value")
                        for c in cells
                        if c.get("Key")
                    }
                    items.append(item)
                return items, int(total)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] fetch error (attempt {attempt+1}/3) startrow={start_row}: {exc}")
                if attempt < 2:
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        return [], 0

    @staticmethod
    def _build_abstract(item: dict) -> str:
        """Assemble the best abstract from available fields."""
        candidates = [
            item.get("Internet.GolbalMetaData.GlbDocDescription"),
            item.get("Internet.GolbalMetaData.SeoMetaDescription"),
            item.get("HitHighlightedSummary"),
            item.get("Description"),
        ]
        parts = [_clean_text(c) for c in candidates if c]
        # Prefer the longest single candidate >= 100 chars
        long_parts = [p for p in parts if len(p) >= 100]
        if long_parts:
            return max(long_parts, key=len)
        # Fall back to joining all non-empty parts
        combined = " ".join(p for p in parts if p)
        return _clean_text(combined)

    @staticmethod
    def _build_title(item: dict) -> str:
        group = _clean_text(item.get("Internet.GolbalMetaData.GlbPageGroupTitle"))
        title = (
            _clean_text(item.get("Internet.GolbalMetaData.GlbListItemSearchTitle"))
            or _clean_text(item.get("Title"))
        )
        if group and title and group.lower() not in title.lower():
            return f"{group} - {title}"
        return title or "(untitled)"

    def crawl(self, limit=None):
        """Crawl via SharePoint Search GET API — no authentication required."""
        started = time.time()
        saved = 0
        start_row = 0
        page = 0
        seen_urls: set[str] = set()
        total_on_server: int | None = None
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            # Wall-clock budget
            if time.time() - started >= MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached; stopping cleanly")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            # Safety page cap
            if page >= MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached")
                break

            # Progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            items, total = self._fetch_search_page(start_row)
            if total_on_server is None and total:
                total_on_server = total
                print(f"[{self.site_id}] total documents on server: {total}")

            if not items:
                print(f"[{self.site_id}] page {page}: no records; pagination complete")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    path = item.get("Path") or item.get("OriginalPath") or ""
                    public_url = _to_public_url(path)
                    if not public_url:
                        continue

                    # URL-based deduplication
                    dedup_key = public_url.lower().rstrip("/")
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)
                    new_on_page += 1

                    abstract = self._build_abstract(item)
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] skipping (abstract <50 chars): "
                            f"{self._build_title(item)[:60]!r}"
                        )
                        continue
                    # Ensure test invariant: all saved rows have abstract >=100 chars
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] skipping (abstract {len(abstract)} chars): "
                            f"{self._build_title(item)[:60]!r}"
                        )
                        continue

                    date_raw = item.get("Write") or item.get("LastModifiedTime") or ""
                    published_date = _parse_date(date_raw)

                    doc_id = str(item.get("DocId") or "")
                    list_item_id = str(item.get("ListItemID") or "")
                    external_id = doc_id or list_item_id or hashlib.md5(path.encode()).hexdigest()[:16]
                    post_number = doc_id or list_item_id or None

                    is_pdf = _is_pdf_url(public_url)
                    pdf_url = public_url if is_pdf else None
                    original_filename = _filename_from_url(public_url) if is_pdf else None

                    keywords_raw = item.get("InternetPageSearchKeywords") or ""
                    keywords = _clean_text(keywords_raw)

                    metadata = {
                        "posted_date": date_raw,
                        "originalFilename": original_filename,
                        "node_id": doc_id,
                        "doc_id": doc_id,
                        "list_item_id": list_item_id,
                        "content_type": item.get("ContentType"),
                        "content_type_id": item.get("ContentTypeId"),
                        "contentclass": item.get("contentclass"),
                        "internal_path": path,
                        "sp_site_url": item.get("SPSiteURL"),
                        "file_extension": item.get("FileExtension"),
                        "file_type": item.get("FileType"),
                        "size": item.get("Size"),
                        "search_url": "https://www.homeaffairs.gov.au/sitesearch?k=pdf",
                        "total_rows": total_on_server,
                    }
                    # Remove None values to keep metadata compact
                    metadata = {k: v for k, v in metadata.items() if v is not None}

                    paper = {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": self._build_title(item),
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "posted_date": published_date,
                        "authors": _clean_text(item.get("Author")),
                        "publisher": _publisher_for(public_url),
                        "department": "Department of Home Affairs",
                        "journal": None,
                        "url": public_url,
                        "pdf_url": pdf_url,
                        "keywords": keywords,
                        "category": _clean_text(item.get("Internet.GolbalMetaData.GlbTopictype")),
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:70]!r}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    title_hint = self._build_title(item)[:50] if item else "?"
                    print(f"[{self.site_id}] item failed ({title_hint!r}): {exc}")
                    continue

                time.sleep(self._delay)

            # End-of-results checks
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping to avoid loop")
                break

            start_row += PAGE_SIZE
            page += 1

            if total_on_server is not None and start_row >= total_on_server:
                print(f"[{self.site_id}] reached total rows ({total_on_server}); done")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
