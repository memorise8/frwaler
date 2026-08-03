# -*- coding: utf-8 -*-
"""Crawler for kammarkollegiet.se/aktuellt (Kammarkollegiet news).

Discovery strategy:
  The /aktuellt page embeds a JSON blob via AppRegistry.registerInitialState(...)
  that contains ALL news items (typically ~46) in the ``news`` array.  No REST
  API or server-side pagination is required – a single page fetch is enough to
  discover every URL.  For each discovered item the detail page is fetched to
  extract the full body text as the abstract.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "kammarkollegiet-se-aktuellt"
_BASE_URL = "https://www.kammarkollegiet.se"
_LIST_URL = f"{_BASE_URL}/aktuellt"
_PAGE_CAP = 200      # safety cap: stop after this many pages (not used here — single-page list)
_DETAIL_DELAY = 1.0  # seconds between detail fetches

_SV_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "maj": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "okt": "10", "nov": "11", "dec": "12",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_fetch(url: str, retries: int = 3) -> bytes | None:
    """Fetch *url* via curl (TLS-flexible, insecure, silent). Returns raw bytes or None."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk",
                    "-A", BaseCrawler.USER_AGENT,
                    "-L", "--max-time", "30",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            print(f"[{_SITE_ID}] curl rc={result.returncode} attempt {attempt+1}/{retries} for {url}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt+1}/{retries} for {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


# ---------------------------------------------------------------------------
# HTML / JSON parsing
# ---------------------------------------------------------------------------

def _make_soup(raw: bytes):
    """Parse *raw* HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            content = raw.decode("utf-8", errors="replace")
            return BeautifulSoup(content, parser)
        except Exception:
            continue
    return None


def _extract_list_json(raw: bytes) -> list:
    """Extract the embedded news array from the /aktuellt page."""
    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception:
        content = raw.decode("latin-1", errors="replace")

    idx = content.find('"news":[{')
    if idx < 0:
        return []
    start = content.rfind("{", 0, idx)
    if start < 0:
        return []
    blob = content[start:]
    try:
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(blob)
        return data.get("news", [])
    except Exception as exc:
        print(f"[{_SITE_ID}] JSON parse error: {exc}")
        return []


def _extract_body(raw: bytes) -> str:
    """Extract full article body text from a detail page."""
    soup = None
    try:
        soup = _make_soup(raw)
    except Exception:
        pass

    if soup is None:
        # Plain tag-strip fallback
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()[:3000]

    paragraphs = []
    for div in soup.find_all("div", class_=lambda c: c and "sv-text-portlet" in c):
        # Skip the h1 title portlet
        if div.find("h1"):
            continue
        text = div.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        # Skip very short or contact-footer blobs
        if len(text) < 20:
            continue
        if text.lower().startswith("kontakta oss") or text.lower().startswith("e-post"):
            continue
        paragraphs.append(text)

    return " ".join(paragraphs)


def _parse_date(published_long, published_str: str) -> str | None:
    """Return ISO date string from epoch-ms or Swedish date string."""
    if published_long:
        try:
            dt = datetime.fromtimestamp(int(published_long) / 1000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    if published_str:
        m = re.match(r"(\d+)\s+(\w+)\s+(\d{4})", published_str.lower().strip())
        if m:
            day, mon_str, year = m.groups()
            month = _SV_MONTHS.get(mon_str[:3])
            if month:
                return f"{year}-{month}-{int(day):02d}"
    return None


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class KammarkollegietSeAktuellt(BaseCrawler):
    """News crawler for kammarkollegiet.se/aktuellt."""

    site_id   = _SITE_ID
    site_name = "Custom: kammarkollegiet-se-aktuellt"
    base_url  = _BASE_URL

    def crawl(self, limit=None):  # noqa: C901
        saved = 0
        start_wall = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget
        seen_urls: set[str] = set()
        limit_display = limit if limit is not None else "∞"

        print(f"[{_SITE_ID}] Starting crawl, limit={limit_display}")

        # ── Step 1: fetch list page ──────────────────────────────────────
        raw = _curl_fetch(_LIST_URL)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch list page, aborting")
            return 0

        all_items = _extract_list_json(raw)
        if not all_items:
            print(f"[{_SITE_ID}] No items found in embedded JSON, aborting")
            return 0

        print(f"[{_SITE_ID}] Found {len(all_items)} items in embedded JSON")

        # ── Step 2: fetch detail pages and save ─────────────────────────
        for i, item in enumerate(all_items):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_wall > max_wall:
                print(f"[{_SITE_ID}] Wall-clock budget (25 min) reached, stopping cleanly")
                break

            if i > 0 and i % 10 == 0:
                print(f"[{_SITE_ID}] item {i}: saved {saved}/{limit_display}")

            try:
                uri = (item.get("uri") or "").strip()
                if not uri:
                    continue

                url = f"{_BASE_URL}{uri}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title = (item.get("title") or "").strip()
                if not title:
                    continue

                ingress = (item.get("ingress") or "").strip()
                published_date = _parse_date(
                    item.get("publishedLong"), item.get("published")
                )
                category = (item.get("category") or item.get("type") or "").strip()
                # external_id: use URI slug (unique and stable)
                external_id = uri.strip("/").replace("/", "_")

                # ── Fetch detail page ────────────────────────────────────
                time.sleep(_DETAIL_DELAY)
                detail_raw = _curl_fetch(url)

                if detail_raw:
                    body = _extract_body(detail_raw)
                else:
                    body = ""

                # Build abstract: prefer full body; fall back to ingress
                abstract = body.strip() if len(body.strip()) >= len(ingress) else ingress
                abstract = abstract.strip()

                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] Skipping short abstract ({len(abstract)} chars): {title[:60]}")
                    continue

                paper = {
                    "site_id":        _SITE_ID,
                    "external_id":    external_id,
                    "title":          title,
                    "authors":        json.dumps([]),
                    "abstract":       abstract,
                    "category":       category,
                    "keywords":       json.dumps([]),
                    "published_date": published_date,
                    "url":            url,
                    "pdf_url":        None,
                    "doi":            None,
                    "department":     "Kammarkollegiet",
                    "metadata":       json.dumps({
                        "type":  item.get("type"),
                        "year":  item.get("year"),
                        "month": item.get("month"),
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {i} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Saved {saved}/{limit_display} items.")
        return saved
