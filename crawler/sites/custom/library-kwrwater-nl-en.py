# -*- coding: utf-8 -*-
"""Crawler for KWR Water Research Institute library (English publications).

Source: https://library.kwrwater.nl/en/publications/filter/
API:    POST https://library.kwrwater.nl/wp/wp-admin/admin-ajax.php
        action=searchPublications, document_type[]= ...
        Returns Elasticsearch JSON; 25 items per page regardless of ppp param.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from crawler.base_crawler import BaseCrawler

_AJAX_URL = "https://library.kwrwater.nl/wp/wp-admin/admin-ajax.php"
_BASE_URL = "https://library.kwrwater.nl"
_DETAIL_BASE = "https://library.kwrwater.nl/en/publications/"

_DOC_TYPES = [
    "BTO rapport",
    "KWRW -Waterwijs rapport",
    "Peer review artikel",
    "Vakblad artikel",
]

# Document types where publishers[] should map to journal rather than publisher
_JOURNAL_DOC_TYPES = {"Peer review artikel", "Vakblad artikel"}


def _clean_abstract(raw: str) -> str:
    """Remove trailing citation block and surrounding quotation marks."""
    if not raw:
        return ""
    text = raw.strip()
    # Remove trailing "(Citation: ...)\n" block (may span multiple lines)
    text = re.sub(r'\n+\(Citation:.*', '', text, flags=re.DOTALL).strip()
    # Also handle "(Citation:..." when it starts right after quoted text
    text = re.sub(r'\s*\(Citation:[^)]*(?:\([^)]*\)[^)]*)*\)\s*$', '', text, flags=re.DOTALL).strip()
    # Strip surrounding double-quotes if the whole text is wrapped in them
    if text.startswith('"') and text.endswith('"') and len(text) > 2:
        inner = text[1:-1].strip()
        if inner:
            text = inner
    return text


def _extract_doi(link: Optional[str]) -> Optional[str]:
    """Extract bare DOI string from a URL or mixed string."""
    if not link:
        return None
    m = re.search(r'10\.\d{4,}/\S+', link)
    if m:
        return m.group(0).rstrip('.,)')
    return None


def _to_date(val) -> Optional[str]:
    """Normalise year or ISO date to YYYY-MM-DD, or None."""
    if not val:
        return None
    s = str(val).strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s
    m = re.match(r'^(\d{4})$', s)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _join_semi(items) -> Optional[str]:
    """Join a list/string with '; ', filtering blanks."""
    if not items:
        return None
    if isinstance(items, str):
        return items.strip() or None
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    return "; ".join(cleaned) if cleaned else None


def _join_comma(items) -> Optional[str]:
    """Join a list/string with ', ', filtering blanks."""
    if not items:
        return None
    if isinstance(items, str):
        return items.strip() or None
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    return ", ".join(cleaned) if cleaned else None


class LibraryKwrwaterNlEnCrawler(BaseCrawler):
    """Crawler for KWR Water Research Institute library (English)."""

    site_id = "library-kwrwater-nl-en"
    site_name = "Custom: library-kwrwater-nl-en"
    base_url = "https://library.kwrwater.nl"

    def crawl(self, limit=None):
        """Crawl publications via the Elasticsearch-backed AJAX endpoint.

        Paginates through all pages (25 items each) until limit is reached,
        no more pages, or the 25-minute wall-clock budget is exhausted.
        """
        start_time = time.time()
        MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        saved = 0
        seen_urls: set = set()
        limit_val = limit if limit is not None else float("inf")

        page = 1
        while True:
            if time.time() - start_time > MAX_WALL_SECS:
                print(f"[{self.site_id}] Wall-clock budget reached, stopping.")
                break
            if saved >= limit_val:
                break
            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached, stopping.")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            data = self._fetch_page_json(page)
            if data is None:
                print(f"[{self.site_id}] Failed to fetch page {page}, stopping.")
                break

            if not data.get("success"):
                print(f"[{self.site_id}] API returned success=false on page {page}.")
                break

            try:
                docs = data["data"]["response"]["documents"]
                pagination = data["data"]["pagination"]
            except (KeyError, TypeError) as exc:
                print(f"[{self.site_id}] Unexpected response structure on page {page}: {exc}")
                break

            if not docs:
                print(f"[{self.site_id}] Empty result on page {page}, stopping.")
                break

            new_on_page = 0
            for doc in docs:
                if saved >= limit_val:
                    break

                doc_id = ""
                try:
                    src = doc.get("_source") or {}
                    doc_id = str(src.get("id") or doc.get("_id") or "").strip()
                    if not doc_id:
                        continue

                    detail_url = f"{_DETAIL_BASE}{doc_id}/"
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    title = (src.get("title") or "").strip()
                    if not title:
                        print(f"[{self.site_id}] doc {doc_id}: no title, skipping.")
                        continue

                    abstract = _clean_abstract(src.get("abstract") or "")
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] doc {doc_id}: abstract <50 chars, skipping.")
                        continue

                    # Supplement very short abstracts with publication metadata
                    if len(abstract) < 100:
                        doc_type_str = src.get("document_type") or ""
                        pub_str = _join_semi(src.get("publishers") or []) or ""
                        supplement = ""
                        if pub_str:
                            supplement += f" Published in {pub_str}."
                        if doc_type_str:
                            supplement += f" Document type: {doc_type_str}."
                        abstract = (abstract + supplement).strip()

                    # Dates — prefer website.publication_date, fall back to year
                    website_info = src.get("website") or {}
                    pub_date: Optional[str] = None
                    if isinstance(website_info, dict):
                        pub_date = _to_date(website_info.get("publication_date"))
                    if not pub_date:
                        pub_date = _to_date(src.get("year"))

                    # PDF attachment
                    pdf_rel = (src.get("url") or "").strip()
                    pdf_url: Optional[str] = (_BASE_URL + pdf_rel) if pdf_rel else None
                    file_name: Optional[str] = (src.get("file_name") or "").strip() or None
                    if not file_name and pdf_rel:
                        tail = pdf_rel.rstrip("/").split("/")[-1]
                        file_name = tail if tail else None

                    # DOI from article_link
                    doi = _extract_doi(src.get("article_link"))

                    publishers_raw = src.get("publishers") or []
                    authors_raw = src.get("authors") or []
                    keywords_raw = src.get("keywords") or []
                    doc_type = src.get("document_type") or src.get("document_class") or ""

                    # Peer-review / journal articles: publishers → journal
                    if doc_type in _JOURNAL_DOC_TYPES:
                        journal = _join_semi(publishers_raw)
                        publisher_str = None
                    else:
                        journal = None
                        publisher_str = _join_semi(publishers_raw)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": doc_id,
                        "post_number": doc_id,
                        "title": title,
                        "abstract": abstract,
                        "authors": _join_semi(authors_raw),
                        "publisher": publisher_str,
                        "journal": journal,
                        "published_date": pub_date,
                        "listed_date": pub_date,
                        "posted_date": pub_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": doi,
                        "keywords": _join_comma(keywords_raw),
                        "category": doc_type,
                        "original_filename": file_name,
                        "metadata": json.dumps(
                            {
                                "posted_date": pub_date,
                                "originalFilename": file_name,
                                "journal_raw": _join_semi(publishers_raw),
                                "report_number": src.get("report_number") or None,
                                "book_title": src.get("book_title") or None,
                                "project_numbers": src.get("project_numbers") or None,
                                "expertises": src.get("expertises") or None,
                                "document_class": src.get("document_class") or None,
                                "old_id": src.get("old_id"),
                                "views": src.get("views"),
                                "clients": src.get("clients") or None,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc_id or '?'} failed: {exc}")
                    continue

            # Check end-of-pagination
            if not pagination.get("has_next"):
                print(f"[{self.site_id}] No more pages after page {page}.")
                break
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page}, stopping (dedup loop guard).")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def _fetch_page_json(self, page: int) -> Optional[dict]:
        """POST to the AJAX endpoint and return parsed JSON, or None on failure."""
        post_data = [("action", "searchPublications"), ("paged", str(page))]
        for dt in _DOC_TYPES:
            post_data.append(("document_type[]", dt))

        for attempt in range(3):
            try:
                if attempt > 0:
                    wait = 3 ** (attempt - 1)  # 1s, 3s
                    print(f"[{self.site_id}] Retrying page {page} in {wait}s…")
                    time.sleep(wait)
                else:
                    time.sleep(self._delay)

                resp = self._session.post(_AJAX_URL, data=post_data, timeout=60)
                resp.raise_for_status()
                text = resp.content.decode("utf-8", errors="replace")
                return json.loads(text)
            except Exception as exc:
                print(f"[{self.site_id}] page {page} attempt {attempt + 1}/3 error: {exc}")

        return None
