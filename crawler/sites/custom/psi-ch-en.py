# -*- coding: utf-8 -*-
"""PSI (Paul Scherrer Institute) English News crawler — psi-ch-en.

Crawls https://www.psi.ch/en/news/news?facet_type%5B6%5D=6&langcode=All
for media releases, PSI stories, and science features.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.psi.ch"
_LIST_URL = f"{_BASE}/en/news/news"
_LIST_PARAMS = "facet_type%5B6%5D=6&langcode=All"

# Match <time datetime="..."> ... href="/en/news/<type>/<slug>" within a listing card.
# Non-greedy so it finds the closest article link after each timestamp.
_PAIR_RE = re.compile(
    r'<time[^>]*datetime="([^"]+)"[^>]*/?>.*?href="(/en/news/[^"?#]+)"',
    re.DOTALL,
)

# Node ID appears as /en/node/<id>/ inside image/download attributes
_NODE_RE = re.compile(r'/en/node/(\d+)/')

# Paragraph body blocks
_PARA_RE = re.compile(
    r'paragraph--type--text[^>]*>.*?<div class="text">(.*?)</div>',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_meta(html: str, prop: str) -> str:
    """Extract content="..." from a <meta property="prop"> tag."""
    m = re.search(rf'<meta[^>]+{re.escape(prop)}[^>]+>', html)
    if not m:
        return ""
    c = re.search(r'content="([^"]*)"', m.group(0))
    return unescape(c.group(1)) if c else ""


def _strip_html(raw: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', raw)
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _bs4_parse(src: str):
    """Parse HTML with html5lib → lxml → html.parser fallback. Returns soup or None."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(src, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class PsiChEnCrawler(BaseCrawler):
    """PSI English News — media releases, PSI stories, science features."""

    site_id = "psi-ch-en"
    site_name = "Custom: psi-ch-en"
    base_url = "https://www.psi.ch"

    BACKOFF = (1, 3, 9)
    MAX_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    MIN_ABSTRACT_CHARS = 50

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url: str, retries: int = 3) -> str | None:
        """Fetch URL via curl with exponential backoff. Returns text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                if res.stdout:
                    try:
                        return res.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return res.stdout.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]
                    print(f"[{self.site_id}] empty response for {url}, retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]
                    print(f"[{self.site_id}] fetch error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] failed after {retries} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------

    def _extract_pairs(self, html: str) -> list[tuple[str, str]]:
        """Return (datetime_str, relative_url) pairs from a listing page."""
        return _PAIR_RE.findall(html)

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str, listed_dt: str) -> dict | None:
        """Parse an article detail page into a paper dict. Returns None on failure."""

        # ── Title (og:title preferred; h1 fallback)
        title = _get_meta(html, "og:title")
        if not title:
            h1 = re.search(r'<h1[^>]*>.*?<span[^>]*>(.*?)</span>', html, re.DOTALL)
            title = _strip_html(h1.group(1)) if h1 else ""
        if not title:
            return None

        # ── og:description (used as abstract lead)
        og_desc = _get_meta(html, "og:description")

        # ── Published date (strip time/tz — keep YYYY-MM-DD)
        pub_raw = _get_meta(html, "article:published_time")
        published_date = pub_raw[:10] if pub_raw else (listed_dt[:10] if listed_dt else "")

        # ── Node ID → external_id / post_number
        m = _NODE_RE.search(html)
        node_id = m.group(1) if m else None
        # Fallback: use URL slug so external_id is never None
        external_id = node_id or url.rstrip("/").split("/")[-1]

        # ── Category from URL path segment
        cat_m = re.search(r'/en/news/([^/]+)/', url)
        category = cat_m.group(1) if cat_m else "news"

        # ── Body paragraphs: slice HTML before the Contact section boundary
        contact_idx = html.find('id="kontakt"')
        if contact_idx < 0:
            mc = re.search(r'>\s*Contact\s*<', html)
            contact_idx = mc.start() if mc else -1
        body_html = html[:contact_idx] if contact_idx > 0 else html

        text_parts: list[str] = []
        soup = _bs4_parse(body_html)
        if soup is not None:
            for para_div in soup.find_all(class_="paragraph--type--text"):
                text_div = para_div.find("div", class_="text")
                if not text_div:
                    continue
                text = text_div.get_text(separator=" ", strip=True)
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) < 30:
                    continue
                if "Paul Scherrer Institute PSI develops" in text:
                    continue
                text_parts.append(text)
        else:
            # Regex fallback when BeautifulSoup unavailable
            for raw_p in _PARA_RE.findall(body_html):
                text = _strip_html(raw_p)
                if len(text) >= 30 and "Paul Scherrer Institute PSI develops" not in text:
                    text_parts.append(text)

        # ── Tags: parse full HTML
        tags: list[str] = []
        full_soup = _bs4_parse(html)
        if full_soup is not None:
            tag_div = full_soup.find(class_="field--name-primer-tags")
            if tag_div:
                seen_t: set[str] = set()
                for s in tag_div.stripped_strings:
                    t = re.sub(r'^Further information about\s+', '', s.strip())
                    if t and 3 <= len(t) <= 80 and t not in seen_t:
                        seen_t.add(t)
                        tags.append(t)
        else:
            # Regex fallback for tags
            tag_m = re.search(r'field--name-primer-tags[^>]*>(.*?)</div>', html, re.DOTALL)
            if tag_m:
                seen_t = set()
                for chunk in re.split(r'\s{2,}', _strip_html(tag_m.group(1))):
                    t = re.sub(r'^Further information about\s+', '', chunk.strip())
                    if t and 3 <= len(t) <= 80 and t not in seen_t:
                        seen_t.add(t)
                        tags.append(t)

        # ── Abstract: og_desc + body paragraphs (deduplicated)
        abstract_parts: list[str] = []
        if og_desc:
            abstract_parts.append(og_desc)
        for p in text_parts:
            if p not in abstract_parts:
                abstract_parts.append(p)
        abstract = "\n\n".join(abstract_parts)

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_dt[:10] if listed_dt else None,
            "url": url,
            "pdf_url": None,
            "keywords": ", ".join(tags) if tags else None,
            "category": category,
            "publisher": "Paul Scherrer Institute PSI",
            "department": None,
            "authors": None,
            "journal": None,
            "doi": None,
            "original_filename": None,
            "metadata": json.dumps({
                "node_id": node_id,
                "article_type": category,
                "og_description": og_desc,
                "posted_date": listed_dt,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl PSI news and persist articles to DB.

        Parameters
        ----------
        limit:
            Maximum number of articles to save. None means unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        page = 0
        start_time = time.monotonic()
        lim_str = str(limit) if limit is not None else "∞"

        while page < self.MAX_PAGES:
            # Limit / budget guards
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time > self.MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-min wall-clock budget reached at page {page}. Exiting.")
                break
            if page == self.MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap ({self.MAX_PAGES} pages) reached. Stopping.")

            # Progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            list_url = f"{_LIST_URL}?{_LIST_PARAMS}&page={page}"
            html = self._curl(list_url)
            if not html:
                print(f"[{self.site_id}] failed to fetch listing page {page}. Stopping.")
                break

            pairs = self._extract_pairs(html)
            new_pairs = [(dt, u) for dt, u in pairs if u not in seen_urls]
            if not new_pairs:
                print(f"[{self.site_id}] no new articles at page {page}. Done.")
                break

            for listed_dt, rel_url in new_pairs:
                if limit is not None and saved >= limit:
                    break
                seen_urls.add(rel_url)
                full_url = _BASE + rel_url

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl(full_url)
                    if not detail_html:
                        print(f"[{self.site_id}] item {full_url} failed: empty response")
                        continue

                    paper = self._parse_detail(detail_html, full_url, listed_dt)
                    if not paper:
                        print(f"[{self.site_id}] item {full_url} failed: parse returned None")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {full_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {full_url} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
