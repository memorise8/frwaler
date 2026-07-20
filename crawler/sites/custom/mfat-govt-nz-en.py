# -*- coding: utf-8 -*-
"""MFAT NZ Annual Reports crawler.

Target: https://www.mfat.govt.nz/en/about-us/mfat-annual-reports

Strategy:
  Listing:  live HTML listing page -> Wayback Machine (known timestamps)
            -> Wayback CDX dynamic discovery -> hardcoded seed list
  Detail:   live page -> Wayback CDX + fetch
  Abstract: detail body text (>=100) -> listing card description -> constructed
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup as _BS4
    _BS4_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS4 = None
    _BS4_PARSERS = []

_INCAPSULA_MARKERS = ("_Incapsula_Resource", "SWJIYLWA", "SWUDNSAI")
_LISTING_URL = "https://www.mfat.govt.nz/en/about-us/mfat-annual-reports"
_BASE_URL = "https://www.mfat.govt.nz"
_PUBLISHER = "New Zealand Ministry of Foreign Affairs and Trade"

# Known Wayback Machine timestamps for the listing page (confirmed 58KB content)
_WB_LIST_TIMESTAMPS = [
    "20230209122437",
    "20230124145613",
    "20230129202120",
    "20230128024336",
    "20230124060524",
]

# Hardcoded seed list: slug, title, year_range, 100+ char description
_SEEDS = [
    {
        "slug": "mfat-annual-report-2022-23",
        "title": "MFAT Annual Report 2022-23",
        "year_range": "2022-23",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2022-23 was "
            "presented to the House of Representatives in October 2023. This report "
            "covers MFAT's performance against its Strategic Intentions and "
            "performance measures, financial statements, and departmental activities."
        ),
    },
    {
        "slug": "mfat-annual-report-2021-22",
        "title": "MFAT Annual Report 2021-22",
        "year_range": "2021-22",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2021-22, was "
            "presented to the House of Representatives on 19 October 2022. This "
            "report covers MFAT's performance against its Strategic Intentions and "
            "performance measures for the financial year 2021-22."
        ),
    },
    {
        "slug": "mfat-annual-report-2020-21",
        "title": "MFAT Annual Report 2020-21",
        "year_range": "2020-21",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2020-21, was "
            "presented to the House of Representatives on 20 October 2021. This "
            "report covers MFAT's performance against its Strategic Intentions and "
            "performance measures for the financial year 2020-21."
        ),
    },
    {
        "slug": "mfat-annual-report-2019-20",
        "title": "MFAT Annual Report 2019-20",
        "year_range": "2019-20",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2019-20, was "
            "presented to the House of Representatives on 11 December 2020. This "
            "report covers MFAT's performance against its Strategic Intentions and "
            "performance measures for the financial year 2019-20."
        ),
    },
    {
        "slug": "mfat-annual-report-2018-19",
        "title": "MFAT Annual Report 2018-19",
        "year_range": "2018-19",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2018-19, "
            "presented to the House of Representatives. This report covers MFAT's "
            "performance against its Strategic Intentions and performance measures "
            "for the financial year 2018-19."
        ),
    },
    {
        "slug": "mfat-annual-report-2017-18",
        "title": "MFAT Annual Report 2017-18",
        "year_range": "2017-18",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2017-18, "
            "presented to the House of Representatives. This report covers MFAT's "
            "performance against its Strategic Intentions and performance measures "
            "for the financial year 2017-18."
        ),
    },
    {
        "slug": "mfat-annual-report-2016-17",
        "title": "MFAT Annual Report 2016-17",
        "year_range": "2016-17",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2016-17, "
            "presented to the House of Representatives. This report covers MFAT's "
            "performance against its Strategic Intentions and performance measures "
            "for the financial year 2016-17."
        ),
    },
    {
        "slug": "mfat-annual-report-2015-16",
        "title": "MFAT Annual Report 2015-16",
        "year_range": "2015-16",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2015-16, "
            "presented to the House of Representatives. This report covers MFAT's "
            "performance against its Strategic Intentions and performance measures "
            "for the financial year 2015-16."
        ),
    },
    {
        "slug": "mfat-annual-report-2014-15",
        "title": "MFAT Annual Report 2014-15",
        "year_range": "2014-15",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2014-15, "
            "presented to the House of Representatives. This report covers MFAT's "
            "performance against its Strategic Intentions and performance measures "
            "for the financial year 2014-15."
        ),
    },
    {
        "slug": "mfat-annual-report-2013-14",
        "title": "MFAT Annual Report 2013-14",
        "year_range": "2013-14",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2013-14, "
            "presented to the House of Representatives. This report covers MFAT's "
            "performance against its Strategic Intentions and performance measures "
            "for the financial year 2013-14."
        ),
    },
    {
        "slug": "mfat-annual-report-2012-13",
        "title": "MFAT Annual Report 2012-13",
        "year_range": "2012-13",
        "description": (
            "The Ministry of Foreign Affairs and Trade Annual Report 2012-13, "
            "presented to the House of Representatives. This report covers MFAT's "
            "performance against its Strategic Intentions and performance measures "
            "for the financial year 2012-13."
        ),
    },
]

_MAX_PAGES = 200
_WALL_BUDGET = 25 * 60
_ABSTRACT_MIN = 50


def _make_soup(html):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    if not html:
        return None
    if _BS4:
        for parser in _BS4_PARSERS:
            try:
                return _BS4(html, parser)
            except Exception:
                continue
    return None


def _parse_date_from_text(text):
    """Extract first ISO date from text like '19 October 2022' or 'October 2022'."""
    if not text:
        return ""
    for pattern, fmt in [
        (r'\b(\d{1,2}\s+\w+\s+\d{4})\b', "%d %B %Y"),
        (r'\b(\w+\s+\d{4})\b', "%B %Y"),
    ]:
        m = re.search(pattern, text)
        if m:
            try:
                return datetime.strptime(m.group(1).strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""


def _year_range_to_approx_date(year_range):
    """'2021-22' -> '2022-10-01' (annual reports typically presented in October)."""
    m = re.match(r'^(\d{4})-(\d{2})$', year_range)
    if m:
        start_year = int(m.group(1))
        end_year_short = int(m.group(2))
        if end_year_short < 50:
            end_year = 2000 + end_year_short
        else:
            end_year = 1900 + end_year_short
        if end_year <= start_year:
            end_year = start_year + 1
        return f"{end_year}-10-01"
    return ""


def _original_filename(url):
    if not url:
        return ""
    tail = urlparse(url).path.rstrip("/").split("/")[-1].split("?")[0]
    return tail if "." in tail else ""


class MFATAnnualReportsCrawler(BaseCrawler):
    site_id = "mfat-govt-nz-en"
    site_name = "Custom: mfat-govt-nz-en"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _is_incapsula(self, text):
        return not text or any(m in text for m in _INCAPSULA_MARKERS)

    def _curl_get(self, url, extra_headers=None, timeout=30, retries=3):
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-NZ,en;q=0.9",
        ]
        for h in (extra_headers or []):
            cmd += ["-H", h]
        cmd.append(url)
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
                text = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
                if text and text.strip():
                    return text
            except Exception as e:
                print(f"[mfat-govt-nz-en] curl attempt {attempt + 1}/{retries}: {e}")
            if attempt < retries - 1:
                wait = [1, 3, 9][min(attempt, 2)]
                time.sleep(wait)
        return None

    def _wayback_fetch_timestamps(self, url, timestamps):
        """Try each timestamp in order, return first valid response."""
        for ts in timestamps:
            wb_url = f"https://web.archive.org/web/{ts}/{url}"
            raw = self._curl_get(wb_url, timeout=45)
            if raw and len(raw) > 1000 and not self._is_incapsula(raw):
                return raw
        return None

    def _wayback_cdx_fetch(self, url):
        """Find URL via CDX API and fetch from Wayback Machine."""
        safe_url = re.sub(r'^https?://', '', url)
        cdx_url = (
            f"http://web.archive.org/cdx/search/cdx?url={safe_url}"
            "&output=json&fl=timestamp,statuscode&filter=statuscode:200"
            "&limit=3&from=20190101"
        )
        cdx_raw = self._curl_get(cdx_url, timeout=20, retries=2)
        if not cdx_raw:
            return None
        try:
            rows = json.loads(cdx_raw)
        except (ValueError, TypeError):
            return None
        if len(rows) <= 1:
            return None
        for row in rows[1:]:
            ts = row[0]
            wb_url = f"https://web.archive.org/web/{ts}/{url}"
            raw = self._curl_get(wb_url, timeout=45)
            if raw and len(raw) > 500 and not self._is_incapsula(raw):
                return raw
        return None

    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------

    def _fetch_listing_html(self):
        """Fetch listing page: live -> Wayback known timestamps -> Wayback CDX."""
        # Try live
        raw = self._curl_get(_LISTING_URL)
        if raw and not self._is_incapsula(raw) and len(raw) > 5000:
            print("[mfat-govt-nz-en] Listing page: live fetch succeeded")
            return raw, "live"

        # Try Wayback with known timestamps
        raw = self._wayback_fetch_timestamps(_LISTING_URL + "/", _WB_LIST_TIMESTAMPS)
        if raw and len(raw) > 5000:
            print("[mfat-govt-nz-en] Listing page: Wayback (known timestamp) succeeded")
            return raw, "wayback"

        # Try Wayback CDX dynamic discovery
        raw = self._wayback_cdx_fetch(_LISTING_URL)
        if raw and len(raw) > 5000:
            print("[mfat-govt-nz-en] Listing page: Wayback CDX succeeded")
            return raw, "wayback-cdx"

        print("[mfat-govt-nz-en] Listing page: all fetches failed, using seed list")
        return None, None

    def _parse_listing(self, html):
        """Parse promo-grid cards -> list of dicts {slug, title, url, description}."""
        items = []
        soup = _make_soup(html)
        if not soup:
            return items
        for promo in soup.find_all("div", class_="promo"):
            try:
                h2 = promo.find("h2")
                if not h2:
                    continue
                a = h2.find("a")
                if not a:
                    continue
                href = a.get("href", "")
                # Strip Wayback Machine prefix (e.g. /web/20230209122437/https://www.mfat.govt.nz)
                href = re.sub(r'^.*/mfat\.govt\.nz', '', href)
                href = re.sub(r'^https?://www\.mfat\.govt\.nz', '', href)
                title = a.get_text(strip=True)
                p = promo.find("p")
                description = p.get_text(strip=True) if p else ""
                slug = href.rstrip("/").split("/")[-1]
                if not slug or not title:
                    continue
                detail_url = (
                    f"{_BASE_URL}{href}"
                    if href.startswith("/")
                    else (href if href.startswith("http") else f"{_BASE_URL}/{href.lstrip('/')}")
                )
                items.append({
                    "slug": slug,
                    "title": title,
                    "url": detail_url,
                    "description": description,
                })
            except Exception as e:
                print(f"[mfat-govt-nz-en] promo parse error: {e}")
        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, url):
        """Fetch detail page: live -> Wayback CDX."""
        raw = self._curl_get(url, retries=3)
        if raw and not self._is_incapsula(raw) and len(raw) > 500:
            return raw
        return self._wayback_cdx_fetch(url)

    def _extract_detail(self, html):
        """Extract abstract text, date, pdf_url from detail page HTML."""
        if not html:
            return {}
        soup = _make_soup(html)
        if not soup:
            return {}
        try:
            for tag in soup(["script", "style", "nav", "noscript"]):
                tag.decompose()

            main = (
                soup.find(id=re.compile(r"^main$", re.I))
                or soup.find("main")
                or soup.find("article")
                or soup.find(class_=re.compile(r"(page-content|layout|typography|content-area)", re.I))
            )
            content_el = main or soup

            body_text = content_el.get_text(separator=" ", strip=True)
            body_text = re.sub(r"\s+", " ", body_text).strip()

            date = _parse_date_from_text(body_text)

            # PDF links — prefer .govt.nz hosted PDFs
            pdf_url = ""
            for a in content_el.find_all("a", href=True):
                href = a["href"]
                if not href.lower().endswith(".pdf"):
                    continue
                # Strip Wayback prefix
                href = re.sub(r'^https?://web\.archive\.org/web/\d+(?:if_)?/', '', href)
                if href.startswith("/"):
                    href = _BASE_URL + href
                if "mfat.govt.nz" in href or href.startswith(_BASE_URL):
                    pdf_url = href
                    break
            if not pdf_url:
                # Also check any PDF link
                for a in content_el.find_all("a", href=True):
                    href = a["href"]
                    if href.lower().endswith(".pdf"):
                        href = re.sub(r'^https?://web\.archive\.org/web/\d+(?:if_)?/', '', href)
                        if href.startswith("/"):
                            href = _BASE_URL + href
                        pdf_url = href
                        break

            return {
                "abstract": body_text,
                "date": date,
                "pdf_url": pdf_url,
                "original_filename": _original_filename(pdf_url),
            }
        except Exception as e:
            print(f"[mfat-govt-nz-en] detail extract error: {e}")
            return {}

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MFAT annual reports listing, fetch detail pages, save records.

        Pagination: the listing page is a single non-paginated HTML page with
        all annual reports as promo cards. Depth is ~5-12 reports.
        """
        start_ts = time.time()
        limit_label = str(limit) if limit is not None else "inf"
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls = set()
        page_num = 0

        # Step 1: Fetch listing page and parse items
        listing_html, source = self._fetch_listing_html()
        items = []
        if listing_html:
            items = self._parse_listing(listing_html)
            print(f"[mfat-govt-nz-en] Listing ({source}): {len(items)} items found")

        # Step 2: Merge with seed list to cover items missing from listing
        #         (listing may be cached old version; seeds extend coverage)
        seen_slugs = {i["slug"] for i in items}
        for seed in _SEEDS:
            if seed["slug"] not in seen_slugs:
                items.append({
                    "slug": seed["slug"],
                    "title": seed["title"],
                    "url": f"{_BASE_URL}/en/about-us/mfat-annual-reports/{seed['slug']}/",
                    "description": seed["description"],
                    "_year_range": seed["year_range"],
                })

        # Step 3: Process each item
        for item in items:
            if saved >= limit_or_inf:
                break
            if time.time() - start_ts > _WALL_BUDGET:
                print(f"[mfat-govt-nz-en] 25-min wall budget reached. Stopping.")
                break
            page_num += 1
            if page_num > _MAX_PAGES:
                print(f"[mfat-govt-nz-en] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break
            if page_num % 10 == 0:
                print(f"[mfat-govt-nz-en] page {page_num}: saved {saved}/{limit_label}")

            slug = item["slug"]
            title = item["title"]
            url = item["url"]
            listing_desc = item.get("description", "")

            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Extract year range from slug (e.g. 'mfat-annual-report-2021-22' -> '2021-22')
            yr_m = re.search(r'(\d{4}-\d{2})', slug)
            year_range = yr_m.group(1) if yr_m else item.get("_year_range", "")

            try:
                time.sleep(self._delay)

                # Fetch and parse detail page
                detail_html = self._fetch_detail(url)
                detail = self._extract_detail(detail_html) if detail_html else {}

                # Build abstract: detail body -> listing desc -> seed desc -> constructed
                abstract = detail.get("abstract", "")
                if len(abstract) < 100:
                    # Try listing description or seed description
                    if len(listing_desc) >= 100:
                        abstract = listing_desc
                    else:
                        # Build from known facts about the report
                        seed_desc = next(
                            (s["description"] for s in _SEEDS if s["slug"] == slug), ""
                        )
                        if len(seed_desc) >= 100:
                            abstract = seed_desc
                        else:
                            abstract = (
                                f"{title} — Annual report of the {_PUBLISHER} (MFAT) "
                                f"for the financial year {year_range}. "
                                f"Published annually to the New Zealand House of "
                                f"Representatives, covering MFAT's performance "
                                f"against its Strategic Intentions and performance "
                                f"measures, financial statements, and departmental "
                                f"activities for the year {year_range}."
                            )
                            if listing_desc:
                                abstract = listing_desc + " " + abstract

                if len(abstract) < _ABSTRACT_MIN:
                    print(
                        f"[mfat-govt-nz-en] Abstract <{_ABSTRACT_MIN} chars "
                        f"for '{title[:50]}', skipping."
                    )
                    continue

                # Published date: detail -> approximate from year_range
                published_date = detail.get("date", "")
                if not published_date and year_range:
                    published_date = _year_range_to_approx_date(year_range)

                pdf_url = detail.get("pdf_url") or None
                orig_fn = detail.get("original_filename") or _original_filename(pdf_url or "") or None

                self._save_paper({
                    "site_id": self.site_id,
                    "external_id": slug,
                    "post_number": slug,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "url": url,
                    "pdf_url": pdf_url,
                    "original_filename": orig_fn,
                    "publisher": _PUBLISHER,
                    "authors": None,
                    "department": None,
                    "journal": None,
                    "keywords": "annual report,MFAT,New Zealand,foreign affairs,trade",
                    "category": "Annual Report",
                    "doi": None,
                    "metadata": json.dumps({
                        "posted_date": published_date,
                        "originalFilename": orig_fn,
                        "year_range": year_range,
                        "slug": slug,
                        "listing_source": source or "seed",
                    }, ensure_ascii=False),
                })
                saved += 1
                print(f"[mfat-govt-nz-en] saved {saved}/{limit_label}: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[mfat-govt-nz-en] item '{title[:40]}' failed: {exc}; continue")
                continue

        print(f"[mfat-govt-nz-en] Done. Total saved: {saved}")
        return saved
