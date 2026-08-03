# -*- coding: utf-8 -*-
"""Crawler for IMT Nord Europe Research News (English, WordPress REST API)."""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "research-imt-nord-europe-fr-news"


def _strip_html(html):
    """Strip HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#?[a-zA-Z0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_get(url, retries=3):
    """GET URL via curl with exponential-backoff retry. Returns decoded str or None."""
    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", url],
                capture_output=True, timeout=35,
            )
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt+1}/{retries}): {exc}")
        if attempt < retries - 1:
            time.sleep(waits[attempt])
    return None


def _bs_parse(html):
    """Parse HTML with fallback parsers. Returns BeautifulSoup or None."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class IMTNordEuropeNewsEnCrawler(BaseCrawler):
    """Crawler for IMT Nord Europe Research News (English)."""

    site_id = "research-imt-nord-europe-fr-news"
    site_name = "Custom: research-imt-nord-europe-fr-news"
    base_url = "https://research.imt-nord-europe.fr"

    _API = "https://research.imt-nord-europe.fr/wp-json/wp/v2/posts"
    _CAT = 75       # WordPress category ID for "News" (English)
    _PER_PAGE = 20
    _RATE_SLEEP = 1.0

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        p = 1
        while True:
            # Wall-clock budget
            if time.time() - start_time > MAX_WALL:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping cleanly.")
                break

            # Limit reached
            if saved >= limit_or_inf:
                break

            # Safety cap
            if p > 200:
                print(f"[{self.site_id}] Safety cap: 200 pages reached. Stopping.")
                break

            # Progress log every 10 pages
            if p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

            url = (
                f"{self._API}?per_page={self._PER_PAGE}&page={p}"
                f"&lang=en&categories={self._CAT}"
                f"&_fields=id,title,excerpt,content,date,link,slug"
            )

            raw = None
            for attempt in range(3):
                raw = _curl_get(url)
                if raw:
                    break
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] page {p} fetch failed (attempt {attempt+1}/3), retrying in {wait}s...")
                time.sleep(wait)

            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {p} after 3 attempts. Stopping.")
                break

            try:
                posts = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON parse error on page {p}: {exc}. Stopping.")
                break

            # WP REST API returns a JSON object (not array) when page is out of range
            if not isinstance(posts, list) or len(posts) == 0:
                print(f"[{self.site_id}] No more posts at page {p}. Done.")
                break

            new_on_page = 0
            for post in posts:
                if saved >= limit_or_inf:
                    break

                try:
                    post_url = post.get("link", "") or ""
                    if not post_url:
                        continue

                    # URL deduplication (guards against silent paginator loops)
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)
                    new_on_page += 1

                    post_id = str(post.get("id", ""))

                    title_obj = post.get("title") or {}
                    title = _strip_html(title_obj.get("rendered", ""))
                    if not title:
                        title = "(untitled)"

                    # Use full content for abstract (much richer than excerpt)
                    content_obj = post.get("content") or {}
                    abstract = _strip_html(content_obj.get("rendered", ""))

                    # Fallback to excerpt if content is empty/short
                    if len(abstract) < 100:
                        excerpt_obj = post.get("excerpt") or {}
                        fallback = _strip_html(excerpt_obj.get("rendered", ""))
                        if len(fallback) > len(abstract):
                            abstract = fallback

                    # Skip items with abstract < 50 chars (per requirement)
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping '{title[:40]}': abstract too short ({len(abstract)} chars)")
                        continue

                    published_date = ""
                    raw_date = post.get("date", "") or ""
                    if raw_date and len(raw_date) >= 10:
                        published_date = raw_date[:10]  # YYYY-MM-DD

                    # Extract PDF URL from content if present
                    content_html = (post.get("content") or {}).get("rendered", "") or ""
                    pdf_url = ""
                    pdf_match = re.search(r'href="([^"]+\.pdf)"', content_html, re.I)
                    if pdf_match:
                        pdf_url = pdf_match.group(1)

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "news",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url": post_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps({"slug": post.get("slug", "")}, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                    time.sleep(self._RATE_SLEEP)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {post.get('id', '?')} failed: {exc}")
                    continue

            # If no new (unseen) items appeared on this page, pagination has ended
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {p} (all seen). Done.")
                break

            p += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
