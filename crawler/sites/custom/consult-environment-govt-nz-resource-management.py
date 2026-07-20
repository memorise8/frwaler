# -*- coding: utf-8 -*-
"""Crawler for consult.environment.govt.nz – Ministry for the Environment (CitizenSpace)."""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler

SITE_ID = "consult-environment-govt-nz-resource-management"


def _make_soup(html: str):
    """Build BeautifulSoup with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def _parse_date(raw: str) -> str:
    """Parse 'DD Mon YYYY' or 'Mon DD, YYYY' → 'YYYY-MM-DD'. Strips status prefixes."""
    if not raw:
        return ""
    raw = re.sub(r"^(Closed|Opened|Open|Close|Updated)\s*", "", raw.strip(), flags=re.IGNORECASE).strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


class ConsultEnvironmentNZCrawler(BaseCrawler):
    site_id = SITE_ID
    site_name = "Custom: consult-environment-govt-nz-resource-management"
    base_url = "https://consult.environment.govt.nz"

    _PAGE_SIZE = 30

    def _fetch_url(self, url: str, retries: int = 3) -> str | None:
        """Fetch URL with exponential backoff. Returns HTML text or None."""
        for attempt in range(retries):
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                try:
                    return resp.content.decode("utf-8")
                except UnicodeDecodeError:
                    return resp.content.decode("utf-8", errors="replace")
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 1 * (3 ** attempt)
                    print(f"[{SITE_ID}] Fetch error ({url}): {exc}. Retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{SITE_ID}] Failed after {retries} attempts: {url}: {exc}")
                    return None

    def _extract_abstract(self, soup) -> str:
        """Extract full abstract text from a detail page soup object."""
        # The layout: row > [col-md-8 order-2 (main), col-md-4 order-1 (sidebar)]
        sidebar = soup.find(class_="cs-consultation-sidebar-container")
        if sidebar:
            sidebar_col = sidebar.parent  # col-md-4 order-1
            row = sidebar_col.parent if sidebar_col else None
            if row:
                main_col = row.find(class_="order-2")
                if not main_col:
                    # Try any col-md-8 that isn't the sidebar column
                    for div in row.find_all(class_="col-md-8"):
                        if div is not sidebar_col:
                            main_col = div
                            break
                if main_col:
                    return main_col.get_text(separator=" ", strip=True)

        # Fallback: pick col-md-8 divs with substantial content, skip nav
        texts = []
        for col in soup.find_all(class_="col-md-8"):
            t = col.get_text(separator=" ", strip=True)
            if len(t) > 100 and not t.startswith("Menu"):
                texts.append(t)
        return " ".join(texts)

    def _extract_dates(self, soup):
        """Return (published_date, closed_date) strings from a detail page soup."""
        primary = soup.find(class_="cs-consultation-sidebar-primary-date")
        secondary = soup.find(class_="cs-consultation-sidebar-secondary-date")
        closed_date = _parse_date(primary.get_text(strip=True) if primary else "")
        opened_date = _parse_date(secondary.get_text(strip=True) if secondary else "")
        return opened_date, closed_date

    def crawl(self, limit=None):
        """Crawl all consultations from the CitizenSpace finder, paginating via b_start."""
        b_start = 0
        saved = 0
        page_num = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        MAX_PAGES = 200
        inf_str = str(limit) if limit is not None else "∞"

        while True:
            # 25-minute wall-clock budget
            if time.time() - start_time > 25 * 60:
                print(f"[{SITE_ID}] 25-minute budget reached, stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num >= MAX_PAGES:
                print(f"[{SITE_ID}] Safety cap of {MAX_PAGES} pages reached.")
                break

            finder_url = f"{self.base_url}/consultation_finder/?b_start={b_start}"
            time.sleep(self._delay)

            raw = self._fetch_url(finder_url)
            if not raw:
                print(f"[{SITE_ID}] Failed to fetch finder at b_start={b_start}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{SITE_ID}] Soup parse error for finder page: {exc}")
                break

            ul = soup.find("ul", id="consultations")
            if not ul:
                print(f"[{SITE_ID}] No #consultations list at b_start={b_start}. Stopping.")
                break

            items = ul.find_all("li", recursive=False)
            if not items:
                print(f"[{SITE_ID}] Empty list at b_start={b_start}. Done.")
                break

            new_on_page = 0

            for li in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    h2 = li.find("h2")
                    if not h2:
                        continue
                    a_tag = h2.find("a")
                    if not a_tag:
                        continue

                    url = a_tag.get("href", "").strip()
                    if not url:
                        continue

                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    title = a_tag.get_text(strip=True)
                    slug = url.rstrip("/").split("/")[-1]

                    # Date from list item
                    date_div = li.find(class_="cs-date-delta")
                    listed_date_raw = date_div.get_text(strip=True) if date_div else ""
                    listed_date_iso = _parse_date(listed_date_raw)

                    # Snippet from list item (used as fallback abstract)
                    snippet = ""
                    col9 = li.find(class_="col-md-9")
                    if col9:
                        span = col9.find("span")
                        if span:
                            snippet = span.get_text(separator=" ", strip=True)

                    # Status from data attribute
                    status = li.get("data-consultation-state", "") or ""

                    # Category from URL (second-to-last path segment)
                    url_parts = url.rstrip("/").split("/")
                    category = url_parts[-2] if len(url_parts) >= 2 else ""

                    # Fetch detail page
                    time.sleep(self._delay)
                    detail_raw = self._fetch_url(url)
                    if not detail_raw:
                        print(f"[{SITE_ID}] Detail fetch failed for {url}, skipping.")
                        continue

                    try:
                        detail_soup = _make_soup(detail_raw)
                    except Exception as exc:
                        print(f"[{SITE_ID}] Soup parse failed for {url}: {exc}")
                        continue

                    abstract = self._extract_abstract(detail_soup)

                    # Fallback to list snippet if detail extraction is thin
                    if len(abstract) < 50 and len(snippet) >= 50:
                        abstract = snippet

                    if len(abstract) < 50:
                        print(f"[{SITE_ID}] Skipping {slug}: abstract too short ({len(abstract)} chars)")
                        continue

                    published_date, closed_date = self._extract_dates(detail_soup)

                    # If no opened date, use the listing date as published_date
                    if not published_date and listed_date_iso:
                        published_date = listed_date_iso

                    # posted_date = when it appeared on the list (closed/listing date)
                    posted_date = closed_date or listed_date_iso

                    # PDF links from detail page
                    pdf_links = [
                        a["href"] for a in detail_soup.find_all("a", href=True)
                        if a["href"].lower().endswith(".pdf")
                    ]
                    pdf_url = pdf_links[0] if pdf_links else None
                    original_filename = (
                        pdf_url.rstrip("/").split("/")[-1] if pdf_url else None
                    )

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": posted_date,
                        "url": url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "category": category,
                        "publisher": "Ministry for the Environment",
                        "authors": None,
                        "keywords": None,
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date_raw,
                                "slug": slug,
                                "status": status,
                                "category": category,
                                "pdfLinks": pdf_links,
                                "closedDate": closed_date,
                                "openedDate": published_date,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{SITE_ID}] Saved {saved}/{inf_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{SITE_ID}] Item failed: {exc}")
                    continue

            if page_num % 10 == 0:
                print(f"[{SITE_ID}] page {page_num}: saved {saved}/{inf_str}")

            if new_on_page == 0:
                print(f"[{SITE_ID}] No new items on page {page_num}. Done.")
                break

            b_start += self._PAGE_SIZE
            page_num += 1

        print(f"[{SITE_ID}] Done. Total saved: {saved}")
        return saved
