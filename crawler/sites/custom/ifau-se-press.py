# -*- coding: utf-8 -*-
"""IFAU Pressmeddelanden crawler.

Starting URL: https://www.ifau.se/Press/Pressmeddelanden/
Pagination:   ?page=N  (8 articles/page; ~27 pages)
Detail pages: plain HTML — h1 title, preamble div, Publicerades date, Författare spans,
              article-content body divs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from html import unescape

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.ifau.se"
_LIST_URL = _BASE_URL + "/Press/Pressmeddelanden/"
_MAX_PAGES = 200
_WALL_SECONDS = 25 * 60  # 25 minutes
_MIN_ABSTRACT = 50       # skip items with abstract shorter than this

_MONTHS_SE = {
    "januari": "01", "februari": "02", "mars": "03", "april": "04",
    "maj": "05", "juni": "06", "juli": "07", "augusti": "08",
    "september": "09", "oktober": "10", "november": "11", "december": "12",
}

# Pattern for article links (excludes the listing page itself and pagination links)
_ARTICLE_HREF_RE = re.compile(r'^/Press/Pressmeddelanden/[^/?#]+/$')


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl(url: str, retries: int = 3) -> str:
    """GET *url* via curl with retries and exponential backoff.

    Returns decoded text (UTF-8 with replace) or '' on total failure.
    """
    backoff = [1, 3, 9]
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(backoff[attempt - 1])
        try:
            cmd = [
                "curl", "--tls-max", "1.3", "-skL",
                "--max-time", "30",
                "--user-agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                "-H", "Accept-Language: sv-SE,sv;q=0.9,en;q=0.8",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[ifau-se-press] curl exit {result.returncode} for {url}")
        except subprocess.TimeoutExpired:
            print(f"[ifau-se-press] curl timeout (attempt {attempt + 1}/{retries}) for {url}")
        except Exception as exc:
            print(f"[ifau-se-press] curl error (attempt {attempt + 1}/{retries}): {exc}")
    return ""


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """BeautifulSoup with fallback chain: html5lib → lxml → html.parser."""
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


def _parse_date_se(raw: str) -> str:
    """Convert Swedish date '24 mars 2026' → '2026-03-24', or '' on failure."""
    cleaned = re.sub(r"\s+", " ", raw).strip().lower()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", cleaned)
    if not m:
        return ""
    day, month_name, year = m.group(1), m.group(2), m.group(3)
    month = _MONTHS_SE.get(month_name)
    if not month:
        return ""
    return f"{year}-{month}-{day.zfill(2)}"


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# Listing + detail parsers
# ---------------------------------------------------------------------------

def _parse_listing(html: str) -> list[str]:
    """Extract article relative paths from a Meddelanden listing page."""
    soup = _make_soup(html)
    if soup is None:
        # Regex fallback
        return list(dict.fromkeys(re.findall(
            r'href="(/Press/Pressmeddelanden/[^"?#/][^"?#]*/)"', html
        )))
    paths: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _ARTICLE_HREF_RE.match(href):
            paths.append(href)
    return list(dict.fromkeys(paths))  # deduplicate, preserve order


def _parse_article(html: str, path: str) -> dict | None:
    """Parse an article detail page into a paper dict. Returns None on hard failure."""
    soup = _make_soup(html)
    if soup is None:
        print(f"[ifau-se-press] could not parse HTML for {path}")
        return None

    # --- Title ---
    h1 = soup.find("h1")
    title = _clean_text(h1.get_text()) if h1 else ""
    if not title:
        return None

    # --- Preamble (lead paragraph) ---
    preamble = ""
    preamble_div = soup.find("div", class_="preamble")
    if preamble_div:
        preamble = _clean_text(preamble_div.get_text(separator=" "))

    # --- Publication date ---
    pub_date = ""
    for p_tag in soup.find_all("p"):
        txt = p_tag.get_text(separator=" ")
        if "Publicerades" in txt:
            date_raw = re.sub(r"Publicerades\s*:", "", txt, flags=re.IGNORECASE)
            pub_date = _parse_date_se(date_raw)
            break

    # --- Authors ---
    authors: list[str] = []
    for span in soup.find_all("span"):
        span_text = span.get_text(strip=True)
        if "rfattare" not in span_text:  # "Författare"
            continue
        parent = span.find_parent()
        if not parent:
            continue
        for author_span in parent.find_all("span"):
            classes = author_span.get("class") or []
            # We want visible "d-inline-block" spans that are NOT "d-none"
            if "d-inline-block" in classes and "d-none" not in classes:
                name = _clean_text(author_span.get_text())
                if name and name.lower() not in ("och", "and"):
                    authors.append(name)
        break

    # --- Body paragraphs from article-content divs ---
    body_parts: list[str] = []
    for div in soup.find_all("div", class_="article-content"):
        txt = _clean_text(div.get_text(separator=" "))
        if txt and txt not in body_parts:
            body_parts.append(txt)

    # Assemble abstract: preamble first, then body
    abstract_parts = []
    if preamble:
        abstract_parts.append(preamble)
    for bp in body_parts:
        if bp and bp not in abstract_parts:
            abstract_parts.append(bp)
    abstract = "\n\n".join(abstract_parts).strip()

    # --- Keywords (research area tags) ---
    keywords: list[str] = []
    for a_tag in soup.find_all("a", class_="tag"):
        kw = _clean_text(a_tag.get_text())
        if kw:
            keywords.append(kw)

    slug = path.rstrip("/").rsplit("/", 1)[-1]

    return {
        "external_id": slug,
        "title": title,
        "authors": json.dumps(authors, ensure_ascii=False),
        "abstract": abstract,
        "published_date": pub_date,
        "keywords": json.dumps(keywords, ensure_ascii=False),
        "url": _BASE_URL + path,
        "pdf_url": "",
        "doi": "",
        "category": "",
        "department": "",
        "metadata": json.dumps({"slug": slug}, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class IFAUPressCrawler(BaseCrawler):
    """Crawler for IFAU Press Meddelanden press releases."""

    site_id   = "ifau-se-press"
    site_name = "Custom: ifau-se-press"
    base_url  = "https://www.ifau.se"

    def crawl(self, limit=None):
        """Crawl IFAU Pressmeddelanden.

        Parameters
        ----------
        limit:
            Maximum number of records to save. None = unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        for page_num in range(1, _MAX_PAGES + 1):
            # --- Wall-clock budget ---
            if time.time() - start_time > _WALL_SECONDS:
                print(f"[ifau-se-press] 25-minute budget reached. Stopping.")
                break

            # --- Limit check ---
            if limit is not None and saved >= limit:
                break

            # --- Safety-cap log ---
            if page_num == _MAX_PAGES:
                print(f"[ifau-se-press] Safety cap of {_MAX_PAGES} pages reached. Stopping.")

            # --- Progress log every 10 pages ---
            if page_num % 10 == 0:
                print(f"[ifau-se-press] page {page_num}: saved {saved}/{limit_str}")

            # --- Fetch listing page ---
            list_url = _LIST_URL if page_num == 1 else f"{_LIST_URL}?page={page_num}"
            html = _curl(list_url)
            if not html:
                print(f"[ifau-se-press] Failed to fetch listing page {page_num}. Stopping.")
                break

            paths = _parse_listing(html)
            new_paths = [p for p in paths if p not in seen_urls]

            if not new_paths:
                print(f"[ifau-se-press] Page {page_num}: no new articles. Done.")
                break

            for path in new_paths:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(path)
                article_url = _BASE_URL + path

                try:
                    time.sleep(self._delay)

                    detail_html = _curl(article_url)
                    if not detail_html:
                        print(f"[ifau-se-press] item {path} failed after retries. Skipping.")
                        continue

                    paper = _parse_article(detail_html, path)
                    if paper is None:
                        print(f"[ifau-se-press] item {path} could not be parsed. Skipping.")
                        continue

                    abstract = paper.get("abstract", "")
                    if len(abstract) < _MIN_ABSTRACT:
                        print(f"[ifau-se-press] item {path} abstract too short "
                              f"({len(abstract)} chars). Skipping.")
                        continue

                    paper["site_id"] = self.site_id
                    paper["id"] = None
                    self._save_paper(paper)
                    saved += 1
                    print(f"[ifau-se-press] saved {saved}/{limit_str}: {paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ifau-se-press] item {path} failed: {exc}")
                    continue

        print(f"[ifau-se-press] Done. Total saved: {saved}")
        return saved
