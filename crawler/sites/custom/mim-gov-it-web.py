# -*- coding: utf-8 -*-
"""MIM (Ministero dell'Istruzione e del Merito) comunicati crawler.

Source: https://www.mim.gov.it/web/guest/comunicati
CMS:    Liferay 7.x — AssetPublisher portlet (PQibxq1lWdyu)
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_PORTLET_NS = (
    "com_liferay_asset_publisher_web_portlet_"
    "AssetPublisherPortlet_INSTANCE_PQibxq1lWdyu"
)
_LIST_URL = "https://www.mim.gov.it/web/guest/comunicati"
_BASE_URL = "https://www.mim.gov.it"
_PAGE_SIZE = 20  # articles per list page

_IT_MONTHS = {
    "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
    "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
    "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_it_date(raw: str) -> str:
    """'12 maggio 2026' or 'Martedì, 12 maggio 2026' → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = raw.strip()
    # Strip weekday prefix like "Martedì, "
    raw = re.sub(r"^[^,]+,\s*", "", raw).strip()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if not m:
        return ""
    day, month_it, year = m.group(1), m.group(2).lower(), m.group(3)
    month = _IT_MONTHS.get(month_it, "")
    if not month:
        return ""
    return f"{year}-{month}-{int(day):02d}"


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_get(url: str) -> str | None:
    """Fetch URL via curl; 3 retries with exponential back-off. Returns body or None."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
        "-A", _UA,
        "-H", "Accept-Language: it-IT,it;q=0.9,en;q=0.8",
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            body = result.stdout.decode("utf-8", errors="replace")
            if body.strip():
                return body
        except Exception as exc:
            print(f"[mim-gov-it-web] curl error (attempt {attempt + 1}/3): {exc}")
        if attempt < 2:
            wait = 3 ** attempt  # 1 s, 3 s
            print(f"[mim-gov-it-web] Retry {attempt + 2}/3 for {url[:60]} in {wait}s")
            time.sleep(wait)
    print(f"[mim-gov-it-web] Failed after 3 attempts: {url[:80]}")
    return None


def _make_soup(html: str):
    """Try html5lib → lxml → html.parser; return BeautifulSoup or None."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MimGovItWebCrawler(BaseCrawler):
    """Crawler for MIM Comunicati (Liferay AssetPublisher)."""

    site_id = "mim-gov-it-web"
    site_name = "Custom: mim-gov-it-web"
    base_url = "https://www.mim.gov.it"

    def _list_url(self, cur: int) -> str:
        return (
            f"{_LIST_URL}"
            f"?p_p_id={_PORTLET_NS}"
            f"&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
            f"&_{_PORTLET_NS}_delta={_PAGE_SIZE}"
            f"&_{_PORTLET_NS}_cur={cur}"
        )

    def crawl(self, limit=None):
        saved = 0
        cur = 1
        seen_urls: set = set()
        start_ts = time.time()
        budget_secs = 25 * 60  # 25-minute wall-clock budget

        while True:
            # ---- budget / safety checks ----
            if time.time() - start_ts > budget_secs:
                print(f"[mim-gov-it-web] 25-minute budget reached at page {cur}. Stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if cur > 200:
                print(f"[mim-gov-it-web] Safety cap of 200 pages reached. Stopping.")
                break

            # ---- progress log every 10 pages ----
            if cur == 1 or cur % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[mim-gov-it-web] page {cur}: saved {saved}/{limit_str}")

            # ---- fetch list page ----
            raw = _curl_get(self._list_url(cur))
            if not raw:
                print(f"[mim-gov-it-web] Empty list at page {cur}. Stopping.")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[mim-gov-it-web] Parse failure at page {cur}. Stopping.")
                break

            # Each article is a <div class="box_list_large even|odd">
            article_boxes = soup.find_all("div", class_="box_list_large")
            if not article_boxes:
                print(f"[mim-gov-it-web] No items at page {cur}. Done.")
                break

            new_on_page = 0

            for box in article_boxes:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_ts > budget_secs:
                    print(f"[mim-gov-it-web] Budget reached mid-page {cur}.")
                    break

                try:
                    # --- listed date ---
                    date_span = box.find("span", class_="date")
                    listed_date_raw = (date_span.get_text(strip=True)
                                       if date_span else "")
                    listed_date = _parse_it_date(listed_date_raw)

                    # --- title + URL ---
                    h3 = box.find("h3")
                    if not h3:
                        continue
                    a_tag = h3.find("a")
                    if not a_tag:
                        continue
                    title = a_tag.get_text(strip=True)
                    href = (a_tag.get("href") or "").strip()
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = _BASE_URL + href

                    # --- URL deduplication ---
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)
                    new_on_page += 1

                    # --- detail fetch ---
                    time.sleep(self._delay)
                    detail_raw = _curl_get(href)
                    if not detail_raw:
                        print(f"[mim-gov-it-web] Detail fetch failed: {href[:60]}")
                        continue

                    detail_soup = _make_soup(detail_raw)
                    if detail_soup is None:
                        print(f"[mim-gov-it-web] Detail parse failed: {href[:60]}")
                        continue

                    # --- asset ID (numeric Liferay ID) ---
                    asset_el = detail_soup.find(
                        attrs={"data-analytics-asset-id": True}
                    )
                    asset_id = (
                        (asset_el["data-analytics-asset-id"] or "").strip()
                        if asset_el else ""
                    )

                    # --- abstract from post-content (fallback: full article div) ---
                    post_div = detail_soup.find("div", class_="post-content")
                    if post_div:
                        abstract = _strip_tags(str(post_div))
                    else:
                        jca = detail_soup.find("div", class_="journal-content-article")
                        abstract = _strip_tags(str(jca)) if jca else ""

                    if len(abstract) < 100:
                        print(
                            f"[mim-gov-it-web] Abstract too short "
                            f"({len(abstract)} chars), skipping: {href[:60]}"
                        )
                        continue

                    # --- published date from detail page ---
                    pub_date = listed_date
                    data_print = detail_soup.find("div", class_="post-data-print")
                    if data_print:
                        p_tag = data_print.find("p", class_="pull-left")
                        if p_tag:
                            parsed = _parse_it_date(p_tag.get_text(strip=True))
                            if parsed:
                                pub_date = parsed

                    # --- PDF attachment ---
                    pdf_url = None
                    original_filename = None
                    for a in detail_soup.find_all("a", href=True):
                        lhref = (a.get("href") or "").strip()
                        if not lhref:
                            continue
                        lower = lhref.lower()
                        if lower.endswith(".pdf") or (
                            "/documents/" in lower and ".pdf" in lower
                        ):
                            pdf_url = (
                                lhref if lhref.startswith("http")
                                else _BASE_URL + lhref
                            )
                            fn = lhref.rstrip("/").split("/")[-1].split("?")[0]
                            if fn.lower().endswith(".pdf"):
                                original_filename = fn
                            break

                    url_slug = href.rstrip("/").split("/")[-1]
                    external_id = asset_id or url_slug

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": asset_id if asset_id else None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "url": href,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": "Ministero dell'Istruzione e del Merito",
                        "authors": "",
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": "comunicato",
                        "doi": "",
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "listed_date_raw": listed_date_raw,
                                "asset_id": asset_id,
                                "url_slug": url_slug,
                                "originalFilename": original_filename,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "∞"
                    print(
                        f"[mim-gov-it-web] saved {saved}/{limit_str}: "
                        f"{title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[mim-gov-it-web] item failed: {exc}; continuing")
                    continue

            # ---- end-of-pagination detection ----
            if new_on_page == 0:
                print(f"[mim-gov-it-web] Page {cur}: no new URLs. Done.")
                break

            cur += 1

        print(f"[mim-gov-it-web] Done. Total saved: {saved}")
        return saved
