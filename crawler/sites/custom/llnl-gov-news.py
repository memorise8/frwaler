# -*- coding: utf-8 -*-
"""LLNL News crawler — Lawrence Livermore National Laboratory news articles.

Starting URL : https://www.llnl.gov/news
List API     : https://contenthub.llnl.gov/articles/all/{page_size}/{offset}/none/www.llnl.gov/0
               Returns JSONP: /**/eventsLoaded([{nid, title, body_value, pub_date, alias_path, ...}])
Pagination   : offset-based (step = _PAGE_SIZE).  Total ≈ 5 200 articles.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_API_BASE  = "https://contenthub.llnl.gov"
_DOMAIN    = "www.llnl.gov"
_WWW_BASE  = "https://www.llnl.gov"
_PAGE_SIZE = 20          # articles per API call
_JSONP_RE  = re.compile(r'(?:/\*\*/)?\s*eventsLoaded\((.*)\)\s*;?\s*$', re.DOTALL)


# ---------------------------------------------------------------------------
# HTTP helper (curl-based for TLS compatibility)
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch *url* via curl with exponential-backoff retry. Returns raw text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", ("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "-H", "Accept: application/javascript, */*;q=0.8",
        "-H", "Referer: https://www.llnl.gov/news",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[llnl-gov-news] empty response, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[llnl-gov-news] curl error: {exc}, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[llnl-gov-news] curl failed after {retries} attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    """Remove HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, repl in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                      ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(ent, repl)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> str | None:
    """'May 12, 2026' → '2026-05-12'. Returns None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_keywords(terms: str | None) -> str | None:
    """'761:Bioscience,496:Biotechnology' → 'Bioscience, Biotechnology'."""
    if not terms:
        return None
    names = []
    seen: set[str] = set()
    for part in terms.split(","):
        part = part.strip()
        name = part.split(":", 1)[1].strip() if ":" in part else part
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return ", ".join(names) if names else None


def _fetch_page(offset: int, page_size: int) -> list | None:
    """Call the contenthub JSONP API; return a list of article dicts (may be empty)."""
    url = (f"{_API_BASE}/articles/all/{page_size}/{offset}"
           f"/none/{_DOMAIN}/0?callback=eventsLoaded")
    raw = _curl_get(url)
    if not raw:
        return None
    m = _JSONP_RE.match(raw.strip())
    if not m:
        print(f"[llnl-gov-news] unexpected response at offset={offset}: {raw[:120]}")
        return None
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[llnl-gov-news] JSON parse error at offset={offset}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class LLNLGovNewsCrawler(BaseCrawler):
    """Crawler for LLNL News (Lawrence Livermore National Laboratory)."""

    site_id   = "llnl-gov-news"
    site_name = "Custom: llnl-gov-news"
    base_url  = "https://www.llnl.gov"

    def crawl(self, limit=None):
        saved        = 0
        seen_urls: set[str] = set()
        start_time   = time.time()
        max_wall     = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))    # 25-minute wall-clock budget
        max_pages    = 200        # safety cap
        page_num     = 0

        limit_or_inf = limit if limit is not None else float("inf")

        while saved < limit_or_inf:
            if time.time() - start_time > max_wall:
                print(f"[llnl-gov-news] 25-minute budget reached, stopping at saved={saved}")
                break
            if page_num >= max_pages:
                print(f"[llnl-gov-news] Safety cap of {max_pages} pages reached, stopping")
                break

            offset     = page_num * _PAGE_SIZE
            remaining  = int(limit_or_inf - saved) if limit is not None else _PAGE_SIZE
            fetch_size = min(_PAGE_SIZE, remaining) if limit is not None else _PAGE_SIZE

            if page_num % 10 == 0:
                print(f"[llnl-gov-news] page {page_num}: saved {saved}/{limit if limit else 'inf'}")

            items = _fetch_page(offset, fetch_size)
            if items is None:
                print(f"[llnl-gov-news] fetch failed at page {page_num}, stopping")
                break
            if not items:
                print(f"[llnl-gov-news] empty page at offset={offset}, pagination done")
                break

            new_this_page = 0

            for item in items:
                if saved >= limit_or_inf:
                    break

                try:
                    nid = str(item.get("nid") or "").strip()
                    if not nid:
                        continue

                    title = (item.get("title") or "").strip()
                    if not title:
                        continue

                    # Build canonical detail URL
                    source     = item.get("source", "pao")
                    alias_path = (item.get("alias_path") or "").strip()
                    ext_link   = (item.get("ext_link") or "").strip()

                    if source == "external" and ext_link:
                        detail_url = ext_link
                    elif alias_path:
                        detail_url = f"{_WWW_BASE}{alias_path}"
                    else:
                        detail_url = f"{_WWW_BASE}/news"

                    # URL-level deduplication
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    # Abstract — body_value is a ~300-char teaser from the API
                    body_raw = item.get("body_value") or item.get("body_mini_value") or ""
                    abstract = _strip_tags(body_raw).strip()

                    if len(abstract) < 50:
                        print(f"[llnl-gov-news] nid={nid} abstract too short "
                              f"({len(abstract)} chars), skipping")
                        continue

                    # Dates
                    pub_date_raw   = (item.get("pub_date") or "").strip()
                    published_date = _parse_date(pub_date_raw)

                    # Keywords extracted from 'terms': "761:Bioscience,496:Biotech,..."
                    keywords = _extract_keywords(item.get("terms"))
                    category = (item.get("name") or "").strip() or None

                    # Metadata: preserve all raw API fields not mapped to top-level columns
                    meta = {
                        "nid":          nid,
                        "tid":          item.get("tid"),
                        "vid":          item.get("vid"),
                        "terms":        item.get("terms"),
                        "alias":        item.get("alias"),
                        "source":       source,
                        "ext_link":     ext_link or None,
                        "pub_date_raw": pub_date_raw,
                        "large_image":  item.get("large_image"),
                        "posted_date":  published_date,
                    }

                    self._save_paper({
                        "site_id":           self.site_id,
                        "external_id":       nid,
                        "post_number":       nid,
                        "url":               detail_url,  # → meta_url via libertree_adapter
                        "title":             title,
                        "abstract":          abstract,
                        "published_date":    published_date,
                        "posted_date":       published_date,
                        "listed_date":       published_date,
                        "publisher":         "Lawrence Livermore National Laboratory",
                        "keywords":          keywords,
                        "category":          category,
                        "pdf_url":           None,
                        "original_filename": None,
                        "metadata":          json.dumps(meta, ensure_ascii=False),
                    })

                    saved         += 1
                    new_this_page += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    nid_str = item.get("nid", "?") if isinstance(item, dict) else "?"
                    print(f"[llnl-gov-news] item nid={nid_str} failed: {exc}")
                    continue

            if new_this_page == 0:
                print(f"[llnl-gov-news] no new items on page {page_num}, stopping")
                break

            page_num += 1
            # Rate-limit between page fetches (no per-item detail-page fetch)
            if saved < limit_or_inf:
                time.sleep(1.0)

        print(f"[llnl-gov-news] crawl complete: saved={saved}")
        return saved
