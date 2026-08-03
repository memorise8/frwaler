# -*- coding: utf-8 -*-
"""World Trade Institute (WTI) Research Publications crawler.

Starting URL : https://www.wti.org/research/publications/?q=&type=2&author=&research_project=
Pagination   : https://www.wti.org/research/publications/?type=2&page=N
~311 total, 16 pages, ~20 items/page.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "wti-org-research"
_BASE_URL = "https://www.wti.org"
_LIST_URL_P1 = "https://www.wti.org/research/publications/?q=&type=2&author=&research_project="
_LIST_URL_PN = "https://www.wti.org/research/publications/?type=2&page={page}"
_PUBLISHER = "World Trade Institute"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3):
    """Fetch URL via curl with TLS-max 1.3 and exponential-backoff retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
        except Exception as exc:
            if attempt >= retries - 1:
                print(f"[{_SITE_ID}] curl error: {exc}")
                return None
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            print(f"[{_SITE_ID}] empty/error for {url}, retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
    return None


def _parse_date(raw: str) -> str:
    """'22 Sep 2025', '22 September 2025', etc → 'YYYY-MM-DD'. Never raises."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %Y", "%b %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d") if "%d" in fmt else dt.strftime("%Y-%m-01")
        except ValueError:
            continue
    return raw


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _parse_listing(html: str) -> list:
    """Return list of {url, pub_id, slug, list_intro} from a publications list page."""
    records = []
    soup = _make_soup(html)
    if soup is None:
        return records

    for article in soup.find_all("article", class_=re.compile(r"publication-item")):
        h3 = article.find("h3")
        if not h3:
            continue
        a = h3.find("a", href=True)
        if not a:
            continue
        href = a["href"]

        # Extract numeric ID from URL path: /research/publications/1485/slug/
        m = re.search(r"/research/publications/(\d+)/", href)
        if not m:
            continue
        pub_id = m.group(1)

        url = _BASE_URL + href if href.startswith("/") else href
        slug = href.rstrip("/").rsplit("/", 1)[-1]

        intro_p = article.find("p", class_="intro")
        list_intro = intro_p.get_text(strip=True) if intro_p else ""

        records.append({
            "url": url,
            "pub_id": pub_id,
            "slug": slug,
            "list_intro": list_intro,
        })

    return records


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _parse_detail(html: str) -> dict:
    """Extract full metadata from a WTI publication detail page."""
    result = {
        "title": "",
        "abstract": "",
        "authors": [],
        "date_raw": "",
        "category": "",
        "pdf_url": None,
        "original_filename": None,
        "doi": "",
    }
    soup = _make_soup(html)
    if soup is None:
        return result

    # og:description is a reliable abstract fallback
    og_desc = ""
    og_meta = soup.find("meta", property="og:description")
    if og_meta:
        og_desc = (og_meta.get("content") or "").strip()

    # Main detail section
    section = soup.find("section", id="publications")
    if section is None:
        section = soup

    # Title from <h1>
    h1 = section.find("h1")
    if h1:
        result["title"] = h1.get_text(strip=True)

    # <p class="info"> contains date, category, authors
    info_p = section.find("p", class_="info")
    if info_p:
        strong = info_p.find("strong")
        if strong:
            result["date_raw"] = strong.get_text(strip=True)

        # Category: text between </strong> and <br> or author links
        info_raw = str(info_p)
        cat_m = re.search(r"</strong>\s*\n?\s*\|\s*([^\n<]+)", info_raw)
        if cat_m:
            result["category"] = cat_m.group(1).strip()

        # Authors from <a class="author">
        result["authors"] = [
            a.get_text(strip=True)
            for a in info_p.find_all("a", class_="author")
        ]

    # Abstract from <div class="left"> — remove the PDF button first
    left_div = section.find("div", class_="left")
    if left_div:
        # Remove button links (PDF download buttons)
        for btn_p in left_div.find_all("p"):
            if btn_p.find("a", class_="button"):
                btn_p.decompose()
        paras = [
            p.get_text(separator=" ", strip=True)
            for p in left_div.find_all(["p", "div"])
            if p.get_text(strip=True)
        ]
        abstract_text = " ".join(paras)
        result["abstract"] = re.sub(r"\s+", " ", abstract_text).strip()

    # Fallback: og:description
    if not result["abstract"] and og_desc:
        result["abstract"] = og_desc

    # DOI from any doi.org link in the page
    doi_m = re.search(r"https?://doi\.org/(10\.[^\s\"'<>]+)", html)
    if doi_m:
        result["doi"] = doi_m.group(1).rstrip(")")

    # PDF: look in the entire section for <a href="...pdf"> or /filer_public/ links
    search_area = section if section is not soup else soup
    for a in search_area.find_all("a", href=True):
        href_val = a.get("href", "")
        if href_val.lower().endswith(".pdf") or "/filer_public/" in href_val:
            pdf_full = href_val if href_val.startswith("http") else _BASE_URL + href_val
            result["pdf_url"] = pdf_full
            result["original_filename"] = href_val.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            break

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class WtiOrgResearchCrawler(BaseCrawler):
    site_id = "wti-org-research"
    site_name = "Custom: wti-org-research"
    base_url = "https://www.wti.org"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_display = limit if limit is not None else "inf"
        start_time = time.time()
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        page = 1

        while True:
            # --- Guards ---
            if limit is not None and saved >= limit:
                break
            if page > MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                break

            # --- Fetch listing page ---
            list_url = _LIST_URL_P1 if page == 1 else _LIST_URL_PN.format(page=page)
            if page > 1:
                time.sleep(1.0)

            html = _curl_get(list_url)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch listing page {page} ({list_url}). Stopping.")
                break

            records = _parse_listing(html)
            if not records:
                print(f"[{_SITE_ID}] No records on page {page}. Done.")
                break

            # Deduplicate across pages (guard against paginator looping back)
            new_records = [r for r in records if r["url"] not in seen_urls]
            if not new_records:
                print(f"[{_SITE_ID}] All URLs on page {page} already seen. Stopping.")
                break
            for r in new_records:
                seen_urls.add(r["url"])

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            # --- Fetch & save each item ---
            for rec in new_records:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_SECONDS:
                    print(f"[{_SITE_ID}] 25-minute budget reached mid-page. Stopping.")
                    break

                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(rec["url"])
                    if not detail_html:
                        print(f"[{_SITE_ID}] Failed to fetch {rec['url']}. Skipping.")
                        continue

                    detail = _parse_detail(detail_html)

                    title = detail["title"]
                    abstract = detail["abstract"]

                    if not abstract or len(abstract) < 50:
                        print(f"[{_SITE_ID}] Abstract <50 chars for {rec['url']}. Skipping.")
                        continue

                    published_date = _parse_date(detail["date_raw"])
                    authors_str = "; ".join(detail["authors"])

                    paper = {
                        "site_id": self.site_id,
                        "external_id": rec["pub_id"],
                        "post_number": rec["pub_id"],
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": published_date,
                        "url": rec["url"],
                        "pdf_url": detail["pdf_url"] or "",
                        "original_filename": detail["original_filename"] or "",
                        "authors": authors_str,
                        "publisher": _PUBLISHER,
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": detail["category"],
                        "doi": detail["doi"],
                        "metadata": json.dumps({
                            "posted_date": detail["date_raw"],
                            "originalFilename": detail["original_filename"],
                            "list_intro": rec["list_intro"],
                            "slug": rec["slug"],
                            "pub_id": rec["pub_id"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_display}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {rec.get('url', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
