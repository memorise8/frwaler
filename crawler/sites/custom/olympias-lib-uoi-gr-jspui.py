# -*- coding: utf-8 -*-
"""Crawler for University of Ioannina DSpace repository (olympias.lib.uoi.gr/jspui).

Target: doctoral theses (doctoralThesis type).

Strategy:
  - List pages: simple-search with filter_field_1=type doctoralThesis, paginated via start=N
  - Detail page: handle/{id}?mode=full  — one request per item, contains:
      * <meta name="citation_pdf_url"> in <head>
      * <td class="metadataFieldLabel">heal.abstract</td> rows in the metadata table
  - Abstract: heal.abstract (bilingual: Greek + English).
    Fallback when absent: descriptive metadata summary from available fields.
  - Items whose final abstract < 100 chars are skipped.
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

# Absolute import — spec_from_file_location gives this module no package context.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from crawler.base_crawler import BaseCrawler


_SITE_ID = "olympias-lib-uoi-gr-jspui"
_BASE = "https://olympias.lib.uoi.gr"
_LIST_TPL = (
    _BASE + "/jspui/simple-search"
    "?query=&filter_field_1=type&filter_type_1=equals"
    "&filter_value_1=doctoralThesis&sort_by=score&order=desc"
    "&rpp=20&etal=0&start={start}"
)
_DETAIL_TPL = _BASE + "/jspui/handle/{handle}?mode=full"
_RPP = 20
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with exponential backoff. Returns decoded text or None."""
    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            r = subprocess.run(
                [
                    "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
                    "-H", (
                        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            if r.stdout:
                return r.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                print(f"[{_SITE_ID}] empty response (attempt {attempt+1}), retry in {waits[attempt]}s: {url}")
                time.sleep(waits[attempt])
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt < retries - 1:
                print(f"[{_SITE_ID}] curl error ({exc}), retry in {waits[attempt]}s: {url}")
                time.sleep(waits[attempt])
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {url}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _clean(raw: str) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace."""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def _extract_handles(html: str) -> list[str]:
    """Return deduplicated item handle strings from a search-results page."""
    found = re.findall(r'href="/jspui/handle/(123456789/\d+)"', html)
    seen: set[str] = set()
    result: list[str] = []
    for h in found:
        if h not in seen:
            seen.add(h)
            result.append(h)
    return result


def _parse_full_record(html: str) -> dict:
    """
    Parse a ?mode=full item page.

    Returns:
      meta   – dict[str, list[str]] of <meta name=...> tags
      fields – dict[str, list[str]] of metadata table rows
    """
    # head <meta> tags
    meta: dict[str, list[str]] = {}
    for m in re.finditer(
        r'<meta\s+name="([^"]+)"\s+content="([^"]*)"', html, re.IGNORECASE
    ):
        name, content = m.group(1).strip(), m.group(2).strip()
        meta.setdefault(name, []).append(content)

    # full-record table: metadataFieldLabel / metadataFieldValue pairs
    fields: dict[str, list[str]] = {}
    for field_raw, val_raw in re.findall(
        r'<td[^>]+class="metadataFieldLabel">([^<]+)</td>\s*'
        r'<td[^>]+class="metadataFieldValue">(.*?)</td>',
        html,
        re.DOTALL,
    ):
        field = field_raw.strip()
        val = _clean(val_raw)
        if val and val != "-":
            fields.setdefault(field, []).append(val)

    return {"meta": meta, "fields": fields}


def _first(lst: list[str], default: str = "") -> str:
    return lst[0] if lst else default


def _metadata_summary(title: str, fields: dict[str, list[str]]) -> str:
    """Build a descriptive abstract from metadata when heal.abstract is absent."""
    parts: list[str] = []
    # Always include the full title
    if title:
        parts.append(f"Title: {title}")
    institution = (
        _first(fields.get("heal.recordProvider", []))
        or _first(fields.get("heal.academicPublisher", []))
    )
    if institution:
        parts.append(f"Institution: {institution}")
    classification = _first(fields.get("heal.classification", []))
    if classification:
        parts.append(f"Subject classification: {classification}")
    subjects = [s for s in fields.get("dc.subject", []) if s != "-"]
    if subjects:
        parts.append(f"Keywords: {', '.join(subjects)}")
    advisor = _first(fields.get("heal.advisorName", []))
    if advisor:
        parts.append(f"Advisor: {advisor}")
    pub_date = _first(fields.get("heal.publicationDate", []))
    if pub_date:
        parts.append(f"Publication year: {pub_date}")
    citation = _first(fields.get("heal.bibliographicCitation", []))
    if citation:
        parts.append(f"Bibliographic citation: {citation}")
    pages = _first(fields.get("heal.numberOfPages", []))
    if pages:
        parts.append(f"Number of pages: {pages}")
    return "; ".join(parts)


def _build_paper(handle: str, parsed: dict) -> dict | None:
    meta = parsed["meta"]
    fields = parsed["fields"]

    # --- Title ---
    title = (
        _first(fields.get("dc.title", []))
        or _first(meta.get("citation_title", []))
    )
    if not title:
        return None

    # --- Abstract ---
    # heal.abstract may appear twice (Greek + English); keep both
    raw_abstracts = [v for v in fields.get("heal.abstract", []) if v and v != "-"]
    if raw_abstracts:
        # Longest first (usually the Greek version), then the shorter (English)
        raw_abstracts = sorted(raw_abstracts, key=len, reverse=True)
        abstract = "\n\n".join(raw_abstracts)
    else:
        abstract = _metadata_summary(title, fields)

    # --- Authors ---
    author_list = (
        fields.get("dc.contributor.author", [])
        or meta.get("DC.creator", [])
        or meta.get("citation_author", [])
    )
    authors = ";".join(author_list)

    # --- Dates ---
    raw_pub = (
        _first(fields.get("heal.publicationDate", []))
        or _first(meta.get("citation_date", []))
    )
    if raw_pub and re.match(r"^\d{4}$", raw_pub):
        published_date = raw_pub + "-01-01"
    elif raw_pub and re.match(r"\d{4}-\d{2}-\d{2}", raw_pub):
        published_date = raw_pub[:10]
    else:
        published_date = raw_pub or ""

    raw_listed = _first(fields.get("dc.date.accessioned", []))
    listed_date = raw_listed[:10] if raw_listed else ""

    # --- Publisher / institution ---
    publisher = (
        _first(fields.get("heal.recordProvider", []))
        or _first(fields.get("heal.academicPublisher", []))
    )

    # --- Keywords ---
    subjects = [s for s in fields.get("dc.subject", []) if s and s != "-"]
    keywords = ",".join(subjects)

    # --- Category ---
    category = _first(fields.get("heal.classification", []))

    # --- DOI ---
    doi = ""
    for uri in fields.get("dc.identifier.uri", []):
        if "doi.org" in uri:
            doi = uri
            break

    # --- URL ---
    url = f"{_BASE}/jspui/handle/{handle}"

    # --- PDF URL ---
    pdf_url = _first(meta.get("citation_pdf_url", []))

    # --- Original filename ---
    original_filename = ""
    if pdf_url:
        path_seg = urlparse(pdf_url).path.rstrip("/").split("/")[-1]
        original_filename = unquote(path_seg)

    # --- post_number ---
    post_number = handle.split("/")[-1]

    # --- Metadata blob ---
    advisor = ";".join(fields.get("heal.advisorName", []))
    committee = ";".join(fields.get("heal.committeeMemberName", []))
    metadata_dict: dict = {
        "posted_date": listed_date,
        "originalFilename": original_filename or None,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "handle": handle,
        "heal_type": _first(fields.get("heal.type", [])) or None,
        "heal_language": _first(fields.get("heal.language", [])) or None,
        "heal_access": _first(fields.get("heal.access", [])) or None,
        "advisor": advisor or None,
        "committee": committee or None,
        "numberOfPages": _first(fields.get("heal.numberOfPages", [])) or None,
        "fullTextAvailability": _first(fields.get("heal.fullTextAvailability", [])) or None,
        "bibliographicCitation": _first(fields.get("heal.bibliographicCitation", [])) or None,
        "healIdentifierSecondary": _first(fields.get("heal.identifier.secondary", [])) or None,
        "heal_classifications": fields.get("heal.classification", []) or None,
    }
    metadata_dict = {k: v for k, v in metadata_dict.items() if v is not None}

    return {
        "id": None,
        "site_id": _SITE_ID,
        "external_id": handle,
        "post_number": post_number,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "published_date": published_date,
        "listed_date": listed_date,
        "publisher": publisher,
        "department": "",
        "journal": "",
        "url": url,
        "pdf_url": pdf_url,
        "doi": doi,
        "keywords": keywords,
        "category": category,
        "original_filename": original_filename,
        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class OlympiasLibUoiGrJspuiCrawler(BaseCrawler):
    """Crawler for University of Ioannina DSpace (doctoralThesis collection)."""

    site_id = "olympias-lib-uoi-gr-jspui"
    site_name = "Custom: olympias-lib-uoi-gr-jspui"
    base_url = "https://olympias.lib.uoi.gr"

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_handles: set[str] = set()
        start = 0
        page = 0
        start_time = time.monotonic()
        lim_str = str(limit) if limit is not None else "inf"

        while True:
            # Wall-clock budget
            if time.monotonic() - start_time > _WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # -- Fetch list page --
            list_url = _LIST_TPL.format(start=start)
            raw_list = _curl_get(list_url)
            if not raw_list:
                print(f"[{self.site_id}] Failed to fetch list at start={start}. Stopping.")
                break

            handles = _extract_handles(raw_list)
            if not handles:
                print(f"[{self.site_id}] No item handles at start={start}. End of results.")
                break

            new_handles = [h for h in handles if h not in seen_handles]
            if not new_handles:
                print(f"[{self.site_id}] All handles on page already seen. Stopping.")
                break

            for h in handles:
                seen_handles.add(h)

            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            # -- Process each item --
            for handle in new_handles:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(self._delay)

                    detail_url = _DETAIL_TPL.format(handle=handle)
                    raw_detail = _curl_get(detail_url)
                    if not raw_detail:
                        print(f"[{self.site_id}] item {handle} failed: empty response")
                        continue

                    parsed = _parse_full_record(raw_detail)
                    paper = _build_paper(handle, parsed)

                    if paper is None:
                        print(f"[{self.site_id}] item {handle}: skipped (no title)")
                        continue

                    abstract = paper.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {handle}: skipped "
                            f"(abstract too short: {len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] item {handle}: skipped "
                            f"(abstract <100 chars: {len(abstract)})"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{lim_str}: "
                        f"{paper['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {handle} failed: {exc}")
                    continue

            start += _RPP

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
