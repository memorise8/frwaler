# -*- coding: utf-8 -*-
"""Bundeskanzleramt Austria news crawler (nachrichten-der-bundesregierung)."""

import json
import os
import re
import subprocess
import time

try:
    from bs4 import BeautifulSoup
    _BS_AVAILABLE = True
except ImportError:
    _BS_AVAILABLE = False

from crawler.base_crawler import BaseCrawler


_MONTHS_DE = {
    "jänner": "01", "januar": "01",
    "februar": "02",
    "märz": "03",
    "april": "04",
    "mai": "05",
    "juni": "06",
    "juli": "07",
    "august": "08",
    "september": "09",
    "oktober": "10",
    "november": "11",
    "dezember": "12",
}

_SITE_ID = "bundeskanzleramt-gv-at-bundeskanzleramt"
_BASE_URL = "https://www.bundeskanzleramt.gv.at"
_LIST_BASE = "/bundeskanzleramt/nachrichten-der-bundesregierung"
_START_PATH = f"{_LIST_BASE}/2024.html"


def _make_soup(html: str):
    """Build BeautifulSoup with html5lib → lxml → html.parser fallback."""
    if not _BS_AVAILABLE:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with exponential-backoff retries."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace").strip()
            if text:
                return text
        except Exception:
            pass
        if attempt < retries - 1:
            wait = 1 * (3 ** attempt)  # 1s, 3s, 9s
            time.sleep(wait)
    return None


def _parse_german_date(raw: str) -> str:
    """Convert '30. Dezember 2024' → '2024-12-30'. Returns '' on failure."""
    s = (raw or "").strip().lower()
    m = re.match(r"(\d{1,2})\.\s+(\w+)\s+(\d{4})", s)
    if not m:
        return ""
    day, month_name, year = m.groups()
    month = _MONTHS_DE.get(month_name, "")
    if not month:
        return ""
    return f"{year}-{month}-{int(day):02d}"


def _discover_year_slugs(soup) -> list:
    """Extract year-archive slugs from the sidebar navigation on the listing page."""
    slugs = []
    seen = set()
    pattern = re.compile(
        r"/bundeskanzleramt/nachrichten-der-bundesregierung/([\d]{4}(?:-[\d]{4})?|[\d]{4})\.html"
    )
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = pattern.match(href)
        if m:
            slug = m.group(1)
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    # Sort descending so we crawl newest-first; 2017-2018 sorts last
    def _sort_key(s):
        return -int(s.split("-")[0])
    slugs.sort(key=_sort_key)
    return slugs


class BundeskanzleramtGvAtBundeskanzleramtCrawler(BaseCrawler):
    """Crawler for Bundeskanzleramt Austria government news."""

    site_id = "bundeskanzleramt-gv-at-bundeskanzleramt"
    site_name = "Custom: bundeskanzleramt-gv-at-bundeskanzleramt"
    base_url = "https://www.bundeskanzleramt.gv.at"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget
        SAFETY_CAP = 200       # max year-pages processed

        limit_or_inf = limit if limit is not None else "∞"

        # ── Step 1: fetch start page to discover all year slugs ────────────
        start_url = f"{self.base_url}{_START_PATH}"
        raw = _curl_get(start_url)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch start page {start_url}")
            return 0

        start_soup = _make_soup(raw)
        if not start_soup:
            print(f"[{_SITE_ID}] Failed to parse start page")
            return 0

        year_slugs = _discover_year_slugs(start_soup)
        if not year_slugs:
            # Hard-coded fallback if discovery fails
            year_slugs = ["2026", "2025", "2024", "2023", "2022", "2021",
                          "2020", "2019", "2017-2018"]

        print(f"[{_SITE_ID}] Year archives discovered: {year_slugs}")

        # ── Step 2: walk year pages ─────────────────────────────────────────
        page_num = 0
        for year_slug in year_slugs:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-min budget reached, exiting cleanly.")
                break

            page_num += 1
            if page_num > SAFETY_CAP:
                print(f"[{_SITE_ID}] Safety cap of {SAFETY_CAP} year-pages reached, stopping.")
                break

            if page_num % 10 == 1:
                print(f"[{_SITE_ID}] page {page_num}: year={year_slug}, "
                      f"saved {saved}/{limit_or_inf}")

            # Reuse the already-fetched soup for 2024; fetch others fresh
            if year_slug == "2024" and start_soup is not None:
                list_soup = start_soup
                start_soup = None  # free it after first use
            else:
                list_url = f"{self.base_url}{_LIST_BASE}/{year_slug}.html"
                raw_list = _curl_get(list_url)
                if not raw_list:
                    print(f"[{_SITE_ID}] Failed to fetch listing {year_slug}, skipping.")
                    continue
                list_soup = _make_soup(raw_list)
                if not list_soup:
                    print(f"[{_SITE_ID}] Failed to parse listing {year_slug}, skipping.")
                    continue

            # Collect article cards from the listing page
            cards = []
            for a_tag in list_soup.find_all("a", class_="card-link"):
                href = (a_tag.get("href") or "").strip()
                if not href:
                    continue
                full_url = (_BASE_URL + href) if href.startswith("/") else href
                if full_url in seen_urls:
                    continue

                date_elem = a_tag.find("small", class_="card-date")
                listed_date_raw = date_elem.get_text(strip=True) if date_elem else ""
                listed_date = _parse_german_date(listed_date_raw)

                title_elem = a_tag.find("h2", class_="card-title-heading")
                listing_title = (
                    title_elem.get_text(separator=" ", strip=True) if title_elem else ""
                )

                teaser_elem = a_tag.find("p", class_="card-text")
                teaser = teaser_elem.get_text(strip=True) if teaser_elem else ""

                cards.append({
                    "url": full_url,
                    "listed_date": listed_date,
                    "listing_title": listing_title,
                    "teaser": teaser,
                })

            if not cards:
                print(f"[{_SITE_ID}] No articles found in year {year_slug}.")
                continue

            print(f"[{_SITE_ID}] year {year_slug}: {len(cards)} articles.")

            # ── Step 3: fetch each detail page ──────────────────────────────
            for card in cards:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_SECONDS:
                    print(f"[{_SITE_ID}] Time budget reached mid-year, stopping.")
                    break

                url = card["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)

                    # external_id / post_number: slug from URL path
                    slug = url.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")

                    detail_raw = _curl_get(url)
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {url} failed: fetch returned None")
                        continue

                    detail_soup = _make_soup(detail_raw)
                    if not detail_soup:
                        print(f"[{_SITE_ID}] item {url} failed: could not parse HTML")
                        continue

                    # Title
                    title_span = detail_soup.find("span", class_="title")
                    if title_span:
                        title = title_span.get_text(separator=" ", strip=True)
                    else:
                        title = card["listing_title"]
                    if not title:
                        title = card["listing_title"]

                    # Published date from <time class="datetime" datetime="YYYY-MM-DD">
                    time_elem = detail_soup.find("time", class_="datetime")
                    published_date = ""
                    if time_elem:
                        published_date = (time_elem.get("datetime") or "").strip()
                    if not published_date:
                        published_date = card["listed_date"]

                    # Teaser abstract: <p class="abstract">
                    abstract_elem = detail_soup.find("p", class_="abstract")
                    abstract_text = abstract_elem.get_text(strip=True) if abstract_elem else ""

                    # Body: <div class="richtext_output">
                    body_elem = detail_soup.find("div", class_="richtext_output")
                    body_text = ""
                    if body_elem:
                        paragraphs = []
                        for elem in body_elem.find_all(["p", "h2", "h3", "h4", "li"]):
                            t = elem.get_text(separator=" ", strip=True)
                            if t:
                                paragraphs.append(t)
                        body_text = "\n\n".join(paragraphs)

                    # Combine for full abstract
                    parts = [p for p in [abstract_text, body_text] if p]
                    full_abstract = "\n\n".join(parts)

                    if len(full_abstract) < 50:
                        print(f"[{_SITE_ID}] Abstract <50 chars for {url}, skipping.")
                        continue

                    # Keywords from meta
                    keywords = None
                    meta_kw = detail_soup.find("meta", {"name": "keywords"})
                    if meta_kw:
                        kw_val = (meta_kw.get("content") or "").strip()
                        if kw_val:
                            keywords = kw_val

                    # PDF link (search inside richtext body first, then whole page)
                    pdf_url = None
                    original_filename = None
                    search_scope = body_elem if body_elem else detail_soup
                    for a_link in search_scope.find_all("a", href=True):
                        href = a_link["href"]
                        if href.lower().endswith(".pdf"):
                            pdf_url = (_BASE_URL + href) if href.startswith("/") else href
                            original_filename = (
                                href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
                            )
                            break

                    meta_dict = {
                        "posted_date": card["listed_date"],
                        "teaser": card["teaser"],
                        "year_archive": year_slug,
                    }
                    if original_filename:
                        meta_dict["originalFilename"] = original_filename

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": full_abstract,
                        "published_date": published_date,
                        "posted_date": card["listed_date"],
                        "url": url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": keywords,
                        "publisher": "Bundeskanzleramt Österreich",
                        "category": "Nachrichten der Bundesregierung",
                        "metadata": json.dumps(meta_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{_SITE_ID}] Saved {counter}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
