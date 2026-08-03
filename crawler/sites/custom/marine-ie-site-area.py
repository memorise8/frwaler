# -*- coding: utf-8 -*-
"""Marine Institute Ireland — Annual Reports / Publications crawler.

Starting URL : https://www.marine.ie/site-area/publications/annual-reports

Site structure discovered by live inspection (curl, no browser/JS needed):

- The "annual-reports" page is NOT a paginated Drupal Views listing — it is a
  single static content page (a WYSIWYG ``body`` field) that lists every
  Annual Report / Annual Impact Report / Year in Review edition as a run of
  ``<a><img></a>`` cover-image links. ``?page=N`` query params are accepted
  by Drupal but return byte-identical content (verified via diff), so there
  is nothing to paginate — the pagination loop below still walks pages for
  robustness/spec-compliance, but it naturally stops after page 0 because
  every item is already in ``seen`` by the time page 1 is fetched.
- Most entries link to the Marine Institute's Open Access Repository (OAR),
  which runs DSpace 7 (Angular Universal SSR). Rather than scrape the
  server-rendered HTML (which we found to be unreliable — some items render
  a bare app-shell with no content, e.g. handle 10793/1186), we resolve each
  entry straight through DSpace's public REST API:
    * ``GET /server/api/pid/find?id=hdl:{handle}`` (302 -> item resource) to
      turn a ``handle.net``/``oar.marine.ie/handle`` link into an item UUID,
      or ``GET /server/api/core/items/{uuid}`` directly for the newer
      ``oar.marine.ie/items/{uuid}`` style links.
    * The item JSON exposes clean ``dc.*`` Dublin Core metadata
      (title, description.abstract, date.issued, contributor.author,
      publisher, subject, type, identifier.uri, ...).
    * ``GET {item}/bundles`` -> the ``ORIGINAL`` bundle -> its bitstreams give
      the PDF's real original filename and download URL directly (no need
      to download the PDF or inspect Content-Disposition headers).
  A handful of the oldest editions (2008-2014) link straight to a PDF
  bitstream URL with no DSpace item/handle behind them at all; those have no
  abstract available anywhere and are skipped per the "short abstract" rule.
"""

import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "marine-ie-site-area"
_BASE_URL = "https://www.marine.ie"
_LIST_URL = "https://www.marine.ie/site-area/publications/annual-reports"
_OAR_BASE = "https://oar.marine.ie"
_PUBLISHER_DEFAULT = "Marine Institute"

_HANDLE_RE = re.compile(r"(?:oar\.marine\.ie|hdl\.handle\.net)/(?:handle/)?(\d+/\d+)(?:[/?#]|$)")
_ITEMS_RE = re.compile(r"oar\.marine\.ie/items/([0-9a-fA-F-]{36})")
_RETRY_SLEEPS = (1, 3, 9)


# ---------------------------------------------------------------------------
# Helpers (module-level, keep re-usable without self)
# ---------------------------------------------------------------------------

def _make_soup(html):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _decode_body(response):
    """Return response text, tolerating bad/mixed encodings."""
    try:
        return response.text
    except Exception:
        try:
            return response.content.decode("utf-8", errors="replace")
        except Exception:
            return ""


def _parse_year_or_date(raw):
    """DSpace ``dc.date.issued``/``dc.date.available`` value -> ISO date.

    Accepts full timestamps ("2025-03-26T12:22:25Z"), plain dates
    ("2025-03-26"), or bare years ("2025"). Returns None if unparsable
    (never raises).
    """
    if not raw:
        return None
    raw = raw.strip()
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    for fmt, out in (("%Y-%m-%d", "%Y-%m-%d"), ("%Y-%m", "%Y-%m-01"), ("%Y", "%Y-01-01")):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime(out)
        except ValueError:
            continue
    return None


def _extract_entries(html):
    """Parse the annual-reports listing page into ordered, deduped entries.

    Returns a list of dicts: {"key": (kind, id), "href": str, "title": str}
    in document order (newest first, matching the page's own layout).
    """
    entries = []
    seen_keys = set()
    soup = _make_soup(html)
    if soup is None:
        return entries

    main = soup.find("main", id="block-mainpagecontent") or soup.find(attrs={"role": "main"}) or soup
    node_content = main.find("div", class_="node__content") or main

    for a in node_content.find_all("a", href=True):
        href = a["href"]
        m = _HANDLE_RE.search(href)
        m2 = _ITEMS_RE.search(href)
        if m:
            key = ("handle", m.group(1))
        elif m2:
            key = ("uuid", m2.group(1))
        elif "oar.marine.ie/bitstream/" in href:
            key = ("bitstream", href)
        else:
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)

        title = a.get("title")
        if not title:
            img = a.find("img")
            title = (img.get("alt") if img else None) or None
        title = title.strip() if isinstance(title, str) else None

        entries.append({"key": key, "href": href, "title": title or None})

    return entries


def _pick_pdf_from_bundle_bitstreams(bitstreams):
    """Choose the best PDF bitstream from an ORIGINAL bundle's bitstream list.

    Prefers an English-looking filename over an Irish/Gaeilge one; falls
    back to the first bitstream that looks like a PDF, then to the first
    bitstream at all.
    """
    if not bitstreams:
        return None, None

    def is_irish(name):
        n = (name or "").lower()
        return "irish" in n or "gaeilge" in n or "bhliant" in n or "tuarasc" in n

    pdf_candidates = [b for b in bitstreams if (b.get("name") or "").lower().endswith(".pdf")]
    candidates = pdf_candidates or bitstreams

    non_irish = [b for b in candidates if not is_irish(b.get("name"))]
    chosen = (non_irish or candidates)[0]

    name = chosen.get("name")
    href = (chosen.get("_links", {}) or {}).get("content", {}).get("href")
    return href, name


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MarineIeSiteAreaCrawler(BaseCrawler):
    site_id = "marine-ie-site-area"
    site_name = "Custom: marine-ie-site-area"
    base_url = "https://www.marine.ie"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)

    # -- low level HTTP helpers -------------------------------------------------

    def _get(self, url, *, retries=3, **kwargs):
        """GET with manual 1s/3s/9s exponential-backoff retries.

        Returns the ``requests.Response`` on success (2xx after redirects),
        or ``None`` after all retries are exhausted.
        """
        for attempt in range(retries):
            try:
                resp = self._session.get(url, timeout=30, **kwargs)
                if resp.status_code < 400:
                    return resp
                print(f"[{_SITE_ID}] HTTP {resp.status_code} for {url} (attempt {attempt + 1}/{retries})")
            except Exception as exc:
                print(f"[{_SITE_ID}] request error for {url} (attempt {attempt + 1}/{retries}): {exc}")
            if attempt < retries - 1:
                wait = _RETRY_SLEEPS[min(attempt, len(_RETRY_SLEEPS) - 1)]
                time.sleep(wait)
        return None

    def _get_json(self, url):
        resp = self._get(url, allow_redirects=True)
        if resp is None:
            return None
        try:
            return resp.json()
        except Exception as exc:
            print(f"[{_SITE_ID}] bad JSON from {url}: {exc}")
            return None

    # -- DSpace item resolution --------------------------------------------------

    def _resolve_item(self, key):
        """Resolve a listing entry key -> DSpace item JSON dict, or None."""
        kind, ident = key
        if kind == "handle":
            url = f"{_OAR_BASE}/server/api/pid/find?id=hdl:{ident}"
        elif kind == "uuid":
            url = f"{_OAR_BASE}/server/api/core/items/{ident}"
        else:
            return None
        return self._get_json(url)

    def _fetch_pdf_info(self, item):
        """Return (pdf_url, original_filename) for an item, or (None, None)."""
        links = item.get("_links", {}) or {}
        bundles_url = (links.get("bundles") or {}).get("href")
        if not bundles_url:
            bundles_url = f"{_OAR_BASE}/server/api/core/items/{item.get('uuid')}/bundles"

        bundles_json = self._get_json(bundles_url)
        if not bundles_json:
            return None, None

        for bundle in bundles_json.get("_embedded", {}).get("bundles", []):
            if bundle.get("name") != "ORIGINAL":
                continue
            bitstreams_url = (bundle.get("_links", {}) or {}).get("bitstreams", {}).get("href")
            if not bitstreams_url:
                continue
            bs_json = self._get_json(bitstreams_url)
            if not bs_json:
                continue
            bitstreams = bs_json.get("_embedded", {}).get("bitstreams", [])
            return _pick_pdf_from_bundle_bitstreams(bitstreams)

        return None, None

    @staticmethod
    def _meta_values(item, field):
        vals = item.get("metadata", {}).get(field) or []
        return [v.get("value", "") for v in vals if v.get("value")]

    def _build_paper(self, entry, item, pdf_url, original_filename):
        handle = item.get("handle")
        uuid_ = item.get("uuid")

        title = (self._meta_values(item, "dc.title") or [None])[0] or entry.get("title") or "(untitled)"

        abstract_vals = self._meta_values(item, "dc.description.abstract")
        abstract = abstract_vals[0] if abstract_vals else ""
        if len(abstract) < 50:
            desc_vals = self._meta_values(item, "dc.description")
            citation_vals = self._meta_values(item, "dc.identifier.citation")
            fallback = " ".join([v for v in (desc_vals + citation_vals) if v])
            if len(fallback) > len(abstract):
                abstract = fallback

        date_issued = (self._meta_values(item, "dc.date.issued") or [None])[0]
        published_date = _parse_year_or_date(date_issued)

        date_available_raw = (self._meta_values(item, "dc.date.available") or [None])[0]
        date_accessioned_raw = (self._meta_values(item, "dc.date.accessioned") or [None])[0]
        posted_raw = date_available_raw or date_accessioned_raw
        listed_date = _parse_year_or_date(posted_raw) or published_date

        authors = "; ".join(self._meta_values(item, "dc.contributor.author"))
        publisher_vals = self._meta_values(item, "dc.publisher")
        publisher = "; ".join(publisher_vals) if publisher_vals else _PUBLISHER_DEFAULT

        keywords_vals = self._meta_values(item, "dc.subject")
        keywords = ", ".join(keywords_vals)

        category_vals = self._meta_values(item, "dc.type")
        category = category_vals[0] if category_vals else None

        doi_vals = self._meta_values(item, "dc.identifier.doi")
        doi = doi_vals[0] if doi_vals else None

        uri_vals = self._meta_values(item, "dc.identifier.uri")
        url = uri_vals[0] if uri_vals else entry.get("href")

        external_id = handle or uuid_
        post_number = None
        if handle and "/" in handle:
            tail = handle.split("/")[-1]
            post_number = tail if tail.isdigit() else handle
        elif uuid_:
            post_number = uuid_

        series_vals = self._meta_values(item, "dc.relation.ispartofseries")
        volume_vals = self._meta_values(item, "dc.relation.volume")
        issue_vals = self._meta_values(item, "dc.relation.issue")
        journal_vals = self._meta_values(item, "dc.relation.ispartof") or self._meta_values(item, "dc.source")

        metadata = {
            "posted_date": posted_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_vals[0] if journal_vals else None,
            "series": series_vals[0] if series_vals else None,
            "volume": volume_vals[0] if volume_vals else None,
            "issue": issue_vals[0] if issue_vals else None,
            "handle": handle,
            "item_uuid": uuid_,
            "dc_type": category_vals[0] if category_vals else None,
            "dc_date_issued_raw": date_issued,
        }

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": authors or None,
            "publisher": publisher,
            "department": None,
            "journal": journal_vals[0] if journal_vals else None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords or None,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # -- main crawl loop -----------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_keys = set()
        limit_display = limit if limit is not None else "inf"
        start_time = time.monotonic()
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        page = 0

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page >= MAX_PAGES:
                    print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                    break
                elapsed = time.monotonic() - start_time
                if elapsed > MAX_SECONDS:
                    print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                    break

                list_url = _LIST_URL if page == 0 else f"{_LIST_URL}?page={page}"
                if page > 0:
                    time.sleep(self._delay)

                resp = self._get(list_url)
                if resp is None:
                    print(f"[{_SITE_ID}] Failed to fetch listing page {page + 1} ({list_url}). Stopping.")
                    break

                html = _decode_body(resp)
                entries = _extract_entries(html)
                if not entries:
                    print(f"[{_SITE_ID}] No records found on page {page + 1}. Done.")
                    break

                new_entries = [e for e in entries if e["key"] not in seen_keys]
                if not new_entries:
                    print(f"[{_SITE_ID}] All entries on page {page + 1} already seen. Stopping.")
                    break
                for e in new_entries:
                    seen_keys.add(e["key"])

                if (page + 1) % 10 == 0:
                    print(f"[{_SITE_ID}] page {page + 1}: saved {saved}/{limit_display}")

                for entry in new_entries:
                    if limit is not None and saved >= limit:
                        break
                    if time.monotonic() - start_time > MAX_SECONDS:
                        print(f"[{_SITE_ID}] 25-minute budget reached mid-page. Stopping.")
                        break

                    kind, ident = entry["key"]
                    try:
                        if kind == "bitstream":
                            # No DSpace item/handle behind this link at all (very
                            # old editions) — no abstract is obtainable, skip.
                            print(f"[{_SITE_ID}] {entry['href']} has no repository "
                                  f"item (direct PDF link); abstract unavailable. Skipping.")
                            continue

                        time.sleep(self._delay)
                        item = self._resolve_item(entry["key"])
                        if not item or "uuid" not in item:
                            print(f"[{_SITE_ID}] Could not resolve item for {entry['href']}. Skipping.")
                            continue

                        pdf_url, original_filename = self._fetch_pdf_info(item)

                        paper = self._build_paper(entry, item, pdf_url, original_filename)

                        if not paper["abstract"] or len(paper["abstract"]) < 50:
                            print(f"[{_SITE_ID}] Abstract <50 chars for {entry['href']}. Skipping.")
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{_SITE_ID}] Saved {saved}/{limit_display}: {paper['title'][:70]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{_SITE_ID}] item {entry.get('href', '?')} failed: {exc}")
                        continue

                page += 1
        except KeyboardInterrupt:
            raise

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
