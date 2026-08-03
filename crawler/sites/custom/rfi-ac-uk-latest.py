# -*- coding: utf-8 -*-
"""Crawler for Rosalind Franklin Institute – Latest (News).

Uses the WordPress REST API:
  GET https://www.rfi.ac.uk/wp-json/wp/v2/posts
    ?categories=8          # 8 = "news"
    &per_page=10
    &page=N
    &_fields=id,title,excerpt,content,date,link,slug,categories

~138 news posts across 14 pages as of 2026-05.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BACKOFF = (1, 3, 9)
_MIN_ABSTRACT = 50
_SAFETY_CAP = 200
_PER_PAGE = 10
_CATEGORY_NEWS = 8
_API_BASE = "https://www.rfi.ac.uk/wp-json/wp/v2/posts"


def _strip_html(html_str: str) -> str:
    """Strip HTML tags and decode entities into plain text."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class RfiAcUkLatestCrawler(BaseCrawler):
    site_id = "rfi-ac-uk-latest"
    site_name = "Custom: rfi-ac-uk-latest"
    base_url = "https://www.rfi.ac.uk"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set = set()
        limit_or_inf = str(limit) if limit is not None else "∞"
        start_time = time.time()

        while True:
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-min wall budget reached; exiting cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page > _SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {_SAFETY_CAP} pages reached; exiting")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            url = (
                f"{_API_BASE}?per_page={_PER_PAGE}"
                f"&categories={_CATEGORY_NEWS}"
                f"&page={page}"
                f"&_fields=id,title,excerpt,content,date,link,slug,categories"
            )

            raw = self._api_get(url)
            if raw is None:
                print(f"[{self.site_id}] page {page} fetch failed after retries; stopping")
                break

            try:
                posts = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] page {page} JSON parse error: {exc}; stopping")
                break

            # WP REST API returns a dict with "code" key when the page is out of range
            if isinstance(posts, dict) and posts.get("code"):
                print(f"[{self.site_id}] page {page}: API error '{posts.get('code')}'; end of results")
                break

            if not isinstance(posts, list) or len(posts) == 0:
                print(f"[{self.site_id}] page {page}: no posts returned; end of results")
                break

            new_on_page = 0
            for post in posts:
                if limit is not None and saved >= limit:
                    break
                try:
                    post_url = post.get("link", "")
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)
                    new_on_page += 1

                    post_id = str(post.get("id", ""))
                    title_raw = (post.get("title") or {}).get("rendered") or ""
                    title = _strip_html(title_raw).strip()

                    # Abstract: use full content (stripped), fallback to excerpt
                    content_html = (post.get("content") or {}).get("rendered") or ""
                    excerpt_html = (post.get("excerpt") or {}).get("rendered") or ""
                    abstract = _strip_html(content_html).strip()
                    if len(abstract) < _MIN_ABSTRACT:
                        abstract = _strip_html(excerpt_html).strip()
                        # Remove trailing "[…]" truncation marker
                        abstract = re.sub(r"\s*\[[…\.]{1,3}\]\s*$", "", abstract).strip()

                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] post {post_id} abstract too short "
                            f"({len(abstract)} chars); skipping"
                        )
                        continue

                    date_raw = post.get("date", "")
                    pub_date = date_raw[:10] if date_raw else None  # YYYY-MM-DD
                    slug = post.get("slug", "")
                    cat_ids = post.get("categories", [])

                    metadata = {
                        "posted_date": date_raw,
                        "post_id": post_id,
                        "slug": slug,
                        "category_ids": cat_ids,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "post_number": post_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": pub_date,
                        "url": post_url,
                        "pdf_url": None,
                        "authors": None,
                        "publisher": "Rosalind Franklin Institute",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "news",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1
                    time.sleep(0.1)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] post {post.get('id', '?')} failed: {exc}; continuing")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _api_get(self, url: str) -> "str | None":
        """Fetch a URL with curl, returning decoded body string or None after 3 retries."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl exit {result.returncode} "
                    f"(attempt {attempt + 1}/3): {stderr[:120]}"
                )
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout (attempt {attempt + 1}/3)")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}")
            if attempt < 2:
                time.sleep(_BACKOFF[attempt])
        return None
