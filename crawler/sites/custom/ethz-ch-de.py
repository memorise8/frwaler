# -*- coding: utf-8 -*-
"""ETH Zürich Medienmitteilungen (press releases) crawler.

API: https://ethz.ch/de/news-und-veranstaltungen/medien/medienmitteilungen
     /_jcr_content/par/newsfeed2.newsfeed.{start}-{end}.json

Returns JSON with `entries` list and `hasMore` boolean.
Each entry has: id, title, author, link, tag, lead, published, updated.
Detail pages are fetched per-item for PDF URL and richer abstract.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://ethz.ch"
_LIST_API = (
    _BASE_URL
    + "/de/news-und-veranstaltungen/medien/medienmitteilungen"
    "/_jcr_content/par/newsfeed2.newsfeed.{start}-{end}.json"
)
_PAGE_SIZE = 16


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso_date(iso_str: str):
    """Parse ISO 8601 datetime string → YYYY-MM-DD, or None on failure."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", iso_str)
        return m.group(1) if m else None


def _strip_html(html: str) -> str:
    """Strip HTML tags; tries html5lib → lxml → html.parser → regex fallback."""
    if not html:
        return ""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, parser)
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()
        except Exception:
            continue
    # Regex fallback
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class EthzChDeCrawler(BaseCrawler):
    """Crawler for ETH Zürich Medienmitteilungen (German press releases)."""

    site_id = "ethz-ch-de"
    site_name = "Custom: ethz-ch-de"
    base_url = "https://ethz.ch"

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3):
        """GET url via curl with TLS flags. Returns decoded body or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json,text/html,*/*;q=0.8",
            "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                body = result.stdout
                if body:
                    return body.decode("utf-8", errors="replace")
                wait = waits[min(attempt, len(waits) - 1)]
                print(
                    f"[{self.site_id}] Empty response from {url!r}, "
                    f"retry {attempt+1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            except subprocess.TimeoutExpired:
                wait = waits[min(attempt, len(waits) - 1)]
                print(
                    f"[{self.site_id}] Timeout on {url!r}, "
                    f"retry {attempt+1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            except Exception as exc:
                wait = waits[min(attempt, len(waits) - 1)]
                print(
                    f"[{self.site_id}] curl error ({exc}), "
                    f"retry {attempt+1}/{retries} in {wait}s"
                )
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after {retries} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List API
    # ------------------------------------------------------------------

    def _fetch_list_page(self, start: int):
        """Fetch one page of the news-feed JSON API. Returns (entries, has_more)."""
        url = _LIST_API.format(start=start, end=start + _PAGE_SIZE)
        raw = self._curl_get(url)
        if not raw:
            return [], False
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON parse error at offset {start}: {exc}")
            return [], False
        entries = data.get("entries") or []
        has_more = bool(data.get("hasMore"))
        return entries, has_more

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict:
        """Fetch detail page; extract PDF URL, published date, abstract."""
        result = {"pdf_url": None, "published_date": None, "abstract": None}
        raw = self._curl_get(url)
        if not raw:
            return result

        # PDF links embedded in /content/dam/... paths
        pdf_matches = re.findall(
            r'href="(/content/dam/[^"]+\.pdf[^"]*)"', raw, re.IGNORECASE
        )
        if pdf_matches:
            result["pdf_url"] = _BASE_URL + pdf_matches[0].split('"')[0]

        # Published date from <time datetime="...">
        dt_matches = re.findall(r'datetime="([^"]+)"', raw)
        if dt_matches:
            result["published_date"] = _parse_iso_date(dt_matches[0])

        # Abstract: prefer <meta name="description">
        m = re.search(
            r'<meta\s+name="description"\s+content="([^"]+)"', raw, re.IGNORECASE
        )
        if m:
            result["abstract"] = m.group(1).strip()
        else:
            # Fallback: find lead paragraph via BeautifulSoup
            try:
                soup = None
                for parser in ("html5lib", "lxml", "html.parser"):
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(raw, parser)
                        break
                    except Exception:
                        continue
                if soup:
                    lead_el = soup.find(class_=re.compile(r"lead", re.I))
                    if lead_el:
                        text = re.sub(r"\s+", " ", lead_el.get_text(separator=" ")).strip()
                        if text:
                            result["abstract"] = text
            except Exception:
                pass

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl ETH Zürich Medienmitteilungen. Returns count of saved items."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        lim_display = str(limit) if limit is not None else "inf"

        offset = 0
        page = 0

        try:
            while True:
                # Safety caps
                if page >= self._MAX_PAGES:
                    print(
                        f"[{self.site_id}] Reached safety cap of {self._MAX_PAGES} pages. Stopping."
                    )
                    break
                elapsed = time.time() - start_time
                if elapsed > self._WALL_CLOCK_BUDGET:
                    print(
                        f"[{self.site_id}] Wall-clock budget exceeded ({elapsed:.0f}s). Stopping."
                    )
                    break
                if limit is not None and saved >= limit:
                    break

                entries, has_more = self._fetch_list_page(offset)
                page += 1

                if not entries:
                    print(f"[{self.site_id}] Page {page} (offset={offset}): no entries. Done.")
                    break

                # Progress log every 10 pages
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_display}")

                for entry in entries:
                    if limit is not None and saved >= limit:
                        break

                    link = entry.get("link") or entry.get("id") or ""
                    if not link:
                        continue

                    full_url = (_BASE_URL + link) if link.startswith("/") else link

                    # URL deduplication — prevents infinite loops on paginator wrap-around
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    try:
                        title = (entry.get("title") or "").strip()
                        if not title:
                            print(f"[{self.site_id}] Skipping entry with no title: {full_url}")
                            continue

                        # Clean lead: strip any inline HTML, entities, extra whitespace
                        lead = entry.get("lead") or ""
                        if "<" in lead:
                            lead = _strip_html(lead)
                        lead = lead.replace("\xa0", " ")
                        lead = re.sub(r"\s+", " ", lead).strip()

                        # Dates from API (ISO 8601)
                        pub_date = _parse_iso_date(
                            entry.get("published") or entry.get("updated") or ""
                        )

                        author = (entry.get("author") or "").strip()
                        category = (entry.get("tag") or "").strip()

                        # Slug for post_number (last path segment before .html)
                        slug_m = re.search(r"/([^/]+)\.html$", link)
                        slug = slug_m.group(1) if slug_m else link.rstrip("/").split("/")[-1]

                        # Fetch detail page for PDF URL and richer content
                        detail = self._fetch_detail(full_url)
                        time.sleep(self._delay)

                        pdf_url = detail.get("pdf_url")
                        if detail.get("published_date"):
                            pub_date = detail["published_date"]

                        # Abstract: take the longer of API lead vs detail meta description
                        det_abs = (detail.get("abstract") or "").strip()
                        abstract = det_abs if len(det_abs) > len(lead) else lead
                        # If both are short, try the other one
                        if len(abstract) < 100 and (det_abs or lead):
                            abstract = det_abs if det_abs else lead

                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] Skip '{title[:50]}': "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        # Original filename from PDF URL
                        original_filename = None
                        if pdf_url:
                            fname = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                            if fname:
                                original_filename = fname

                        metadata = {
                            "posted_date": entry.get("published") or entry.get("updated"),
                            "commentCount": entry.get("commentCount"),
                            "imageBigHash": entry.get("imageBigHash"),
                            "tagLink": entry.get("tagLink"),
                            "lead_raw": entry.get("lead"),
                        }

                        paper = {
                            "site_id": self.site_id,
                            "external_id": link,        # stable path-based dedup key
                            "title": title,
                            "abstract": abstract,
                            "published_date": pub_date,
                            "listed_date": pub_date,
                            "url": full_url,
                            "authors": author or None,
                            "department": "ETH Zürich", # → publisher via adapter
                            "category": category or None,
                            "keywords": category or None,
                            "pdf_url": pdf_url or None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_display}: {title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {full_url} failed: {exc}")
                        continue

                if not has_more:
                    print(f"[{self.site_id}] No more pages after page {page}. Done.")
                    break

                offset += _PAGE_SIZE

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted. Saved {saved} items so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
