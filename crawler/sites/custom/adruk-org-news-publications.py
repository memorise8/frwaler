# -*- coding: utf-8 -*-
"""ADR UK Impact Case Studies crawler.

Starting URL : https://www.adruk.org/news-publications/impact-case-studies/
Pagination   : https://www.adruk.org/our-mission/our-impact/{N}/  (N = 1, 2, …)
10 items/page, ~114 total (~12 pages).
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

_SITE_ID = "adruk-org-news-publications"
_BASE_URL = "https://www.adruk.org"
_LIST_PAGE_0 = "https://www.adruk.org/news-publications/impact-case-studies/"
_LIST_PAGE_N = "https://www.adruk.org/our-mission/our-impact/{n}/"
_PUBLISHER = "ADR UK"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers (module-level, keep re-usable without self)
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


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-max 1.3 and exponential-backoff retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-GB,en;q=0.9",
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
    """'22 April 2026' or 'April 2026' → 'YYYY-MM-DD'. Never raises."""
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


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Parser functions
# ---------------------------------------------------------------------------

def _parse_listing(html: str) -> list[dict]:
    """Return list of {url, slug, listed_date_raw, list_title, list_abstract}."""
    records = []
    soup = _make_soup(html)
    if soup is None:
        return records
    for rec in soup.find_all("div", class_=re.compile(r"record\s+model-impact-cases")):
        a = rec.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        # Only real article links, not filter links
        if not re.match(r"^/our-mission/our-impact/[^?#]+/$", href):
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug:
            continue
        date_span = rec.find("span", class_="date")
        listed_date_raw = date_span.get_text(strip=True) if date_span else ""
        h2 = rec.find("h2")
        list_title = h2.get_text(strip=True) if h2 else ""
        # Short abstract from listing page
        paras = rec.find_all("p")
        list_abstract = " ".join(
            p.get_text(strip=True) for p in paras
            if "fauxLink" not in " ".join(p.get("class") or [])
        )
        records.append({
            "url": _BASE_URL + href,
            "slug": slug,
            "listed_date_raw": listed_date_raw,
            "list_title": list_title,
            "list_abstract": list_abstract,
        })
    return records


def _parse_detail(html: str) -> dict:
    """Extract title, abstract, authors, categories, pdf_url, doi from detail page."""
    result = {
        "title": "",
        "abstract": "",
        "authors": [],
        "categories": [],
        "pdf_url": None,
        "original_filename": None,
        "date_raw": "",
        "doi": "",
    }
    soup = _make_soup(html)
    if soup is None:
        return result

    # Title
    h1 = soup.find("h1")
    if h1:
        span = h1.find("span")
        result["title"] = (span or h1).get_text(strip=True)

    # Main content block
    main = soup.find("div", class_=re.compile(r"news\s+single\s+model-impact-cases"))
    if main is None:
        main = soup.find("div", class_="tx-llcatalog-pi") or soup

    # Date
    date_span = main.find("span", class_="date")
    result["date_raw"] = date_span.get_text(strip=True) if date_span else ""

    # Categories / tags
    cat_p = main.find("p", class_="category")
    if cat_p:
        result["categories"] = [a.get_text(strip=True) for a in cat_p.find_all("a")]

    # Content div — contains the full body
    content = main.find("div", class_="content")
    if content is None:
        content = main

    # Remove the image div so it doesn't pollute text
    for img_div in content.find_all("div", class_="image"):
        img_div.decompose()

    # Extract authors and build full abstract
    text_parts = []
    for tag in content.find_all(["p", "h3", "h4", "ul", "li"]):
        text = tag.get_text(separator=" ", strip=True)
        if not text:
            continue
        # Detect author line
        if tag.name == "p" and re.match(r"Author[s]?\s*:", text, re.IGNORECASE):
            author_raw = re.sub(r"^Author[s]?\s*:\s*", "", text, flags=re.IGNORECASE).strip()
            # Split on " and " or ";" preserving full names
            parts = re.split(r"\s+and\s+|;\s*", author_raw)
            result["authors"] = [p.strip() for p in parts if p.strip()]
        text_parts.append(text)

    result["abstract"] = " ".join(text_parts)

    # DOI
    doi_match = re.search(r"https?://doi\.org/(10\.[^\s\"'<>]+)", html)
    if doi_match:
        result["doi"] = doi_match.group(1).rstrip(")")

    # PDF link
    for a in main.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            url = href if href.startswith("http") else _BASE_URL + href
            result["pdf_url"] = url
            result["original_filename"] = url.rstrip("/").split("/")[-1].split("?")[0]
            break

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class AdrukOrgNewsPublicationsCrawler(BaseCrawler):
    site_id = "adruk-org-news-publications"
    site_name = "Custom: adruk-org-news-publications"
    base_url = "https://www.adruk.org"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_display = limit if limit is not None else "∞"
        start_time = time.time()
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        page = 0  # 0 → first listing URL, N → /our-mission/our-impact/N/

        while True:
            # --- Guards ---
            if limit is not None and saved >= limit:
                break
            if page >= MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                break

            # --- Fetch listing page ---
            list_url = _LIST_PAGE_0 if page == 0 else _LIST_PAGE_N.format(n=page)
            if page > 0:
                time.sleep(1.0)

            html = _curl_get(list_url)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch listing page {page + 1} ({list_url}). Stopping.")
                break

            records = _parse_listing(html)
            if not records:
                print(f"[{_SITE_ID}] No records on page {page + 1}. Done.")
                break

            # Deduplicate across pages
            new_records = [r for r in records if r["url"] not in seen_urls]
            if not new_records:
                print(f"[{_SITE_ID}] All URLs on page {page + 1} already seen. Stopping.")
                break
            for r in new_records:
                seen_urls.add(r["url"])

            if (page + 1) % 10 == 0:
                print(f"[{_SITE_ID}] page {page + 1}: saved {saved}/{limit_display}")

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

                    title = detail["title"] or rec["list_title"]
                    abstract = detail["abstract"] or rec["list_abstract"]

                    if not abstract or len(abstract) < 50:
                        print(f"[{_SITE_ID}] Abstract <50 chars for {rec['url']}. Skipping.")
                        continue

                    published_date = _parse_date(detail["date_raw"] or rec["listed_date_raw"])
                    listed_date = _parse_date(rec["listed_date_raw"])

                    authors_str = "; ".join(detail["authors"])
                    categories = detail["categories"]
                    keywords = ", ".join(categories) if categories else ""
                    category = categories[0] if categories else ""

                    slug = rec["slug"]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": rec["url"],
                        "pdf_url": detail["pdf_url"] or "",
                        "original_filename": detail["original_filename"] or "",
                        "authors": authors_str,
                        "publisher": _PUBLISHER,
                        "department": "",
                        "journal": "",
                        "keywords": keywords,
                        "category": category,
                        "doi": detail["doi"],
                        "metadata": json.dumps({
                            "posted_date": rec["listed_date_raw"],
                            "originalFilename": detail["original_filename"],
                            "categories": categories,
                            "slug": slug,
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
