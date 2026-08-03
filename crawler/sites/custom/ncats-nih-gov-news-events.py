# -*- coding: utf-8 -*-
"""NCATS NIH News & Events crawler.

Starting URL: https://ncats.nih.gov/news-events/news
Mechanism: Drupal Views AJAX endpoint for pagination; external (and internal)
           article pages are fetched to extract abstracts from meta tags or
           body text.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_SITE_ID = "ncats-nih-gov-news-events"
_VIEWS_AJAX_URL = "https://ncats.nih.gov/views/ajax"
_NEWS_PAGE_URL = "https://ncats.nih.gov/news-events/news"
_DEFAULT_VIEW_DOM_ID = (
    "7c55cb4f870013aeff3985ad3e0b28ac23f9f3325cfd7dc5613f5505b5491978"
)
_VIEW_NAME = "news_new"
_VIEW_DISPLAY_ID = "block_7"
_VIEW_NODE_PATH = "%2Fnode%2F27561"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_RATE_LIMIT = 1.0        # seconds between detail-page fetches
_WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock cap


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _curl(url, method="GET", post_data=None, max_retries=3):
    """Fetch URL via curl with exponential-backoff retries. Returns text or None."""
    cmd = [
        "curl", "-sk", "--max-time", "30",
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    ]
    if method == "POST" and post_data:
        cmd += [
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", "X-Requested-With: XMLHttpRequest",
            "--data", post_data,
        ]
    cmd.append(url)

    delays = [1, 3, 9]
    for attempt in range(max_retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            if attempt == max_retries - 1:
                print(f"[{_SITE_ID}] curl failed ({url[:70]}): {exc}")
        if attempt < max_retries - 1:
            time.sleep(delays[attempt])
    return None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _try_bs4(html):
    """Parse HTML trying html5lib → lxml → html.parser. Returns soup or None."""
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


def _extract_abstract(html):
    """Extract a usable abstract string from an article page.

    Priority: meta description → og:description → first long <p> in body.
    Returns empty string when nothing useful is found.
    """
    if not html:
        return ""

    candidates = []

    # meta name="description" (handle both attribute orderings)
    for pat in (
        r'<meta[^>]+name="description"[^>]+content="([^"]{40,})"',
        r'<meta[^>]+content="([^"]{40,})"[^>]+name="description"',
    ):
        m = re.search(pat, html, re.I)
        if m:
            candidates.append(m.group(1).strip())
            break

    # og:description
    for pat in (
        r'<meta[^>]+property="og:description"[^>]+content="([^"]{40,})"',
        r'<meta[^>]+content="([^"]{40,})"[^>]*property="og:description"',
    ):
        m = re.search(pat, html, re.I)
        if m:
            cand = m.group(1).strip()
            if cand not in candidates:
                candidates.append(cand)
            break

    # Return longest meta candidate that meets the 100-char minimum
    for cand in sorted(candidates, key=len, reverse=True):
        if len(cand) >= 100:
            return cand

    # Fall back to first substantial paragraph via BeautifulSoup
    try:
        soup = _try_bs4(html)
        if soup:
            for tag in soup.find_all(["nav", "header", "footer", "script", "style", "aside"]):
                tag.decompose()
            for p in soup.find_all("p"):
                text = p.get_text(separator=" ", strip=True)
                if len(text) >= 100:
                    return text
    except Exception:
        pass

    # Last resort: combine whatever meta text we have
    return " ".join(c for c in candidates if c)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_date(date_str):
    """'May 4, 2026' → '2026-05-04'. Returns original string on failure."""
    s = re.sub(r"\s+", " ", (date_str or "").strip())
    for fmt in ("%B %d, %Y", "%B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s or None


def _url_to_external_id(url):
    """Derive a stable, unique external_id from an article URL."""
    slug = url.rstrip("/").split("/")[-1] or url.rstrip("/").split("/")[-2]
    clean = re.sub(r"[^\w\-]", "_", slug)[:60]
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{h}_{clean}" if clean else h


def _get_view_dom_id():
    """Fetch the live Drupal view dom_id; fall back to hardcoded default."""
    raw = _curl(_NEWS_PAGE_URL)
    if raw:
        m = re.search(r"views_dom_id:([a-f0-9]{40,})", raw)
        if m:
            return m.group(1)
    return _DEFAULT_VIEW_DOM_ID


# ---------------------------------------------------------------------------
# Listing-page fetch & parse
# ---------------------------------------------------------------------------

def _fetch_page(page_num, dom_id):
    """Fetch one AJAX listing page. Returns (articles: list[dict], has_next: bool)."""
    post_data = (
        f"view_name={_VIEW_NAME}&view_display_id={_VIEW_DISPLAY_ID}&view_args=&"
        f"view_path={_VIEW_NODE_PATH}&view_base_path=null&"
        f"view_dom_id={dom_id}&pager_element=0&page={page_num}"
    )
    raw = _curl(_VIEWS_AJAX_URL, method="POST", post_data=post_data)
    if not raw:
        return [], False

    try:
        commands = json.loads(raw)
    except Exception:
        return [], False

    html = ""
    for cmd in commands:
        if cmd.get("command") == "insert" and cmd.get("method") == "replaceWith":
            html = cmd.get("data", "")
            break
    if not html:
        return [], False

    try:
        soup = _try_bs4(html)
    except Exception:
        return [], False
    if not soup:
        return [], False

    articles = []
    seen_in_page = set()

    for row in soup.find_all("div", class_="views-row"):
        try:
            title_el = row.find("h3", class_="title")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            date_str = ""
            article_type = ""
            date_el = row.find("p", class_="m-0")
            if date_el:
                parts = date_el.get_text(strip=True).split(" - ", 1)
                if len(parts) == 2:
                    date_str, article_type = parts[0].strip(), parts[1].strip()
                else:
                    date_str = parts[0].strip()

            programs = [li.get_text(strip=True) for li in row.find_all("li")]

            # First non-pagination link is the article URL
            url = ""
            for a in row.find_all("a", href=True):
                href = a["href"].strip()
                if href and not href.startswith("?") and not href.startswith("#"):
                    url = href
                    break

            if not url or url in seen_in_page:
                continue
            seen_in_page.add(url)

            articles.append({
                "title": title,
                "date_str": date_str,
                "article_type": article_type,
                "programs": programs,
                "url": url,
            })
        except Exception:
            continue

    has_next = bool(
        re.search(r'rel="next"', html)
        or re.search(r'href="\?page=\d+"', html)
    )
    return articles, has_next


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class NCATSNIHNewsEventsCrawler(BaseCrawler):
    """Crawler for NCATS NIH News & Events (https://ncats.nih.gov/news-events/news)."""

    site_id = _SITE_ID
    site_name = "Custom: ncats-nih-gov-news-events"
    base_url = "https://ncats.nih.gov"

    def crawl(self, limit=None):
        """Crawl NCATS news listing. Returns total number of saved records."""
        seen_urls = set()
        saved = 0
        start_time = time.time()
        limit_disp = limit if limit is not None else "∞"

        dom_id = _get_view_dom_id()

        for page_num in range(_MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > _WALL_BUDGET:
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page_num}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_disp}")

            try:
                articles, has_next = _fetch_page(page_num, dom_id)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] page {page_num} fetch error: {exc}")
                break

            if not articles:
                print(f"[{_SITE_ID}] page {page_num}: 0 articles returned, stopping.")
                break

            new_this_page = 0
            for art in articles:
                if limit is not None and saved >= limit:
                    break

                url = art["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_this_page += 1

                try:
                    time.sleep(_RATE_LIMIT)
                    detail_html = _curl(url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] item '{art['title'][:50]}' failed: no HTTP response")
                        continue

                    abstract = _extract_abstract(detail_html)

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] item '{art['title'][:50]}' skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": _url_to_external_id(url),
                        "title": art["title"],
                        "authors": json.dumps([]),
                        "abstract": abstract,
                        "category": art["article_type"],
                        "keywords": json.dumps(art["programs"]),
                        "published_date": _parse_date(art["date_str"]),
                        "url": url,
                        "pdf_url": None,
                        "doi": None,
                        "department": None,
                        "metadata": json.dumps({
                            "news_type": art["article_type"],
                            "programs": art["programs"],
                        }),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item '{art.get('title', '')[:50]}' failed: {exc}")
                    continue

            if new_this_page == 0:
                print(f"[{_SITE_ID}] page {page_num}: all URLs already seen, stopping.")
                break

            if not has_next:
                print(f"[{_SITE_ID}] page {page_num}: no next-page link, done.")
                break

            if page_num == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached, stopping.")

        print(f"[{_SITE_ID}] Crawl complete. Total saved: {saved}")
        return saved
