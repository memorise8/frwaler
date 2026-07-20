# -*- coding: utf-8 -*-
"""CNIPA Annual Reports crawler — english.cnipa.gov.cn/col/col1336/

Hierarchy:
  Main page (col1336) → year sub-collections (col3568=2024, col3480=2023, …)
  Each year page → dataproxy XML records with title/date/PDF URL/article URL
  Article detail pages → metadata tags (content body is always empty PDF-only)
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup as _BS4
    _BS4_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS4 = None
    _BS4_PARSERS = []

_BASE_URL = "https://english.cnipa.gov.cn"
_WEBID = "2"
_WEBNAME = "China+National+Intellectual+Property+Administration"
_PUBLISHER = "China National Intellectual Property Administration (CNIPA)"
_START_URL = f"{_BASE_URL}/col/col1336/index.html"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, max_attempts: int = 3) -> str | None:
    """GET via curl with exponential-backoff retry. Returns decoded text or None."""
    delays = [1, 3, 9]
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["curl", "-skL", "--tls-max", "1.3", "--compressed",
                 "--max-time", "30", "-H", f"User-Agent: {_UA}", url],
                capture_output=True, timeout=35,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            print(f"[english-cnipa-gov-cn-col] curl attempt {attempt+1} error for {url}: {exc}")
        if attempt < max_attempts - 1:
            time.sleep(delays[attempt])
    print(f"[english-cnipa-gov-cn-col] curl failed after {max_attempts} attempts: {url}")
    return None


def _make_soup(html: str):
    if not _BS4 or not html:
        return None
    for parser in _BS4_PARSERS:
        try:
            return _BS4(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return None


def _build_abstract(year_label: str, title: str) -> str:
    """Construct a ≥100-char synthetic abstract for an annual report section."""
    return (
        f"CNIPA Annual Report {year_label} — {title}. "
        f"Published by the China National Intellectual Property Administration (CNIPA), "
        f"this document is part of the official annual report series for {year_label}, "
        f"covering intellectual property statistics, policies, and developments in China."
    )


def _extract_meta_tags(html: str) -> dict:
    """Fast regex extraction of <meta name=... content=...> pairs."""
    result: dict = {}
    # name before content
    for m in re.finditer(
        r'<meta\s+name=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
        html, re.IGNORECASE,
    ):
        result[m.group(1)] = m.group(2)
    # content before name
    for m in re.finditer(
        r'<meta\s+content=["\']([^"\']*)["\'][^>]+name=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ):
        result.setdefault(m.group(2), m.group(1))
    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class CnipaAnnualReportCrawler(BaseCrawler):
    site_id = "english-cnipa-gov-cn-col"
    site_name = "Custom: english-cnipa-gov-cn-col"
    base_url = "https://english.cnipa.gov.cn"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_n = float("inf") if limit is None else int(limit)
        start_ts = time.time()
        MAX_SECS = 25 * 60  # 25-minute wall-clock budget

        year_cols = self._get_year_collections()
        if not year_cols:
            print(f"[{self.site_id}] ERROR: no year collections found on main page")
            return 0

        print(f"[{self.site_id}] Found {len(year_cols)} year collections")

        col_idx = 0
        for year_label, col_url in year_cols:
            if saved >= limit_n:
                break
            if time.time() - start_ts > MAX_SECS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached, stopping")
                break

            col_idx += 1
            if col_idx % 10 == 0:
                print(f"[{self.site_id}] page {col_idx}: saved {saved}/{limit_n}")

            try:
                n = self._crawl_year(
                    year_label, col_url, seen_urls, limit_n - saved, start_ts, MAX_SECS
                )
                saved += n
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] year {year_label} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Step 1: discover year sub-collections from main page
    # ------------------------------------------------------------------

    def _get_year_collections(self) -> list:
        html = _curl_get(_START_URL)
        if not html:
            return []
        soup = _make_soup(html)
        if not soup:
            # fallback regex
            results = []
            seen: set = set()
            for m in re.finditer(r'href=["\'](/col/col(\d+)/index\.html)["\']', html):
                href = m.group(1)
                if href not in seen and "col1336" not in href:
                    seen.add(href)
                    results.append((f"col{m.group(2)}", _BASE_URL + href))
            return results

        results = []
        seen: set = set()
        col_list = soup.find("div", class_="column_list")
        if col_list:
            for a in col_list.find_all("a", href=re.compile(r"/col/col\d+")):
                href = a.get("href", "")
                label = a.get_text(strip=True)
                if not label or href in seen:
                    continue
                seen.add(href)
                if not href.startswith("http"):
                    href = _BASE_URL + href
                results.append((label, href))

        if not results:
            for a in soup.find_all("a", href=re.compile(r"/col/col\d+")):
                href = a.get("href", "")
                label = a.get_text(strip=True)
                if label and href not in seen and label not in ("Annual Reports", "News & Events"):
                    seen.add(href)
                    if not href.startswith("http"):
                        href = _BASE_URL + href
                    results.append((label, href))

        return results

    # ------------------------------------------------------------------
    # Step 2: crawl one year's collection
    # ------------------------------------------------------------------

    def _crawl_year(
        self,
        year_label: str,
        col_url: str,
        seen_urls: set,
        remaining: int,
        start_ts: float,
        max_secs: float,
    ) -> int:
        m = re.search(r"/col/col(\d+)/", col_url)
        if not m:
            return 0
        col_id = m.group(1)

        html = _curl_get(col_url)
        if not html:
            print(f"[{self.site_id}] Failed to fetch {col_url}")
            return 0

        unit_id = self._extract_unit_id(html)
        if unit_id:
            items = self._fetch_via_dataproxy(col_id, unit_id, year_label)
        else:
            items = self._parse_page_items(html, col_id, year_label)

        if not items:
            print(f"[{self.site_id}] No items for year {year_label} (col {col_id})")
            return 0

        saved = 0
        for item in items:
            if saved >= remaining:
                break
            if time.time() - start_ts > max_secs:
                break

            dedup_key = item.get("art_url") or item.get("pdf_url") or ""
            if dedup_key and dedup_key in seen_urls:
                continue
            if dedup_key:
                seen_urls.add(dedup_key)

            try:
                ok = self._process_item(item, year_label)
                if ok:
                    saved += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                title_hint = item.get("title") or item.get("art_id") or "?"
                print(f"[{self.site_id}] item {title_hint!r} failed: {exc}")
                continue

            time.sleep(self._delay)

        return saved

    # ------------------------------------------------------------------
    # Dataproxy pagination
    # ------------------------------------------------------------------

    def _extract_unit_id(self, html: str) -> str | None:
        """Find the unitid from the page's jpage setup script."""
        m = re.search(r"unitid['\s:,]+['\"]?(\d+)", html)
        if m:
            return m.group(1)
        # div-id wrapping <datastore>
        m = re.search(r'<div\s+id=["\'](\d+)["\'][^>]*>[\s\S]{0,200}?<datastore>', html)
        if m:
            return m.group(1)
        return None

    def _fetch_via_dataproxy(self, col_id: str, unit_id: str, year_label: str) -> list:
        """Fetch all pages via the jpage dataproxy API."""
        items: list = []
        safety_cap = 200

        for page in range(1, safety_cap + 1):
            url = (
                f"{_BASE_URL}/module/web/jpage/dataproxy.jsp"
                f"?page={page}&webid={_WEBID}&path={_BASE_URL}/"
                f"&columnid={col_id}&unitid={unit_id}"
                f"&webname={_WEBNAME}&permissiontype=0"
            )
            raw = _curl_get(url)
            if not raw:
                break

            records = re.findall(r"<record><!\[CDATA\[([\s\S]*?)\]\]></record>", raw)
            if not records:
                break

            for rec in records:
                item = self._parse_record_cdata(rec, col_id, year_label)
                if item:
                    items.append(item)

            m = re.search(r"<totalpage>(\d+)</totalpage>", raw)
            total_pages = int(m.group(1)) if m else 1

            if page >= safety_cap:
                print(f"[{self.site_id}] Safety cap of {safety_cap} pages reached for col {col_id}")
                break
            if page >= total_pages:
                break

            time.sleep(0.5)

        return items

    # ------------------------------------------------------------------
    # Fallback: parse items from raw page HTML
    # ------------------------------------------------------------------

    def _parse_page_items(self, html: str, col_id: str, year_label: str) -> list:
        """Fallback for pages without dataproxy (e.g. image-grid layout)."""
        items: list = []

        # Try embedded CDATA records first
        records = re.findall(r"<record><!\[CDATA\[([\s\S]*?)\]\]></record>", html)
        for rec in records:
            item = self._parse_record_cdata(rec, col_id, year_label)
            if item:
                items.append(item)
        if items:
            return items

        # Fallback: parse download links (older image-grid format)
        seen_ids: set = set()
        for m in re.finditer(r'href=["\']([^"\']*down\.jsp\?i_ID=(\d+)[^"\']*)["\']', html):
            href = m.group(1)
            art_id = m.group(2)
            if art_id in seen_ids:
                continue
            seen_ids.add(art_id)
            if not href.startswith("http"):
                href = _BASE_URL + href
            items.append({
                "title": None,
                "art_id": art_id,
                "col_id": col_id,
                "art_url": None,
                "pdf_url": href,
                "date_str": None,
                "year_label": year_label,
            })

        return items

    # ------------------------------------------------------------------
    # Parse a single CDATA record
    # ------------------------------------------------------------------

    def _parse_record_cdata(self, cdata: str, col_id: str, year_label: str) -> dict | None:
        title = None
        pdf_url = None
        art_url = None
        date_str = None
        art_id = None

        # Title from title= attribute
        m = re.search(r'title=["\']([^"\']+)["\']', cdata)
        if m:
            title = (m.group(1)
                     .replace("&#39;", "'")
                     .replace("&amp;", "&")
                     .replace("&lt;", "<")
                     .replace("&gt;", ">")
                     .replace("&quot;", '"'))

        # Direct PDF — /attach/0/hash.pdf (dataproxy response)
        m = re.search(r'href=["\']([^"\']*?/attach/[^"\']+\.pdf)["\']', cdata, re.IGNORECASE)
        if m:
            href = m.group(1)
            pdf_url = href if href.startswith("http") else _BASE_URL + href

        # Download redirect — down.jsp?i_ID=… (page-embedded HTML)
        if not pdf_url:
            m = re.search(r'href=["\']([^"\']*?down\.jsp\?i_ID=\d+[^"\']*)["\']', cdata)
            if m:
                href = m.group(1)
                pdf_url = href if href.startswith("http") else _BASE_URL + href

        # Article URL (often in an HTML comment inside CDATA)
        m = re.search(r'href=["\']([^"\']*?/art/[^"\']+\.html)["\']', cdata)
        if m:
            href = m.group(1)
            art_url = href if href.startswith("http") else _BASE_URL + href

        # Art ID from article URL
        if art_url:
            m = re.search(r"/art_\d+_(\d+)\.html$", art_url)
            if m:
                art_id = m.group(1)

        # Art ID from download URL
        if not art_id and pdf_url:
            m = re.search(r"i_ID=(\d+)", pdf_url)
            if m:
                art_id = m.group(1)

        # Date from (YYYY-MM-DD) span
        m = re.search(r"<span[^>]*>\(([^)]+)\)</span>", cdata)
        if m:
            date_str = m.group(1).strip()

        if not title and not art_id:
            return None

        if not title:
            title = f"CNIPA Annual Report {year_label} — Section {art_id}"

        return {
            "title": title,
            "art_id": art_id,
            "col_id": col_id,
            "art_url": art_url,
            "pdf_url": pdf_url,
            "date_str": date_str,
            "year_label": year_label,
        }

    # ------------------------------------------------------------------
    # Step 3: process one item (fetch detail, build abstract, save)
    # ------------------------------------------------------------------

    def _process_item(self, item: dict, year_label: str) -> bool:
        art_id = item.get("art_id")
        art_url = item.get("art_url")
        pdf_url = item.get("pdf_url")
        col_id = item.get("col_id")
        title = item.get("title")
        date_str = item.get("date_str")

        # Fetch article detail page for metadata
        page_meta: dict = {}
        if art_url:
            html = _curl_get(art_url)
            if html:
                try:
                    page_meta = _extract_meta_tags(html)
                except Exception as exc:
                    print(f"[{self.site_id}] meta-extract failed for {art_url}: {exc}")

        if not title:
            title = (page_meta.get("ArticleTitle")
                     or page_meta.get("article:title")
                     or f"CNIPA Annual Report {year_label} — Section {art_id}")

        # Abstract: article body is always empty on this site → synthetic
        raw_desc = page_meta.get("description", "").strip()
        if len(raw_desc) >= 50:
            abstract = raw_desc
        else:
            abstract = _build_abstract(year_label, title)

        if len(abstract) < 50:
            print(f"[{self.site_id}] Skipping '{title}': abstract too short ({len(abstract)} chars)")
            return False

        # Dates
        raw_date = date_str or page_meta.get("pubdate", "")
        pub_date = _parse_date(raw_date)
        listed_date = pub_date

        # URL fields
        url = art_url or (
            f"{_BASE_URL}/module/download/down.jsp?i_ID={art_id}&colID={col_id}"
            if art_id and col_id else ""
        )

        # Stable external_id for dedup
        external_id = f"col{col_id}_{art_id}" if art_id else f"col{col_id}_{re.sub(r'[^a-zA-Z0-9]', '_', title)[:40]}"

        # Original filename from PDF URL
        original_filename = None
        if pdf_url:
            m = re.search(r"/([^/?#]+\.pdf)", pdf_url, re.IGNORECASE)
            if m:
                original_filename = m.group(1)

        paper = {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": art_id,
            "title": title,
            "abstract": abstract,
            "url": url,
            "pdf_url": pdf_url,
            "published_date": pub_date,
            "listed_date": listed_date,
            "publisher": _PUBLISHER,
            "category": f"Annual Report {year_label}",
            "original_filename": original_filename,
            "keywords": f"CNIPA,annual report,{year_label},intellectual property,China",
            "metadata": json.dumps(
                {
                    "year": year_label,
                    "col_id": col_id,
                    "art_id": art_id,
                    "posted_date": date_str,
                    "originalFilename": original_filename,
                    "guid": page_meta.get("guid"),
                },
                ensure_ascii=False,
            ),
        }

        self._save_paper(paper)
        return True
