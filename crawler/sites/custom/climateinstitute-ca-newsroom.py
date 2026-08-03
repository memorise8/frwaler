# -*- coding: utf-8 -*-
"""Canadian Climate Institute - News Releases crawler.

Uses the WordPress REST API (/wp-json/wp/v2/news) which returns full content
in the list endpoint — no per-item detail fetches needed.
Total corpus: ~108 posts in ~2 pages at per_page=100.
"""

import html
import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_API_BASE = "https://climateinstitute.ca/wp-json/wp/v2/news"
_PAGE_SIZE = 100  # WP REST API max per_page


def _strip_html(raw: str) -> str:
    """Strip HTML tags, decode entities, normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _curl_get(url: str, site_id: str = "climateinstitute-ca-newsroom", retries: int = 3) -> str | None:
    """GET via curl with exponential-backoff retry. Returns response body or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "-skL", "--tls-max", "1.3", "--max-time", "30", url],
                capture_output=True,
                timeout=35,
            )
            body = result.stdout.decode("utf-8", errors="replace").strip()
            if body:
                return body
        except Exception as exc:
            print(f"[{site_id}] curl error (attempt {attempt + 1}/{retries}): {exc}")

        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            print(f"[{site_id}] Retrying in {wait}s...")
            time.sleep(wait)

    return None


class ClimateInstituteNewsroomCrawler(BaseCrawler):
    """Crawler for Canadian Climate Institute news releases (climateinstitute.ca)."""

    site_id = "climateinstitute-ca-newsroom"
    site_name = "Custom: climateinstitute-ca-newsroom"
    base_url = "https://climateinstitute.ca"

    def crawl(self, limit=None):
        """Crawl news releases via the WordPress REST API.

        Paginates through /wp-json/wp/v2/news until the limit is reached,
        no new records appear, or a safety cap is hit.
        """
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        limit_display = str(limit) if limit is not None else "inf"

        while True:
            # Time budget
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached. Stopping cleanly.")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            url = (
                f"{_API_BASE}"
                f"?per_page={_PAGE_SIZE}"
                f"&page={page}"
                f"&_fields=id,title,excerpt,content,date,link,slug"
                f"&orderby=date&order=desc"
            )

            raw = _curl_get(url, site_id=self.site_id)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page} after retries. Stopping.")
                break

            try:
                posts = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON decode error at page {page}: {exc}. Stopping.")
                break

            # WP REST API returns a dict with "code" when page number exceeds total pages
            if isinstance(posts, dict):
                print(f"[{self.site_id}] API returned error at page {page}: {posts.get('code', posts)}. Done.")
                break

            if not isinstance(posts, list) or not posts:
                print(f"[{self.site_id}] No items at page {page}. Done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            new_on_page = 0

            for post in posts:
                if limit is not None and saved >= limit:
                    break

                try:
                    post_id = post.get("id")
                    post_url = post.get("link", "")
                    slug = post.get("slug", "")
                    date_raw = post.get("date", "")

                    # URL-based deduplication to detect pagination loops
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)

                    title = _strip_html(post.get("title", {}).get("rendered", ""))
                    if not title:
                        print(f"[{self.site_id}] Post {post_id} has no title, skipping.")
                        continue

                    # Abstract: full content preferred; excerpt as fallback
                    content_html = post.get("content", {}).get("rendered", "")
                    excerpt_html = post.get("excerpt", {}).get("rendered", "")
                    abstract = _strip_html(content_html)
                    if len(abstract) < 50:
                        abstract = _strip_html(excerpt_html)
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Post {post_id} abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    published_date = date_raw[:10] if date_raw else None
                    external_id = str(post_id) if post_id is not None else slug
                    post_number = str(post_id) if post_id is not None else None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": post_url,
                        "pdf_url": None,
                        "authors": None,
                        "publisher": "Canadian Climate Institute",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": None,
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_raw,
                                "slug": slug,
                                "wp_id": post_id,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_display}: {title[:60]}")

                except Exception as exc:
                    print(f"[{self.site_id}] Post {post.get('id', '?')} failed: {exc}")
                    continue

            # All items on this page were already seen — pagination loop detected
            if new_on_page == 0 and posts:
                print(f"[{self.site_id}] No new records on page {page} (all duplicates). Done.")
                break

            page += 1
            # Sleep between pages (not per-item: list API returns full content)
            if limit is None or saved < limit:
                time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
