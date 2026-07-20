# -*- coding: utf-8 -*-
"""Geoscience Australia website search crawler.

Starting URL: https://www.ga.gov.au/search?from=0&query=pdf&index=geoscience_site_crawl

Architecture:
  1. Playwright renders GA search page (React/Funnelback) → extracts eCat UUIDs.
  2. For each UUID, fetches eCat ISO 19115-3 XML via requests.
  3. Parses XML → saves via _save_paper().

Pagination: ?from=0, ?from=10, ?from=20 … (10 results/page, ~950 total).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from xml.etree import ElementTree as ET

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "ga-gov-au-search"
_BASE = "https://www.ga.gov.au"
_SEARCH_URL = f"{_BASE}/search"
_SEARCH_QUERY = "pdf"
_SEARCH_INDEX = "geoscience_site_crawl"
_PAGE_SIZE = 10
_ECAT_XML_URL = "https://ecat.ga.gov.au/geonetwork/srv/api/records/{uuid}/formatters/xml"
_ECAT_DETAIL_BASE = "https://ecat.ga.gov.au/geonetwork/srv/eng/catalog.search#/metadata/{uuid}"
_MIN_ABSTRACT = 100   # skip items whose abstract is shorter than this
_MAX_PAGES = 200
_MAX_SECONDS = 25 * 60   # 25-minute wall-clock budget
_BACKOFF = (1, 3, 9)

# ISO 19115-3 XML namespaces
_NS = {
    "mdb": "http://standards.iso.org/iso/19115/-3/mdb/2.0",
    "mri": "http://standards.iso.org/iso/19115/-3/mri/1.0",
    "gco": "http://standards.iso.org/iso/19115/-3/gco/1.0",
    "cit": "http://standards.iso.org/iso/19115/-3/cit/2.0",
    "mcc": "http://standards.iso.org/iso/19115/-3/mcc/1.0",
    "mrd": "http://standards.iso.org/iso/19115/-3/mrd/1.0",
}


class GAGovAuSearchCrawler(BaseCrawler):
    """Crawler for Geoscience Australia website search (ga.gov.au/search)."""

    site_id = _SITE_ID
    site_name = "Custom: ga-gov-au-search"
    base_url = _BASE

    def crawl(self, limit=None):
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            print(f"[{_SITE_ID}] ERROR: playwright not installed", file=sys.stderr)
            return 0

        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "∞"
        page_num = 0

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    viewport={"width": 1280, "height": 800},
                )
                bpage = context.new_page()

                for page_num in range(_MAX_PAGES):
                    # --- guards ---
                    if limit is not None and saved >= limit:
                        break
                    elapsed = time.time() - start_time
                    if elapsed > _MAX_SECONDS:
                        print(
                            f"[{_SITE_ID}] wall-clock budget exceeded at page "
                            f"{page_num}; stopping"
                        )
                        break

                    if page_num % 10 == 0:
                        print(
                            f"[{_SITE_ID}] page {page_num}: "
                            f"saved {saved}/{limit_display}"
                        )

                    from_idx = page_num * _PAGE_SIZE
                    url = (
                        f"{_SEARCH_URL}?from={from_idx}"
                        f"&query={_SEARCH_QUERY}&index={_SEARCH_INDEX}"
                    )

                    # --- navigate ---
                    try:
                        bpage.goto(url, wait_until="networkidle", timeout=35000)
                    except Exception as exc:
                        print(f"[{_SITE_ID}] goto error page {page_num}: {exc}")
                        time.sleep(3)
                        continue

                    time.sleep(3)  # let React finish rendering

                    # --- extract result containers ---
                    containers = bpage.query_selector_all(
                        ".standard-search-result__container"
                    )
                    if not containers:
                        print(
                            f"[{_SITE_ID}] page {page_num}: "
                            "no result containers; stopping"
                        )
                        break

                    items = _extract_items(bpage, containers)
                    if not items:
                        print(
                            f"[{_SITE_ID}] page {page_num}: "
                            "no parseable items; stopping"
                        )
                        break

                    new_items = [
                        i for i in items
                        if i.get("uuid") and i["detail_url"] not in seen_urls
                    ]
                    if not new_items:
                        print(
                            f"[{_SITE_ID}] page {page_num}: "
                            "all items already seen (pagination loop); stopping"
                        )
                        break

                    for item in new_items:
                        if limit is not None and saved >= limit:
                            break

                        seen_urls.add(item["detail_url"])

                        try:
                            meta = _fetch_ecat_xml(item["uuid"], self._session)
                            if meta is None:
                                print(
                                    f"[{_SITE_ID}] eCat fetch failed "
                                    f"for {item['uuid']}; skipping"
                                )
                                continue

                            abstract = meta.get("abstract") or ""
                            # fallback to search-page snippet when eCat abstract is short
                            if len(abstract) < _MIN_ABSTRACT:
                                snip = item.get("snippet", "")
                                if len(snip) > len(abstract):
                                    abstract = snip

                            if len(abstract) < _MIN_ABSTRACT:
                                print(
                                    f"[{_SITE_ID}] skip {item['uuid']}: "
                                    f"abstract too short ({len(abstract)} chars)"
                                )
                                continue

                            title = (
                                meta.get("title") or item.get("title") or "(untitled)"
                            )
                            pdf_url = meta.get("pdf_url") or None
                            orig_filename = None
                            if pdf_url:
                                path_part = urllib.parse.urlparse(pdf_url).path
                                fname = urllib.parse.unquote(
                                    path_part.split("/")[-1]
                                )
                                orig_filename = fname if fname else None

                            self._save_paper({
                                "site_id": _SITE_ID,
                                "external_id": item["uuid"],
                                "post_number": item["uuid"],
                                "title": title,
                                "abstract": abstract,
                                "published_date": meta.get("published_date"),
                                "posted_date": meta.get("listed_date"),
                                "authors": meta.get("authors"),
                                "publisher": (
                                    meta.get("publisher")
                                    or "Geoscience Australia"
                                ),
                                "department": None,
                                "journal": None,
                                "url": item["detail_url"],
                                "pdf_url": pdf_url,
                                "keywords": meta.get("keywords"),
                                "category": meta.get("category"),
                                "doi": None,
                                "original_filename": orig_filename,
                                "metadata": json.dumps(
                                    {
                                        "posted_date": meta.get("listed_date"),
                                        "originalFilename": orig_filename,
                                        "uuid": item["uuid"],
                                        "display_url": item.get("display_url"),
                                        "ecat_id": meta.get("ecat_id"),
                                        "search_query": _SEARCH_QUERY,
                                        "search_index": _SEARCH_INDEX,
                                    },
                                    ensure_ascii=False,
                                ),
                            })
                            saved += 1
                            print(
                                f"[{_SITE_ID}] saved {saved}/{limit_display}: "
                                f"{title[:60]}"
                            )

                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            print(
                                f"[{_SITE_ID}] item {item.get('uuid')} "
                                f"failed: {exc}"
                            )
                            continue

                        time.sleep(self._delay)

                if page_num == _MAX_PAGES - 1:
                    print(
                        f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached"
                    )

                browser.close()

        except KeyboardInterrupt:
            print(f"[{_SITE_ID}] interrupted after {saved} records")
            raise

        print(f"[{_SITE_ID}] done: saved {saved}")
        return saved


# ---------------------------------------------------------------------------
# Extract search result items from Playwright-rendered DOM
# ---------------------------------------------------------------------------

def _extract_items(page, containers) -> list[dict]:
    """Extract eCat UUIDs and metadata from rendered search result containers."""
    items = []
    for el in containers:
        try:
            title_el = el.query_selector(".standard-search-result__title")
            if not title_el:
                continue
            title = title_el.inner_text().strip()
            href = title_el.get_attribute("href") or ""

            # UUID is URL-encoded inside the Funnelback redirect href
            uuid = None
            m = re.search(r"url=([^&]+)", href)
            if m:
                decoded = urllib.parse.unquote(m.group(1))
                um = re.search(r"/metadata/([a-f0-9-]{36})", decoded)
                if um:
                    uuid = um.group(1)

            if not uuid:
                continue

            disp_el = el.query_selector(".standard-search-result__display-url")
            display_url = (
                disp_el.inner_text().strip()
                if disp_el
                else f"https://pid.geoscience.gov.au/dataset/ga/{uuid}"
            )

            p_el = el.query_selector("p")
            snippet = (
                re.sub(r"\s+", " ", p_el.inner_text().strip()) if p_el else ""
            )

            detail_url = _ECAT_DETAIL_BASE.format(uuid=uuid)
            items.append({
                "uuid": uuid,
                "title": title,
                "display_url": display_url,
                "detail_url": detail_url,
                "snippet": snippet,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] item extract error: {exc}")
            continue
    return items


# ---------------------------------------------------------------------------
# eCat metadata fetch and XML parse
# ---------------------------------------------------------------------------

def _fetch_ecat_xml(uuid: str, session) -> dict | None:
    """Fetch eCat ISO 19115-3 XML and return parsed metadata dict, or None."""
    url = _ECAT_XML_URL.format(uuid=uuid)
    for attempt in range(3):
        try:
            resp = session.get(
                url, timeout=30,
                headers={"Accept": "application/xml,text/xml,*/*"},
            )
            resp.raise_for_status()
            raw = resp.content
            # Strip UTF-8 BOM (0xEF 0xBB 0xBF) which trips up ElementTree
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            xml_text = raw.decode("utf-8", errors="replace")
            return _parse_ecat_xml(xml_text, uuid)
        except Exception as exc:
            print(
                f"[{_SITE_ID}] eCat XML attempt {attempt + 1}/3 "
                f"for {uuid}: {exc}"
            )
            if attempt < 2:
                time.sleep(_BACKOFF[attempt])

    # Fallback: try via curl (bypasses session/TLS quirks)
    return _fetch_ecat_xml_curl(uuid)


def _fetch_ecat_xml_curl(uuid: str) -> dict | None:
    """Fetch eCat XML via curl as a fallback when requests fails."""
    import subprocess
    url = _ECAT_XML_URL.format(uuid=uuid)
    cmd = [
        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
        "-H", "Accept: application/xml,text/xml,*/*",
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=40)
            raw = result.stdout
            if not raw or not raw.strip():
                raise ValueError("empty response")
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            xml_text = raw.decode("utf-8", errors="replace")
            return _parse_ecat_xml(xml_text, uuid)
        except Exception as exc:
            print(
                f"[{_SITE_ID}] eCat curl attempt {attempt + 1}/3 "
                f"for {uuid}: {exc}"
            )
            if attempt < 2:
                time.sleep(_BACKOFF[attempt])
    return None


def _parse_ecat_xml(xml_text: str, uuid: str) -> dict:
    """Parse ISO 19115-3 XML into a flat metadata dict."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(f"[{_SITE_ID}] XML parse error for {uuid}: {exc}")
        return {}

    def text(el, path):
        r = el.find(path, _NS)
        return r.text.strip() if r is not None and r.text else None

    result: dict = {}

    # Title
    result["title"] = text(
        root,
        ".//mri:MD_DataIdentification/mri:citation"
        "/cit:CI_Citation/cit:title/gco:CharacterString",
    )

    # Abstract
    result["abstract"] = (
        text(
            root,
            ".//mri:MD_DataIdentification/mri:abstract/gco:CharacterString",
        )
        or ""
    )

    # Dates: pick first publication and first revision dates
    pub_date = None
    listed_date = None
    for d in root.findall(".//cit:CI_Date", _NS):
        dt = text(d, "cit:date/gco:DateTime") or text(d, "cit:date/gco:Date")
        if not dt:
            continue
        dt_short = dt[:10]
        dtype_el = d.find("cit:dateType/cit:CI_DateTypeCode", _NS)
        dtype = dtype_el.get("codeListValue") if dtype_el is not None else None
        if dtype == "publication" and pub_date is None:
            pub_date = dt_short
        elif dtype == "revision" and listed_date is None:
            listed_date = dt_short
    result["published_date"] = pub_date
    result["listed_date"] = listed_date

    # Keywords (comma-separated)
    kws = [
        k.text.strip()
        for k in root.findall(".//mri:keyword/gco:CharacterString", _NS)
        if k.text and k.text.strip()
    ]
    result["keywords"] = ",".join(kws) if kws else None

    # Topic category
    cats = [
        c.get("codeListValue")
        for c in root.findall(
            ".//mri:topicCategory/mri:MD_TopicCategoryCode", _NS
        )
    ]
    result["category"] = next((c for c in cats if c), None)

    # Authors (semi-colon separated) and publisher
    authors: list[str] = []
    publisher = None
    for c in root.findall(".//cit:CI_Responsibility", _NS):
        role_el = c.find(".//cit:CI_RoleCode", _NS)
        role = role_el.get("codeListValue") if role_el is not None else None
        org = text(c, ".//cit:name/gco:CharacterString")
        ind = text(c, ".//cit:individualName/gco:CharacterString")
        if role == "author":
            a = org or ind
            if a:
                authors.append(a)
        elif role in ("owner", "publisher", "pointOfContact") and publisher is None:
            if org and "Geoscience" in org:
                publisher = org
    result["authors"] = ";".join(authors) if authors else None
    result["publisher"] = (
        publisher or "Commonwealth of Australia (Geoscience Australia)"
    )

    # Distribution / download URL
    pdf_url = None
    for d in root.findall(".//mrd:MD_DigitalTransferOptions/mrd:onLine", _NS):
        u = text(
            d, ".//cit:CI_OnlineResource/cit:linkage/gco:CharacterString"
        )
        if u and pdf_url is None:
            pdf_url = u
    result["pdf_url"] = pdf_url

    # Numeric eCat ID (used in metadata for reference)
    ecat_id = None
    for ident in root.findall(
        ".//mcc:MD_Identifier/mcc:code/gco:CharacterString", _NS
    ):
        if ident.text and re.match(r"^\d+$", ident.text.strip()):
            ecat_id = ident.text.strip()
            break
    result["ecat_id"] = ecat_id

    return result
