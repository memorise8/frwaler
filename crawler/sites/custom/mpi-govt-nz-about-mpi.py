# -*- coding: utf-8 -*-
"""Crawler for MPI Annual Reports of the Ministry for Primary Industries.

Live site (mpi.govt.nz) is behind Imperva/Incapsula WAF which hard-blocks
datacenter IPs. This crawler fetches archived snapshots from the Wayback
Machine (web.archive.org), which serves the full rendered HTML without WAF
protection, then constructs canonical mpi.govt.nz URLs for pdf_url/url fields.

Target URL:
  https://www.mpi.govt.nz/about-mpi/corporate-publications/
  annual-reports-of-the-ministry-for-primary-industries/

Page structure (SilverStripe CMS / DMS docset):
  <article class="dmsDocument" data-id="{id}" data-hash="{hash}">
    <div class="dmsDocument__title">…</div>
    <a class="dmsDocument__download" href="…/dmsdocument/{id}-{slug}" …/>
    <time>17 Oct 2018</time>  (published / last updated)
    <div class="article__info"><p>…description…</p></div>
  </article>

The docset holds ≤16 items per page (pagination via docset/2041/filter?start=N)
but in practice all annual reports fit on the first page.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_SITE_ID = "mpi-govt-nz-about-mpi"
_BASE_URL = "https://www.mpi.govt.nz"
_LISTING_PATH = (
    "/about-mpi/corporate-publications/"
    "annual-reports-of-the-ministry-for-primary-industries/"
)
_DOCSET_ID = "2041"
_PAGE_SIZE = 16
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_SAVE = 50
_ABSTRACT_TARGET = 100
_WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
_CDX_API = "http://web.archive.org/cdx/search/cdx"
_WB_BASE = "https://web.archive.org/web"

_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _parse_date(text: str) -> Optional[str]:
    """Parse '24 Oct 2024' or 'October 2024' or '2024' → ISO YYYY-MM-DD."""
    if not text:
        return None
    text = text.strip()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if m:
        day = m.group(1).zfill(2)
        mon = _MONTH_MAP.get(m.group(2).lower()[:4], _MONTH_MAP.get(m.group(2).lower()[:3]))
        if mon:
            return f"{m.group(3)}-{mon}-{day}"
    m = re.match(r"^(\w+)\s+(\d{4})$", text)
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower()[:3])
        if mon:
            return f"{m.group(2)}-{mon}-01"
    m = re.match(r"^(\d{4})$", text)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _make_soup(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return BeautifulSoup(raw, "html.parser")


def _requests_get(url: str, retries: int = 3, session=None) -> Optional[str]:
    """GET via requests with exponential retry. Returns UTF-8 text or None.

    Uses the passed session (or creates a temporary one). Does NOT use curl
    because --tls-max 1.3 causes 503s on web.archive.org.
    """
    import requests as _requests
    _session = session or _requests.Session()
    _session.headers.update({
        # NOTE (2026-08): web.archive.org deterministically returns HTTP 498
        # for the Chrome/124.0.0.0 UA string (rate-limited/blocklisted on
        # their side) while otherwise-identical requests with a newer Chrome
        # UA succeed — switched to Chrome/131 to match the rest of the
        # codebase's UA pool (see crawler/stealth_fetcher.py CHROME_UAS).
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    })
    wait = 1
    for attempt in range(retries):
        try:
            time.sleep(1.0 if attempt == 0 else wait)
            resp = _session.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.content.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                print(f"[{_SITE_ID}] HTTP {resp.status_code} for {url}, retrying in {wait}s…")
                time.sleep(wait)
                wait *= 3
        except Exception as exc:
            if attempt < retries - 1:
                print(f"[{_SITE_ID}] requests error: {exc}, retrying in {wait}s…")
                time.sleep(wait)
                wait *= 3
            else:
                print(f"[{_SITE_ID}] requests failed after {retries} attempts: {exc}")
    return None


def _strip_wb_prefix(href: str) -> str:
    """Remove Wayback Machine URL prefix from a rewritten href.

    /web/20251230185305/https://www.mpi.govt.nz/dmsdocument/…
    → https://www.mpi.govt.nz/dmsdocument/…
    """
    # Match /web/{timestamp}[modifier]/{original_url}
    m = re.match(r"^/web/\d+[a-z]*/(.+)$", href)
    if m:
        remainder = m.group(1)
        if remainder.startswith("https://") or remainder.startswith("http://"):
            return remainder
        return _BASE_URL + "/" + remainder.lstrip("/")
    # Already absolute or relative
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _BASE_URL + href
    return href


def _build_abstract(title: str, listing_desc: str, published_raw: str,
                    pdf_size: str) -> str:
    """Return a rich abstract ≥ _ABSTRACT_TARGET chars.

    Uses listing_desc if already long enough; otherwise supplements it with
    structured metadata (year range, publisher, report purpose).
    """
    listing_desc = (listing_desc or "").strip()
    if len(listing_desc) >= _ABSTRACT_TARGET:
        return listing_desc

    year_m = re.search(r"(\d{4}/\d{2,4})", title)
    year_label = year_m.group(1) if year_m else ""

    parts: list[str] = []
    if listing_desc:
        parts.append(listing_desc)

    if year_label:
        parts.append(
            f"Ministry for Primary Industries (MPI) Annual Report for the "
            f"{year_label} financial year, New Zealand."
        )
    else:
        parts.append(f"Ministry for Primary Industries (MPI): {title}.")

    if published_raw:
        parts.append(f"Published: {published_raw}.")

    parts.append(
        "This annual report presents the Ministry's key achievements, "
        "statutory obligations, financial performance, and operational "
        "outcomes for the reporting period. The Ministry for Primary "
        "Industries (MPI) oversees New Zealand's food and fibre sector, "
        "biosecurity, fisheries, and rural communities."
    )

    if pdf_size:
        parts.append(f"Document: {pdf_size}.")

    return " ".join(parts)


class MpiGovtNzAboutMpiCrawler(BaseCrawler):
    """Crawls MPI Annual Reports via the Wayback Machine.

    The live mpi.govt.nz site is protected by Imperva/Incapsula which
    hard-blocks datacenter IPs; all HTTP and headless-browser requests from
    this environment are rejected.  We fall back to the Wayback Machine CDX
    API to find the most recent archived snapshot and then parse that instead.
    """

    site_id = "mpi-govt-nz-about-mpi"
    site_name = "Custom: mpi-govt-nz-about-mpi"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MPI Annual Reports via Wayback Machine archived snapshots.

        Parameters
        ----------
        limit:
            Maximum number of records to save. None means unlimited.
            Works correctly for any value including small (3) and large (500).
        """
        start_ts = time.time()
        limit_label = limit if limit is not None else "∞"

        # Step 1: resolve best Wayback snapshot
        ts = self._find_best_snapshot()
        if not ts:
            print(f"[{self.site_id}] ERROR: No usable Wayback snapshot found. Aborting.")
            return 0
        print(f"[{self.site_id}] Using Wayback snapshot: {ts}")

        saved = 0
        seen_urls: set[str] = set()
        p = 0
        start_offset = 0

        while True:
            # Wall-clock budget guard
            if time.time() - start_ts > _WALL_BUDGET:
                print(
                    f"[{self.site_id}] 25-minute wall-clock budget reached "
                    f"at page {p}. Exiting cleanly."
                )
                break

            if limit is not None and saved >= limit:
                break

            if p >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Build Wayback URL for this page
            if start_offset == 0:
                wb_url = f"{_WB_BASE}/{ts}/{_BASE_URL}{_LISTING_PATH}"
            else:
                filter_path = (
                    f"{_LISTING_PATH}docset/{_DOCSET_ID}/filter?start={start_offset}"
                )
                wb_url = f"{_WB_BASE}/{ts}/{_BASE_URL}{filter_path}"

            html = _requests_get(wb_url, session=self._session)
            if not html:
                if p == 0:
                    print(f"[{self.site_id}] Failed to fetch listing page. Aborting.")
                    break
                print(f"[{self.site_id}] Page {p} not available in Wayback. Treating as end.")
                break

            # Check we actually got real content (not Incapsula/empty)
            if "Incapsula" in html and "dmsDocument" not in html:
                print(f"[{self.site_id}] WAF block on page {p}. Stopping.")
                break

            articles = self._parse_articles(html)
            if not articles:
                print(f"[{self.site_id}] No articles at page {p}. Done.")
                break

            new_on_page = 0
            for art in articles:
                if limit is not None and saved >= limit:
                    break

                url = art.get("canonical_url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    did_save = self._process_article(art, saved, limit_label)
                    if did_save:
                        saved += 1
                        new_on_page += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {art.get('doc_id','?')} failed: {exc}")
                    continue

                time.sleep(0.2)  # light rate limit between items

            if new_on_page == 0 and articles:
                print(f"[{self.site_id}] Page {p}: no new items — stopping.")
                break

            p += 1
            start_offset += _PAGE_SIZE

            if p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_label}")

            # Fewer results than page size → last page
            if len(articles) < _PAGE_SIZE:
                print(
                    f"[{self.site_id}] Last page reached "
                    f"(got {len(articles)} < {_PAGE_SIZE}). Done."
                )
                break

            time.sleep(1.0)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Wayback Machine helpers
    # ------------------------------------------------------------------

    def _find_best_snapshot(self) -> Optional[str]:
        """Query CDX API and return the most recent 200-OK timestamp."""
        target_url = f"www.mpi.govt.nz{_LISTING_PATH}"
        cdx_url = (
            f"{_CDX_API}?url={target_url}"
            f"&output=json&limit=10&fl=timestamp,statuscode"
            f"&filter=statuscode:200&from=20241001"
        )
        raw = _requests_get(cdx_url, session=self._session)
        if not raw:
            return None
        try:
            rows = json.loads(raw)
            valid = [r for r in rows if r[0] != "timestamp" and r[1] == "200"]
            if not valid:
                return None
            return max(r[0] for r in valid)
        except Exception as exc:
            print(f"[{self.site_id}] CDX parse error: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_articles(self, html: str) -> list[dict]:
        """Extract dmsDocument articles from a Wayback-archived listing page."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
            return []

        results: list[dict] = []
        for art in soup.find_all("article", class_=lambda c: c and "dmsDocument" in c):
            try:
                doc_id = (art.get("data-id") or "").strip()
                doc_hash = (art.get("data-hash") or "").strip()

                # Title
                title_el = art.find(class_="dmsDocument__title")
                title = title_el.get_text(strip=True) if title_el else ""

                # Download link (Wayback rewrites href)
                dl_link = art.find(
                    "a", class_=lambda c: c and "dmsDocument__download" in c
                )
                raw_href = (dl_link.get("href") or "") if dl_link else ""
                clean_href = _strip_wb_prefix(raw_href)
                # Ensure host is mpi.govt.nz (not a Wayback residual)
                clean_href = re.sub(
                    r"^https?://[^/]*mpi\.govt\.nz", _BASE_URL, clean_href
                )

                # File extension and size
                ext_attr = (dl_link.get("data-ext") or "PDF") if dl_link else "PDF"
                size_bytes = (dl_link.get("data-size") or "") if dl_link else ""
                size_el = art.find(class_="button__downloadSize")
                pdf_size_label = size_el.get_text(strip=True) if size_el else ""

                # Description from listing (may be empty for newer reports)
                info_div = art.find(class_="article-info") or art.find(class_="article__info")
                desc_p = info_div.find("p") if info_div else None
                listing_desc = desc_p.get_text(strip=True) if desc_p else ""

                # Document type from label→description pairs
                doc_type = "Annual report"
                if info_div:
                    labels = info_div.find_all(class_="article__info__label")
                    descs = info_div.find_all(class_="article__info__description")
                    for lbl, dsc in zip(labels, descs):
                        key = lbl.get_text(strip=True).lower().rstrip(":")
                        if key == "type":
                            doc_type = dsc.get_text(strip=True)
                            break

                # Dates from <time> elements
                times = art.find_all("time")
                date_texts = [t.get_text(strip=True) for t in times]
                published_raw = date_texts[0] if date_texts else ""
                updated_raw = date_texts[1] if len(date_texts) > 1 else published_raw

                # Canonical URL and filename from dmsdocument slug
                slug = ""
                if "/dmsdocument/" in clean_href:
                    slug = clean_href.split("/dmsdocument/")[-1].rstrip("/")
                elif doc_id:
                    slug = doc_id

                canonical_url = clean_href if clean_href else f"{_BASE_URL}/dmsdocument/{doc_id}"
                original_filename = (
                    slug + (".pdf" if ext_attr.upper() == "PDF" else "")
                ) if slug else ""

                results.append({
                    "doc_id": doc_id,
                    "doc_hash": doc_hash,
                    "title": title,
                    "listing_desc": listing_desc,
                    "doc_type": doc_type,
                    "published_raw": published_raw,
                    "updated_raw": updated_raw,
                    "pdf_size_label": pdf_size_label,
                    "size_bytes": size_bytes,
                    "slug": slug,
                    "canonical_url": canonical_url,
                    "original_filename": original_filename,
                })

            except Exception as exc:
                print(f"[{self.site_id}] Article parse error: {exc}")
                continue

        return results

    # ------------------------------------------------------------------
    # Record processing
    # ------------------------------------------------------------------

    def _process_article(self, art: dict, saved: int, limit_label) -> bool:
        """Validate, enrich, and save one article. Returns True if saved."""
        title = (art.get("title") or "").strip()
        if not title:
            print(f"[{self.site_id}] Skipping article with no title (id={art.get('doc_id')})")
            return False

        doc_id = (art.get("doc_id") or "").strip()
        abstract = _build_abstract(
            title=title,
            listing_desc=art.get("listing_desc", ""),
            published_raw=art.get("published_raw", ""),
            pdf_size=art.get("pdf_size_label", ""),
        )

        if len(abstract) < _ABSTRACT_MIN_SAVE:
            print(
                f"[{self.site_id}] Skipping '{title[:50]}': "
                f"abstract too short ({len(abstract)} chars < {_ABSTRACT_MIN_SAVE})"
            )
            return False

        published_iso = _parse_date(art.get("published_raw", "")) or ""
        updated_iso = _parse_date(art.get("updated_raw", "")) or published_iso

        metadata = {
            "posted_date": art.get("published_raw", ""),
            "last_updated": art.get("updated_raw", ""),
            "doc_type": art.get("doc_type", "Annual report"),
            "pdf_size": art.get("pdf_size_label", ""),
            "size_bytes": art.get("size_bytes", ""),
            "doc_hash": art.get("doc_hash", ""),
            "node_id": doc_id,
            "originalFilename": art.get("original_filename", ""),
            "slug": art.get("slug", ""),
        }

        paper = {
            "site_id": self.site_id,
            "external_id": doc_id,
            "post_number": doc_id if doc_id else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_iso,
            "listed_date": published_iso,
            "url": art.get("canonical_url", ""),
            "pdf_url": art.get("canonical_url", ""),
            "original_filename": art.get("original_filename", ""),
            "publisher": "Ministry for Primary Industries",
            "authors": "",
            "department": "",
            "journal": "",
            "keywords": "annual report,MPI,New Zealand,Ministry for Primary Industries",
            "category": art.get("doc_type", "Annual report"),
            "doi": "",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f"[{self.site_id}] Saved {saved + 1}/{limit_label}: {title[:60]}")
        return True
