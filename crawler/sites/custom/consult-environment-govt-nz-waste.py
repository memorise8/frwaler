# -*- coding: utf-8 -*-
"""Crawler for NZ Ministry for the Environment public consultations.

Target: https://consult.environment.govt.nz/consultation_finder/
~55 consultations, 30/page, HTML-only (no API).
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_SITE_ID = "consult-environment-govt-nz-waste"
_BASE_URL = "https://consult.environment.govt.nz"
_FINDER_URL = "https://consult.environment.govt.nz/consultation_finder/"
_PAGE_SIZE = 30
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_RATE_SLEEP = 1.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ",
    "&quot;": '"', "&apos;": "'", "&#160;": " ", "&#8211;": "–",
    "&#8212;": "—", "&#8220;": "“", "&#8221;": "”",
    "&#8216;": "‘", "&#8217;": "’", "&#8230;": "...",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS 1.3 compat; returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-NZ,en;q=0.9",
        url,
    ]
    wait_secs = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace").strip()
            if text:
                return text
            if attempt < retries - 1:
                w = wait_secs[attempt]
                print(f"[{_SITE_ID}] Empty response, retrying in {w}s ({url})")
                time.sleep(w)
        except Exception as exc:
            w = wait_secs[min(attempt, len(wait_secs) - 1)]
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/{retries}: {exc}")
            if attempt < retries - 1:
                time.sleep(w)
    return None


def _strip_tags(html_fragment: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    for ent, ch in _HTML_ENTITIES.items():
        text = text.replace(ent, ch)
    # decode numeric entities like &#8203;
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_date(text: str) -> str | None:
    """Parse 'Closed 1 Jun 2025', 'Opened 31 Mar 2025', '26 November 2025' → YYYY-MM-DD."""
    text = _strip_tags(text).strip()
    # Remove state prefix words
    text = re.sub(
        r"^(Closed|Opened|Open Until|Forthcoming|Opens|Open)\s+",
        "", text, flags=re.IGNORECASE
    ).strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Fallback: extract day month year from text
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(
                    f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt
                ).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def _extract_text_block(html: str, div_id: str) -> str:
    """Extract text from <div id="div_id">...</div> (shallow, regex-based)."""
    idx = html.find(f'id="{div_id}"')
    if idx < 0:
        return ""
    # find the opening > of the div tag
    tag_end = html.find(">", idx)
    if tag_end < 0:
        return ""
    content_start = tag_end + 1
    # Walk forward tracking nested div depth
    depth = 1
    pos = content_start
    while pos < len(html) and depth > 0:
        open_m = html.find("<div", pos)
        close_m = html.find("</div>", pos)
        if close_m < 0:
            break
        if open_m >= 0 and open_m < close_m:
            depth += 1
            pos = open_m + 4
        else:
            depth -= 1
            if depth == 0:
                block = html[content_start:close_m]
                return _strip_tags(block).strip()
            pos = close_m + 6
    return ""


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class ConsultEnvironmentGovtNzWasteCrawler(BaseCrawler):
    """Crawls NZ Ministry for the Environment public consultations."""

    site_id = _SITE_ID
    site_name = "Custom: consult-environment-govt-nz-waste"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_val = limit if limit is not None else float("inf")

        page = 1
        while saved < limit_val:
            # 25-minute wall-clock budget
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute budget reached, stopping cleanly.")
                break

            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")
                break

            # Build list URL
            if page == 1:
                list_url = _FINDER_URL
            else:
                b_start = (page - 1) * _PAGE_SIZE
                list_url = f"{_FINDER_URL}?page={page}&b_start={b_start}"

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_val}")

            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}, stopping.")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page}, done.")
                break

            new_on_page = 0
            for item in items:
                url = item.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                if saved >= limit_val:
                    break

                try:
                    record = self._fetch_detail(url, item)
                    if record is None:
                        print(f"[{_SITE_ID}] Could not fetch detail for {url}, skipping.")
                        continue
                    abstract = record.get("abstract", "") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] Skipping {url}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(record)
                    saved += 1
                    print(
                        f"[{_SITE_ID}] [{saved}] {record.get('title','')[:70]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

                time.sleep(_RATE_SLEEP)

            if new_on_page == 0:
                print(f"[{_SITE_ID}] No new items on page {page}, done.")
                break

            page += 1

        print(f"[{_SITE_ID}] Crawl complete: saved {saved} items.")
        return saved

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list:
        """Return list of {url, title, snippet, state, date_text} dicts."""
        items = []
        idx = html.find('id="consultations"')
        if idx < 0:
            return items
        section = html[idx:]

        li_pattern = re.compile(
            r'<li\s[^>]*data-consultation-state="([^"]*)"[^>]*>(.*?)</li>',
            re.DOTALL,
        )
        for m in li_pattern.finditer(section):
            state = m.group(1)
            li_html = m.group(2)

            # Title + URL
            link_m = re.search(
                r'<a[^>]+class="cs-no-underline"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                li_html, re.DOTALL,
            )
            if not link_m:
                continue
            url = link_m.group(1).strip()
            title = _strip_tags(link_m.group(2)).strip()
            if not url or not title:
                continue

            # Short description snippet (col-md-9 > span)
            desc_m = re.search(
                r'<div\s+class="col-md-9"[^>]*>\s*<span>(.*?)</span>',
                li_html, re.DOTALL,
            )
            snippet = _strip_tags(desc_m.group(1)).strip() if desc_m else ""

            # Date from cs-date-delta
            date_m = re.search(
                r'<div\s+class="cs-date-delta"[^>]*>(.*?)</div>',
                li_html, re.DOTALL,
            )
            date_text = _strip_tags(date_m.group(1)).strip() if date_m else ""

            items.append({
                "url": url,
                "title": title,
                "snippet": snippet,
                "state": state,
                "date_text": date_text,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str, list_item: dict) -> dict | None:
        """Fetch and parse a consultation detail page; returns paper dict or None."""
        raw = _curl_get(url)
        if not raw:
            return None

        # external_id = "{category}/{slug}"
        slug_m = re.match(
            r"https?://consult\.environment\.govt\.nz/([^/?#]+)/([^/?#]+)/?",
            url,
        )
        if slug_m:
            category = slug_m.group(1)
            slug = slug_m.group(2)
            external_id = f"{category}/{slug}"
            post_number = slug
        else:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            external_id = slug
            post_number = slug
            category = ""

        # Title
        title_m = re.search(
            r'<h1[^>]*id="cs-consultation-title-in-banner"[^>]*>\s*(.*?)\s*</h1>',
            raw, re.DOTALL,
        )
        title = (
            _strip_tags(title_m.group(1)).strip()
            if title_m
            else list_item.get("title", "")
        )

        # Open/close dates from sidebar
        primary_m = re.search(
            r'<p\s+class="cs-consultation-sidebar-primary-date"[^>]*>(.*?)</p>',
            raw, re.DOTALL,
        )
        secondary_m = re.search(
            r'<p\s+class="cs-consultation-sidebar-secondary-date"[^>]*>(.*?)</p>',
            raw, re.DOTALL,
        )
        primary_text = _strip_tags(primary_m.group(1)).strip() if primary_m else ""
        secondary_text = _strip_tags(secondary_m.group(1)).strip() if secondary_m else ""

        # published_date = opened date (when consultation started)
        # close_date     = when consultation closed
        published_date = _parse_date(secondary_text)
        close_date = _parse_date(primary_text)
        listed_date = published_date  # same as opened date

        # Abstract: overview section (primary) + results section (supplemental)
        overview_text = _extract_text_block(raw, "overview")
        # Remove leading "Overview" heading
        overview_text = re.sub(r"^Overview\s*", "", overview_text).strip()

        results_text = _extract_text_block(raw, "results")
        # Remove "Results updated DD Mon YYYY" heading
        results_text = re.sub(
            r"^Results updated\s+\d{1,2}\s+\w+\s+\d{4}\s*", "", results_text
        ).strip()

        parts = []
        if overview_text:
            parts.append(overview_text)
        if results_text and results_text not in overview_text:
            parts.append(results_text)
        if not parts and list_item.get("snippet"):
            parts.append(list_item["snippet"])

        abstract = " ".join(parts)[:8000].strip()

        # PDF links — prefer overview section, then any in page
        pdf_url = None
        original_filename = None
        ov_idx = raw.find('id="overview"')
        search_region = raw[ov_idx: ov_idx + 8000] if ov_idx >= 0 else raw
        pdf_m = re.search(r'href="([^"]+\.pdf)"', search_region, re.IGNORECASE)
        if pdf_m:
            pdf_url = pdf_m.group(1)
            fname_m = re.search(r"/([^/]+\.pdf)$", pdf_url, re.IGNORECASE)
            if fname_m:
                original_filename = fname_m.group(1)

        metadata = json.dumps(
            {
                "state": list_item.get("state", ""),
                "category": category,
                "close_date": close_date,
                "primary_date_raw": primary_text,
                "secondary_date_raw": secondary_text,
                "posted_date": listed_date,
                "snippet": list_item.get("snippet", ""),
            },
            ensure_ascii=False,
        )

        return {
            "site_id": _SITE_ID,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": "Ministry for the Environment",
            "department": None,
            "authors": None,
            "journal": None,
            "category": category,
            "keywords": None,
            "doi": None,
            "metadata": metadata,
        }
