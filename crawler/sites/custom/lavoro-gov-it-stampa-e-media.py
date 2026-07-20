# -*- coding: utf-8 -*-
"""Crawler for lavoro.gov.it — Stampa e Media / Comunicati (press releases).

Target: https://www.lavoro.gov.it/stampa-e-media/Comunicati/Pagine/Comunicati
Pagination: ?page=0 .. ?page=N (20 items/page, ~87 pages total as of 2026-05)
Detail pages: /stampa-e-media/comunicati/pagine/<slug>
Node ID extracted from data-history-node-id attribute (used as post_number).
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "lavoro-gov-it-stampa-e-media"
_BASE_URL = "https://www.lavoro.gov.it"
_LIST_URL = "https://www.lavoro.gov.it/stampa-e-media/Comunicati/Pagine/Comunicati"
_PUBLISHER = "Ministero del Lavoro e delle Politiche Sociali"

_MONTHS_IT = {
    "gen": "01", "feb": "02", "mar": "03", "apr": "04",
    "mag": "05", "giu": "06", "lug": "07", "ago": "08",
    "set": "09", "ott": "10", "nov": "11", "dic": "12",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _parse_it_date(raw: str) -> str:
    """Convert 'DD Mmm YYYY' or ISO datetime string → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    m = re.match(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", raw, re.IGNORECASE)
    if m:
        day, mon, year = m.group(1), m.group(2).lower()[:3], m.group(3)
        mo = _MONTHS_IT.get(mon, "")
        if mo:
            return f"{year}-{mo}-{day.zfill(2)}"
    return ""


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS 1.3 and retry/backoff. Returns decoded text or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: it-IT,it;q=0.9,en;q=0.8",
        url,
    ]
    wait_times = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/{retries} ({url}): {exc}")
        if attempt < retries - 1:
            time.sleep(wait_times[attempt])
    return None


def _make_soup(html: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_detail(html: str):
    """Parse a Comunicati detail page.

    Returns (abstract, node_id, pub_date, pdf_url, original_filename).
    """
    try:
        soup = _make_soup(html)
    except Exception:
        soup = None
    if soup is None:
        return "", "", "", None, None

    # Drupal node ID (numeric, incrementing — perfect post_number)
    article_tag = soup.find(attrs={"data-history-node-id": True})
    node_id = str(article_tag["data-history-node-id"]) if article_tag else ""

    # Publication date: field--name-field-data-riferimento → "13 Mag 2026"
    date_field = soup.find(class_=re.compile(r"field--name-field-data-riferimento"))
    pub_date = ""
    if date_field:
        pub_date = _parse_it_date(date_field.get_text(strip=True))

    # Abstract: body field paragraphs
    abstract = ""
    body_field = soup.find(class_=re.compile(r"field--name-body"))
    if body_field:
        paras = body_field.find_all("p")
        parts = [p.get_text(separator=" ", strip=True) for p in paras
                 if p.get_text(strip=True)]
        abstract = "\n\n".join(parts)

    if not abstract:
        # Fallback: full node content text
        content = soup.find(class_="node__content")
        if content:
            abstract = re.sub(r"\s+", " ", content.get_text(separator=" ")).strip()[:6000]

    # PDF attachment links (press releases occasionally link PDFs)
    pdf_url = None
    original_filename = None
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if ".pdf" in href.lower():
            pdf_url = href if href.startswith("http") else _BASE_URL + href
            fname = href.rstrip("/").split("/")[-1].split("?")[0]
            original_filename = fname if fname else None
            break

    return abstract, node_id, pub_date, pdf_url, original_filename


class LavoroGovItStampaEMediaCrawler(BaseCrawler):
    """Crawler for Ministero del Lavoro e delle Politiche Sociali — Comunicati."""

    site_id = _SITE_ID
    site_name = "Custom: lavoro-gov-it-stampa-e-media"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Walk paginated Comunicati list, fetch detail pages, save records.

        Pagination: ?page=0, ?page=1, … up to 200 pages (safety cap).
        Each list page has 20 items.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECONDS = 25 * 60  # 25-minute wall-clock budget

        for page_num in range(MAX_PAGES):
            # Wall-clock budget check
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-min budget reached at page {page_num}. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            # Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str}")

            list_url = f"{_LIST_URL}?page={page_num}"
            html = _curl_get(list_url)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch list page {page_num}. Stopping.")
                break

            try:
                soup = _make_soup(html)
            except Exception as exc:
                print(f"[{_SITE_ID}] Parse error on list page {page_num}: {exc}. Skipping.")
                continue

            if soup is None:
                print(f"[{_SITE_ID}] Could not parse list page {page_num}. Skipping.")
                continue

            rows = soup.find_all(class_="views-row")
            if not rows:
                print(f"[{_SITE_ID}] No rows at page {page_num}. Pagination complete.")
                break

            new_count = 0  # new (unseen) URLs on this page

            for row in rows:
                if limit is not None and saved >= limit:
                    break

                detail_url = "unknown"
                try:
                    link_tag = row.find("a", class_="search-item-link")
                    if not link_tag or not link_tag.get("href"):
                        continue
                    rel_path = link_tag["href"]
                    detail_url = (
                        _BASE_URL + rel_path if rel_path.startswith("/") else rel_path
                    )

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_count += 1

                    # Title
                    h2 = row.find("h2")
                    title = h2.get_text(strip=True) if h2 else ""
                    if not title:
                        continue

                    # listed_date from <time datetime="...">
                    time_tag = row.find("time")
                    listed_date = ""
                    if time_tag and time_tag.get("datetime"):
                        listed_date = _parse_it_date(time_tag["datetime"])

                    # Teaser/short description from list page
                    list_desc = ""
                    item_div = row.find("div", class_="item")
                    if item_div:
                        row_div = item_div.find("div", class_="row")
                        if row_div:
                            child_divs = row_div.find_all("div", recursive=False)
                            for d in reversed(child_divs):
                                txt = d.get_text(strip=True)
                                if txt and len(txt) > 20:
                                    list_desc = txt
                                    break

                    # Fetch detail page with per-attempt retry/backoff
                    abstract = ""
                    node_id = ""
                    pub_date = ""
                    pdf_url = None
                    original_filename = None

                    for attempt in range(3):
                        try:
                            detail_html = _curl_get(detail_url, retries=1)
                            if detail_html:
                                abstract, node_id, pub_date, pdf_url, original_filename = (
                                    _parse_detail(detail_html)
                                )
                                break
                        except Exception as det_exc:
                            print(
                                f"[{_SITE_ID}] Detail attempt {attempt + 1}/3 "
                                f"failed ({detail_url}): {det_exc}"
                            )
                            if attempt < 2:
                                time.sleep([1, 3, 9][attempt])

                    # Fall back to list teaser if detail body is thin
                    if not abstract or len(abstract) < 50:
                        abstract = list_desc

                    if not abstract or len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] Skipping '{title[:60]}' "
                            f"— abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    ext_id = node_id if node_id else rel_path.rstrip("/").split("/")[-1]
                    post_num = node_id if node_id else None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ext_id,
                        "post_number": post_num,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date or listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url or "",
                        "doi": "",
                        "department": _PUBLISHER,
                        "authors": "[]",
                        "keywords": "",
                        "category": "Comunicati",
                        "original_filename": original_filename or "",
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "originalFilename": original_filename,
                                "node_id": node_id,
                                "listed_date": listed_date,
                                "list_description": list_desc,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}: {title[:70]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Item failed ({detail_url}): {exc}")
                    continue

            # All URLs on this page already seen → loop detected, stop
            if new_count == 0 and page_num > 0:
                print(f"[{_SITE_ID}] All URLs on page {page_num} already seen. Stopping.")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
