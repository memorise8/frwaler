# -*- coding: utf-8 -*-
"""SGU (Geological Survey of Sweden) English news crawler.

Starting URL: https://www.sgu.se/en/about-sgu/news-from-sgu/
Pagination:   ?page=N  (currently 6 pages)
Detail pages: /en/about-sgu/news-from-sgu/YEAR/MONTH/SLUG/
"""

import json
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.sgu.se/en/about-sgu/news-from-sgu/"
_BASE_URL = "https://www.sgu.se"

_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&rsquo;": "’", "&lsquo;": "‘",
    "&ndash;": "–", "&mdash;": "—",
    "&rdquo;": "”", "&ldquo;": "“",
    "&nbsp;": " ", "&aelig;": "\xe6", "&oslash;": "\xf8",
    "&aring;": "\xe5", "&Aelig;": "\xc6", "&Oslash;": "\xd8",
    "&Aring;": "\xc5", "&auml;": "\xe4", "&ouml;": "\xf6",
    "&uuml;": "\xfc", "&Auml;": "\xc4", "&Ouml;": "\xd6",
}


def _make_soup(html):
    """Build BeautifulSoup with fallback: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url, retries=3):
    """GET via curl with TLS compatibility. Returns decoded string or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                    "-H", (
                        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "-H", "Accept-Language: en-US,en;q=0.9",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            body = result.stdout
            if body:
                try:
                    return body.decode("utf-8")
                except UnicodeDecodeError:
                    return body.decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                print(f"[sgu-se-en] curl error: {exc}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[sgu-se-en] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _parse_date(date_str):
    """Parse '27 March 2026' or 'YYYY-MM-DD' to ISO date string."""
    if not date_str:
        return ""
    date_str = date_str.strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
    if m:
        return m.group(1)
    try:
        return datetime.strptime(date_str, "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    # Try without leading zero
    try:
        return datetime.strptime(date_str, "%-d %B %Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    return date_str


def _decode_entities(text):
    """Decode common HTML entities in a string."""
    for ent, char in _HTML_ENTITIES.items():
        text = text.replace(ent, char)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    return text


def _soup_text(tag):
    """Get clean text from a BS4 tag."""
    if tag is None:
        return ""
    return re.sub(r"\s+", " ", tag.get_text(separator=" ", strip=True))


class SguSeEnCrawler(BaseCrawler):
    """Crawler for SGU (Geological Survey of Sweden) English news."""

    site_id = "sgu-se-en"
    site_name = "Custom: sgu-se-en"
    base_url = "https://www.sgu.se"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "inf"
        start_time = time.time()
        MAX_MINUTES = 25
        MAX_PAGES = 200

        page = 1
        while page <= MAX_PAGES:
            # Time budget
            if (time.time() - start_time) / 60 >= MAX_MINUTES:
                print(f"[sgu-se-en] Time budget ({MAX_MINUTES}m) reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[sgu-se-en] page {page}: saved {saved}/{limit_str}")

            list_url = _LIST_URL if page == 1 else f"{_LIST_URL}?page={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[sgu-se-en] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[sgu-se-en] Parse error on list page {page}: {exc}")
                page += 1
                continue

            if soup is None:
                print(f"[sgu-se-en] Could not parse list page {page}. Stopping.")
                break

            listing = soup.find("div", class_="news-listing")
            if not listing:
                print(f"[sgu-se-en] No news-listing div on page {page}. Stopping.")
                break

            items = listing.find_all("li", class_=re.compile(r"listResult"))
            if not items:
                print(f"[sgu-se-en] No items on page {page}. Done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    h2 = item.find("h2")
                    if not h2:
                        continue
                    a_tag = h2.find("a")
                    if not a_tag:
                        continue

                    relative_url = a_tag.get("href", "")
                    article_url = (
                        _BASE_URL + relative_url
                        if relative_url.startswith("/")
                        else relative_url
                    )
                    list_title = _soup_text(a_tag)

                    if article_url in seen_urls:
                        continue
                    seen_urls.add(article_url)
                    new_on_page += 1

                    # Date from list page
                    date_p = item.find("p", class_="date")
                    list_date_raw = _soup_text(date_p) if date_p else ""
                    listed_date = _parse_date(list_date_raw)

                    # Short abstract snippet from list page (fallback)
                    list_abstract = ""
                    for p in item.find_all("p"):
                        cls = p.get("class") or []
                        if "date" not in cls:
                            list_abstract = _soup_text(p)
                            if list_abstract:
                                break

                    # Fetch detail page
                    time.sleep(self._delay)
                    detail_html = None
                    for attempt in range(3):
                        detail_html = _curl_get(article_url)
                        if detail_html:
                            break
                        wait = [1, 3, 9][attempt]
                        print(
                            f"[sgu-se-en] detail fetch failed for {article_url} "
                            f"(attempt {attempt + 1}/3), retrying in {wait}s"
                        )
                        time.sleep(wait)

                    if not detail_html:
                        print(f"[sgu-se-en] Skipping {article_url}: could not fetch detail")
                        continue

                    try:
                        dsoup = _make_soup(detail_html)
                    except Exception as exc:
                        print(f"[sgu-se-en] Parse error for detail {article_url}: {exc}")
                        dsoup = None

                    if dsoup is None:
                        # Fall back to list data
                        abstract = list_abstract
                        pageid = ""
                        title = list_title
                        published_date = listed_date
                        pdf_url = None
                        original_filename = None
                    else:
                        # Numeric page ID (CMS internal ID)
                        pageid_meta = dsoup.find("meta", attrs={"name": "pageid"})
                        pageid = (
                            pageid_meta.get("content", "").strip()
                            if pageid_meta else ""
                        )

                        # Title
                        h1 = dsoup.find("h1")
                        title = _soup_text(h1) if h1 else list_title
                        if not title:
                            title = list_title

                        # Date from detail
                        date_span = dsoup.find("span", class_="date")
                        detail_date_raw = _soup_text(date_span) if date_span else ""
                        published_date = _parse_date(detail_date_raw) or listed_date

                        # Fallback: "Last reviewed YYYY-MM-DD"
                        if not published_date:
                            changed = dsoup.find("div", class_="changed-stamp")
                            if changed:
                                m = re.search(r"(\d{4}-\d{2}-\d{2})", _soup_text(changed))
                                if m:
                                    published_date = m.group(1)

                        # Abstract: introduction paragraph + main-content body
                        intro = dsoup.find("p", class_="introduction")
                        intro_text = _soup_text(intro) if intro else ""

                        main_div = dsoup.find("div", class_="main-content")
                        body_text = ""
                        if main_div:
                            for tag in main_div(["script", "style"]):
                                tag.decompose()
                            body_text = _soup_text(main_div)

                        parts = []
                        if intro_text:
                            parts.append(intro_text)
                        if body_text and body_text != intro_text:
                            parts.append(body_text)
                        abstract = "\n\n".join(parts)

                        if not abstract.strip():
                            abstract = list_abstract

                        # PDF URL from main-content links
                        pdf_url = None
                        original_filename = None
                        if main_div:
                            for link in main_div.find_all("a", href=True):
                                href = link["href"]
                                if href.lower().endswith(".pdf"):
                                    pdf_url = href
                                    fname = href.rstrip("/").split("/")[-1].split("?")[0]
                                    original_filename = fname if fname else None
                                    break

                    # Skip items with very short abstracts
                    if len(abstract.strip()) < 50:
                        print(
                            f"[sgu-se-en] Skipping {article_url}: "
                            f"abstract too short ({len(abstract.strip())} chars)"
                        )
                        continue

                    # Slug for external_id fallback
                    slug = relative_url.rstrip("/").rsplit("/", 1)[-1]
                    external_id = pageid if pageid else slug
                    post_number = pageid if pageid else None

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": article_url,
                        "pdf_url": pdf_url or "",
                        "original_filename": original_filename,
                        "authors": "",
                        "publisher": "Geological Survey of Sweden (SGU)",
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": "News",
                        "doi": "",
                        "metadata": json.dumps(
                            {
                                "posted_date": list_date_raw,
                                "pageid": pageid,
                                "slug": slug,
                                "originalFilename": original_filename,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[sgu-se-en] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[sgu-se-en] item failed: {exc}")
                    continue

            # All items on this page were already seen → pagination loop
            if new_on_page == 0:
                print(f"[sgu-se-en] page {page}: all items already seen. Done.")
                break

            if page == MAX_PAGES:
                print(f"[sgu-se-en] Safety cap of {MAX_PAGES} pages reached. Stopping.")

            page += 1

        print(f"[sgu-se-en] Done. Total saved: {saved}")
        return saved
