# -*- coding: utf-8 -*-
"""DHS Congressional Appropriations Reports crawler.

Target:   https://www.dhs.gov/congressional-appropriations-reports
Strategy: playwright-based fetch (Akamai CDN blocks plain curl with 403).
  1. Fetch main listing page → extract year publication URLs (2015–present).
  2. For each year URL → fetch publication page → parse table of PDF attachments.
  3. Abstract = year-page meta-description + report title + FY context (always ≥ 100 chars).
"""

import json
import os
import re
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_START_URL = "https://www.dhs.gov/congressional-appropriations-reports"
_BASE_URL  = "https://www.dhs.gov"
_SITE_ID   = "dhs-gov-congressional-approp"


# ── HTML parsing helpers ─────────────────────────────────────────────────────

def _try_bs4(html: str):
    """Parse with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date_mdy(s: str) -> str:
    """MM/DD/YYYY → YYYY-MM-DD; return raw string on no match."""
    if not s:
        return ""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s.strip())
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s.strip()


# ── Network helpers ──────────────────────────────────────────────────────────

def _fetch_playwright(url: str, retries: int = 3) -> str | None:
    """Fetch URL via playwright with exponential back-off (1s, 3s, 9s)."""
    try:
        from crawler.playwright_fetcher import fetch_html
    except ImportError:
        print(f"[{_SITE_ID}] playwright_fetcher not available — install playwright")
        return None

    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            html = fetch_html(url, timeout_seconds=35, extra_wait_seconds=2.0)
            if html and len(html) > 500:
                return html
            msg = "empty/short response"
        except Exception as exc:
            msg = str(exc)

        if attempt < retries - 1:
            wait = waits[attempt]
            print(
                f"[{_SITE_ID}] playwright fetch attempt {attempt+1}/{retries} "
                f"for {url}: {msg} — retrying in {wait}s…"
            )
            time.sleep(wait)
        else:
            print(f"[{_SITE_ID}] playwright fetch failed after {retries} attempts for {url}: {msg}")
    return None


# ── Page parsers ─────────────────────────────────────────────────────────────

def _extract_year_urls(html: str) -> list:
    """Return ordered list of year publication URLs (newest first)."""
    urls: list = []

    # Fast regex path first (pattern is stable)
    for m in re.finditer(
        r'href="(/publication/(\d{4})-dhs-congressional-appropriations-reports)"',
        html,
    ):
        url = _BASE_URL + m.group(1)
        if url not in urls:
            urls.append(url)

    if not urls:
        soup = _try_bs4(html)
        if soup:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.match(r"/publication/\d{4}-dhs-congressional-appropriations-reports", href):
                    url = _BASE_URL + href
                    if url not in urls:
                        urls.append(url)

    return urls


def _parse_year_page(html: str, year_url: str) -> tuple:
    """Parse a year publication page.

    Returns
    -------
    (meta_desc, page_title, year, records)
    records: list of {'title': str, 'pdf_url': str, 'date_raw': str}
    """
    # Meta description
    meta_desc = ""
    desc_m = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', html)
    if not desc_m:
        desc_m = re.search(r'<meta\s+content="([^"]+)"\s+name="description"', html)
    if desc_m:
        meta_desc = desc_m.group(1).strip()

    # Year
    year_m = re.search(r"/publication/(\d{4})-dhs", year_url)
    year = year_m.group(1) if year_m else ""

    # Page title (strip trailing " | Homeland Security" suffix)
    ptitle_m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    page_title = ptitle_m.group(1).strip() if ptitle_m else f"{year} DHS Congressional Appropriations Reports"
    page_title = re.sub(r"\s*\|\s*Homeland Security.*$", "", page_title).strip()

    records: list = []

    # ── BS4 path ────────────────────────────────────────────────────────
    soup = _try_bs4(html)
    if soup:
        table = soup.find("table", attrs={"aria-label": lambda x: x and "Files" in x})
        if not table:
            table = soup.find("table")

        if table:
            for row in table.find_all("tr"):
                # Title cell is <th role="rowheader"> (not <td>)
                title_cell = row.find(["th", "td"], attrs={"role": "rowheader"})
                if not title_cell:
                    continue

                title_text = title_cell.get("data-sort-value", "").strip()
                if not title_text:
                    title_text = title_cell.get_text(strip=True)

                link = title_cell.find("a", href=True)
                if not link:
                    continue

                href = link["href"]
                if not href.lower().endswith(".pdf"):
                    continue

                pdf_url = href if href.startswith("http") else _BASE_URL + href

                date_raw = ""
                for cell in row.find_all(["td", "th"]):
                    text = cell.get_text(strip=True)
                    if re.match(r"\d{1,2}/\d{1,2}/\d{4}$", text):
                        date_raw = text
                        break

                records.append({"title": title_text, "pdf_url": pdf_url, "date_raw": date_raw})

            return meta_desc, page_title, year, records

    # ── Regex fallback ───────────────────────────────────────────────────
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        t_m = re.search(r'data-sort-value="([^"]+)"', row)
        p_m = re.search(
            r'href="(https://www\.dhs\.gov/sites/default/files/[^"]+\.pdf)"',
            row,
        )
        d_m = re.search(r"(\d{2}/\d{2}/\d{4})", row)
        if t_m and p_m:
            records.append({
                "title":    t_m.group(1).strip(),
                "pdf_url":  p_m.group(1),
                "date_raw": d_m.group(1) if d_m else "",
            })

    return meta_desc, page_title, year, records


# ── Crawler class ────────────────────────────────────────────────────────────

class DHSCongressionalAppropCrawler(BaseCrawler):
    """DHS Congressional Appropriations Reports (2015–present)."""

    site_id   = "dhs-gov-congressional-approp"
    site_name = "Custom: dhs-gov-congressional-approp"
    base_url  = "https://www.dhs.gov"

    def crawl(self, limit=None):
        """Crawl DHS Congressional Appropriations Reports.

        Parameters
        ----------
        limit : int or None
            Maximum records to save (None = unlimited).
        """
        limit_str  = str(limit) if limit is not None else "inf"
        saved      = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_SECONDS = 24 * 60   # 24-minute wall-clock budget
        SAFETY_CAP  = 200       # page-level safety cap

        # ── Step 1: main listing page ────────────────────────────────────
        print(f"[{self.site_id}] Fetching main listing page: {_START_URL}")
        main_html = _fetch_playwright(_START_URL)
        if not main_html:
            print(f"[{self.site_id}] Failed to fetch main listing page. Aborting.")
            return 0

        year_urls = _extract_year_urls(main_html)
        if not year_urls:
            print(f"[{self.site_id}] No year pages found on main listing page. Aborting.")
            return 0

        print(f"[{self.site_id}] Found {len(year_urls)} year pages.")

        # ── Step 2: iterate year pages ───────────────────────────────────
        for page_num, year_url in enumerate(year_urls, 1):
            if limit is not None and saved >= limit:
                break

            if page_num > SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {SAFETY_CAP} pages reached, stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > MAX_SECONDS:
                print(
                    f"[{self.site_id}] Time budget of {MAX_SECONDS // 60}min exceeded "
                    f"after {elapsed / 60:.1f}min. Stopping."
                )
                break

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            time.sleep(self._delay)

            try:
                year_html = _fetch_playwright(year_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] Error fetching {year_url}: {exc}")
                continue

            if not year_html:
                print(f"[{self.site_id}] Skipping {year_url}: no HTML returned")
                continue

            try:
                meta_desc, page_title, year, records = _parse_year_page(year_html, year_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] Error parsing {year_url}: {exc}")
                continue

            if not records:
                print(f"[{self.site_id}] page {page_num} ({year}): 0 records — skipping")
                continue

            print(f"[{self.site_id}] page {page_num} ({year}): {len(records)} records found")

            # ── Step 3: save records from this year page ─────────────────
            for record in records:
                if limit is not None and saved >= limit:
                    break

                try:
                    pdf_url = record["pdf_url"]

                    # URL-level deduplication (prevents infinite loops on
                    # paginators that silently loop back to page 1)
                    if pdf_url in seen_urls:
                        continue
                    seen_urls.add(pdf_url)

                    title = record["title"].strip()
                    if not title:
                        print(f"[{self.site_id}] Skipping record with empty title on {year_url}")
                        continue

                    # Build abstract — guaranteed ≥ 100 chars by construction:
                    #   meta_desc (≥0) + "Report title: …" + FY boilerplate (≥90).
                    abstract_parts = []
                    if meta_desc:
                        abstract_parts.append(meta_desc)
                    abstract_parts.append(f"Report title: {title}.")
                    abstract_parts.append(
                        f"Fiscal Year: {year}. "
                        "Required by the Committees on Appropriations of the "
                        "Senate and the House of Representatives."
                    )
                    abstract = " ".join(abstract_parts)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping '{title[:50]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Date
                    date_raw = record.get("date_raw", "")
                    date_iso = _parse_date_mdy(date_raw)

                    # Agency sub-unit = text before first em-dash or hyphen separator
                    agency = ""
                    for sep in (" – ", " — ", " - "):
                        if sep in title:
                            agency = title.split(sep)[0].strip()
                            break
                    if not agency:
                        agency = "U.S. Department of Homeland Security"

                    # PDF filename
                    pdf_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                    pdf_filename = pdf_filename.replace("%20", "_")

                    # external_id = path below dhs.gov (stable, unique per PDF)
                    external_id = pdf_url.replace("https://www.dhs.gov/", "")

                    # post_number — no numeric post IDs on DHS; use filename slug
                    post_number = pdf_filename.replace(".pdf", "")

                    paper = {
                        "id":               None,
                        "site_id":          self.site_id,
                        "external_id":      external_id,
                        "post_number":      post_number,
                        "title":            title,
                        "abstract":         abstract,
                        "published_date":   date_iso,
                        "listed_date":      date_iso,
                        "url":              year_url,
                        "pdf_url":          pdf_url,
                        "publisher":        "U.S. Department of Homeland Security",
                        "department":       agency,
                        "authors":          "",
                        "keywords":         f"congressional appropriations, {year}, DHS, homeland security",
                        "category":         "Congressional Appropriations Report",
                        "doi":              "",
                        "original_filename": pdf_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date":     date_raw,
                                "originalFilename": pdf_filename,
                                "fiscal_year":     year,
                                "page_title":      page_title,
                                "parent_url":      year_url,
                                "agency":          agency,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {title[:60]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({record.get('title', '')[:50]}): {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
