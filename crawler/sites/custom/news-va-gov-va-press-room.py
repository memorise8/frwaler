# -*- coding: utf-8 -*-
"""VA Press Room (news.va.gov) crawler.

Starting URL: https://news.va.gov/va-press-room/?redirect=1

The press room listing is a WP Grid Builder masonry grid backed by AJAX,
but its REST filter endpoint (``/wp-json/wpgb/v2/filter/``) ignores the
``page``/``paged``/``offset`` params sent from a plain HTTP client and
always returns page 1 — it appears to depend on client-side JS state
that isn't reproducible via a bare POST. The site's press releases are
also exposed as a dedicated WordPress custom post type (``news-releases``)
through the standard WP REST API, which supports proper pagination and
returns full HTML content (title/excerpt/content/terms/dates) in one
shot — no separate detail-page fetch needed:

    https://news.va.gov/wp-json/wp/v2/news-releases?per_page=100&page=N&_embed=1
"""

from __future__ import annotations

import html as html_mod
import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


class NewsVaGovVaPressRoomCrawler(BaseCrawler):
    """Crawler for the VA Press Room (news.va.gov/va-press-room)."""

    site_id = "news-va-gov-va-press-room"
    site_name = "Custom: news-va-gov-va-press-room"
    base_url = "https://news.va.gov"

    _API_URL = "https://news.va.gov/wp-json/wp/v2/news-releases"
    _PUBLISHER = "U.S. Department of Veterans Affairs"
    _PER_PAGE = 100
    _MIN_ABSTRACT = 100
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _strip_html(self, raw: str) -> str:
        """Strip HTML tags/entities from a WP-rendered HTML fragment."""
        if not raw:
            return ""
        text = None
        if _BS is not None:
            try:
                soup = _make_soup(raw)
                text = soup.get_text(" ", strip=True)
            except Exception as exc:
                print(f"[{self.site_id}] bs4 parse failed ({exc}); falling back to regex strip")
                text = None
        if text is None:
            text = re.sub(r"<[^>]+>", " ", raw)
            text = html_mod.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _embedded_terms(item: dict) -> tuple[list, list]:
        """Return (news_release_topics, news_release_sections) name lists."""
        topics, sections = [], []
        groups = (item.get("_embedded") or {}).get("wp:term") or []
        for group in groups:
            for term in group:
                tax = term.get("taxonomy")
                name = term.get("name")
                if not name:
                    continue
                if tax == "news-release-topics":
                    topics.append(name)
                elif tax == "news-release-sections":
                    sections.append(name)
        return topics, sections

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 1
        lim_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget (25min) exceeded. Stopping cleanly.")
                    break

                url = f"{self._API_URL}?per_page={self._PER_PAGE}&page={page}&_embed=1"
                raw = self._curl_get(url)
                if not raw:
                    print(f"[{self.site_id}] page {page}: fetch failed after retries. Stopping pagination.")
                    break

                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError) as exc:
                    print(f"[{self.site_id}] page {page}: JSON decode error ({exc}). Stopping pagination.")
                    break

                if isinstance(data, dict):
                    # WP REST error object, e.g. rest_post_invalid_page_number -> end reached.
                    print(f"[{self.site_id}] page {page}: API error {data.get('code')!r}; end of pagination.")
                    break

                if not isinstance(data, list) or not data:
                    print(f"[{self.site_id}] page {page}: no items returned; end of pagination.")
                    break

                new_count = 0
                for item in data:
                    if limit is not None and saved >= limit:
                        break

                    wp_id = item.get("id")
                    try:
                        link = (item.get("link") or "").strip()
                        if not link:
                            continue
                        if link in seen_urls:
                            continue
                        seen_urls.add(link)
                        new_count += 1

                        title = self._strip_html((item.get("title") or {}).get("rendered", ""))
                        if not title:
                            print(f"[{self.site_id}] item {wp_id}: empty title, skipping")
                            continue

                        date_raw = item.get("date") or ""
                        published_date = date_raw[:10] if len(date_raw) >= 10 else None

                        abstract = self._strip_html((item.get("excerpt") or {}).get("rendered", ""))
                        if len(abstract) < self._MIN_ABSTRACT:
                            content_text = self._strip_html((item.get("content") or {}).get("rendered", ""))
                            if len(content_text) > len(abstract):
                                abstract = content_text[:1200].strip()
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] item {wp_id}: abstract too short ({len(abstract)} chars), skipping")
                            continue

                        topics, sections = self._embedded_terms(item)
                        keywords = ", ".join(topics) if topics else None
                        category = "; ".join(sections) if sections else None

                        meta_box = item.get("meta_box") or {}
                        author_info = ((item.get("_embedded") or {}).get("author") or [{}])[0]

                        meta = {
                            "posted_date": date_raw,
                            "originalFilename": None,
                            "wp_post_id": wp_id,
                            "modified": item.get("modified"),
                            "news_release_topics": topics,
                            "news_release_sections": sections,
                            "author_login": author_info.get("slug"),
                        }
                        subheading = meta_box.get("release_subheading")
                        if subheading:
                            meta["release_subheading"] = self._strip_html(subheading)

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": str(wp_id) if wp_id is not None else None,
                            "post_number": str(wp_id) if wp_id is not None else None,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": published_date,
                            "authors": None,
                            "publisher": self._PUBLISHER,
                            "department": None,
                            "journal": None,
                            "url": link,
                            "pdf_url": None,
                            "keywords": keywords,
                            "category": category,
                            "doi": None,
                            "original_filename": None,
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {wp_id!r} failed: {exc}; continuing.")
                        continue

                if page % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

                if new_count == 0:
                    print(f"[{self.site_id}] page {page}: 0 new records; stopping.")
                    break

                page += 1
                time.sleep(self._delay)

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
