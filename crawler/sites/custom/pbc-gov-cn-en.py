# -*- coding: utf-8 -*-
"""Crawler for pbc.gov.cn (People's Bank of China) - English site.

Target section: Statistics > Statistical Releases > Aggregate Financing
Reports.

  Start URL:
    https://www.pbc.gov.cn/en/3688247/3688978/3709140/index.html

List page structure (server-rendered HTML, eportal/easysite CMS):
  Each item is a ``<li>`` containing a ``div.prhhd1`` with:
    - ``span.prhhdata``            -> listed date, already "YYYY-MM-DD"
    - ``a[istitle="true"]``        -> detail page href + full title (title attr)

Pagination: the list page embeds a small JS pagination widget whose
"jumpToPage(...)" call reveals both the total page count and a URL
template for subsequent pages, e.g.:

    jumpToPage(event,this,'23','1','/en/3688247/3688978/3709140/48b09237-%1.html')

Page 1 is the plain ``index.html`` URL; pages 2..N substitute the page
number into the ``%1`` placeholder of that template. The token before
the page number (``48b09237`` above) is a per-listing module id and is
extracted dynamically rather than hard-coded.

Detail page structure:
    <div class="xiangxiDetial">
      <div class="artTit">
        <div class="DetailBreadcrum">Home > Statistics > Statistical
             Releases > Aggregate Financing Reports</div>
        <h2 class="enTitle">...full title...</h2>
      </div>
      <div class="content">
        <div class="gitop w1000"> <p>...body paragraphs...</p> ... </div>
        <div class="data" style="display:none!important;">2025年09月12日</div>
      </div>
    </div>

The hidden ``div.data`` holds the article's own date in Chinese
"YYYY年MM月DD日" format; it generally matches the list page's date but is
parsed independently as the ``published_date`` when present. No PDF
attachments were observed on this particular listing (reports are
published as inline HTML), but detail pages are still scanned for
``.pdf`` links for robustness / forward-compatibility.

External/native ID: the numeric path segment right before
``index.html`` in the detail URL (e.g. ``5839519`` or the longer purely
numeric string ``2025080817514949251``) — used as both ``external_id``
and ``post_number``.
"""

import json
import os
import re
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "pbc-gov-cn-en"
_BASE_URL = "https://www.pbc.gov.cn"
_LIST_URL = "https://www.pbc.gov.cn/en/3688247/3688978/3709140/index.html"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))  # safety cap
_WALL_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes
_DETAIL_DELAY = 1.0  # seconds between detail-page fetches
_RETRY_WAITS = (1, 3, 9)  # exponential backoff between retries

_PAGINATION_RE = re.compile(
    r"jumpToPage\([^,]+,[^,]+,'(\d+)','\d+','([^']+)'\)"
)
_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _make_soup(html):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    if isinstance(html, str):
        raw = html.encode("utf-8", errors="replace")
    else:
        raw = html
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return BeautifulSoup(raw, "html.parser")


def _parse_cn_date(text):
    """'2025年09月12日' -> '2025-09-12'. Returns None on failure."""
    if not text:
        return None
    m = _CN_DATE_RE.search(text)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


class PbcGovCnEnCrawler(BaseCrawler):
    """Crawler for pbc.gov.cn/en - Aggregate Financing Reports."""

    site_id = _SITE_ID
    site_name = "Custom: pbc-gov-cn-en"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Networking helper
    # ------------------------------------------------------------------

    def _fetch(self, url, retries=3):
        """GET ``url`` via the shared session, with manual backoff retries.

        Returns decoded text on success, or ``None`` after exhausting
        retries. Never raises for network/decoding errors.
        """
        for attempt in range(retries):
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                try:
                    return resp.content.decode("utf-8")
                except UnicodeDecodeError:
                    return resp.content.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{_SITE_ID}] fetch error (attempt {attempt + 1}/{retries}) for {url}: {exc}")
                if attempt < retries - 1:
                    wait = _RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)]
                    print(f"[{_SITE_ID}] retrying in {wait}s...")
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_items(self, list_soup):
        """Return a list of dicts {url, external_id, title, listed_date, listed_date_raw}."""
        items = []
        for block in list_soup.find_all("div", class_="prhhd1"):
            a_tag = block.find("a", attrs={"istitle": "true"})
            if not a_tag:
                continue
            href = a_tag.get("href", "")
            if not href:
                continue
            full_url = href if href.startswith("http") else _BASE_URL + href
            title = (a_tag.get("title") or a_tag.get_text(strip=True) or "").strip()
            if not title:
                continue

            date_el = block.find("span", class_="prhhdata")
            listed_date_raw = date_el.get_text(strip=True) if date_el else ""
            listed_date = listed_date_raw if re.match(r"^\d{4}-\d{2}-\d{2}$", listed_date_raw) else None

            m = re.search(r"/(\d+)/index\.html", full_url)
            external_id = m.group(1) if m else full_url.rstrip("/").split("/")[-2]

            items.append({
                "url": full_url,
                "external_id": external_id,
                "title": title,
                "listed_date": listed_date,
                "listed_date_raw": listed_date_raw,
            })
        return items

    def _find_pagination(self, html):
        """Return (total_pages:int, url_template:str) or (None, None)."""
        m = _PAGINATION_RE.search(html)
        if not m:
            return None, None
        total_pages_str, template = m.groups()
        try:
            total_pages = int(total_pages_str)
        except ValueError:
            return None, None
        full_template = template if template.startswith("http") else _BASE_URL + template
        return total_pages, full_template

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, detail_soup, fallback_title):
        h2 = detail_soup.find("h2", class_="enTitle")
        title = h2.get_text(strip=True) if h2 else ""
        if not title:
            title = fallback_title

        content_div = detail_soup.find("div", class_="content")
        paragraphs = []
        if content_div:
            for p in content_div.find_all("p"):
                text = p.get_text(strip=True)
                if text:
                    paragraphs.append(text)
        abstract = "\n\n".join(paragraphs)

        published_date = None
        data_div = detail_soup.find("div", class_="data")
        if data_div:
            published_date = _parse_cn_date(data_div.get_text(strip=True))

        category = ""
        breadcrumb = detail_soup.find("div", class_="DetailBreadcrum")
        breadcrumb_crumbs = []
        if breadcrumb:
            breadcrumb_crumbs = [a.get_text(strip=True) for a in breadcrumb.find_all("a")]
            if breadcrumb_crumbs:
                category = breadcrumb_crumbs[-1]
        department = breadcrumb_crumbs[1] if len(breadcrumb_crumbs) > 1 else ""

        pdf_url = None
        original_filename = None
        search_root = content_div if content_div else detail_soup
        for a in search_root.find_all("a", href=True):
            href_val = a["href"]
            if ".pdf" in href_val.lower():
                pdf_url = href_val if href_val.startswith("http") else _BASE_URL + href_val
                fn_m = re.search(r"/([^/?#]+\.pdf)", href_val, re.IGNORECASE)
                original_filename = fn_m.group(1) if fn_m else pdf_url.rstrip("/").split("/")[-1]
                break

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "category": category,
            "department": department,
            "breadcrumb": breadcrumb_crumbs,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        page = 1
        total_pages = None
        url_template = None

        while page <= _MAX_PAGES:
            elapsed = time.monotonic() - start_time
            if elapsed > _WALL_BUDGET_S:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded ({elapsed:.0f}s). Exiting with {saved} saved.")
                return saved

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            if page == 1:
                page_url = _LIST_URL
            else:
                if not url_template:
                    print(f"[{_SITE_ID}] No pagination template found; stopping at page {page - 1}.")
                    break
                page_url = url_template.replace("%1", str(page))

            list_raw = self._fetch(page_url)
            if not list_raw:
                print(f"[{_SITE_ID}] Failed to fetch listing page {page}. Stopping.")
                break

            try:
                list_soup = _make_soup(list_raw)
            except Exception as exc:
                print(f"[{_SITE_ID}] HTML parse error on listing page {page}: {exc}. Skipping page.")
                page += 1
                continue

            if page == 1:
                total_pages, url_template = self._find_pagination(list_raw)
                if total_pages:
                    total_pages = min(total_pages, _MAX_PAGES)

            items = self._parse_list_items(list_soup)
            new_items = [it for it in items if it["url"] not in seen_urls]
            for it in new_items:
                seen_urls.add(it["url"])

            if not new_items:
                print(f"[{_SITE_ID}] No new items on page {page}. Pagination complete.")
                break

            page_saved = 0
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(_DETAIL_DELAY)
                    detail_raw = self._fetch(item["url"])
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {item['url']} failed: empty response. Skipping.")
                        continue

                    try:
                        detail_soup = _make_soup(detail_raw)
                    except Exception as exc:
                        print(f"[{_SITE_ID}] item {item['url']} HTML parse failed: {exc}. Skipping.")
                        continue

                    parsed = self._parse_detail(detail_soup, item["title"])

                    abstract = parsed["abstract"]
                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] item {item['url']} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    published_date = parsed["published_date"] or item["listed_date"]
                    listed_date = item["listed_date"] or published_date

                    paper = {
                        "site_id": self.site_id,
                        "external_id": item["external_id"],
                        "post_number": item["external_id"],
                        "title": parsed["title"] or item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": item["url"],
                        "pdf_url": parsed["pdf_url"],
                        "original_filename": parsed["original_filename"],
                        "authors": "",
                        "publisher": "People's Bank of China",
                        "department": parsed["department"],
                        "journal": "",
                        "keywords": "",
                        "category": parsed["category"] or "Aggregate Financing Reports",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": item["listed_date_raw"],
                                "originalFilename": parsed["original_filename"],
                                "journal_raw": None,
                                "series": None,
                                "volume": None,
                                "issue": None,
                                "article_key": item["external_id"],
                                "column_id": "3709140",
                                "breadcrumb": parsed["breadcrumb"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    page_saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_or_inf}: {paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item['url']} failed: {exc}")
                    continue

            if page_saved == 0 and (limit is None or saved < limit):
                print(f"[{_SITE_ID}] Page {page} yielded 0 new saved records. Stopping.")
                break

            if total_pages and page >= total_pages:
                print(f"[{_SITE_ID}] Reached last known page ({total_pages}).")
                break

            page += 1

        if page >= _MAX_PAGES:
            print(f"[{_SITE_ID}] Reached safety cap of {_MAX_PAGES} pages.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
