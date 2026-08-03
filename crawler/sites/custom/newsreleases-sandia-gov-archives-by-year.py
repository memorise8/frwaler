# -*- coding: utf-8 -*-
"""Crawler for Sandia National Laboratories News Releases.

Starting URL: https://newsreleases.sandia.gov/archives-by-year/
API:          WordPress REST API  /wp-json/wp/v2/posts
              Returns full content + embedded categories/tags in one call per page.
              Total: ~2030 posts, 21 pages at 100/page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape

from crawler.base_crawler import BaseCrawler

_SITE_ID = "newsreleases-sandia-gov-archives-by-year"


class SandiaNewsReleasesCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: newsreleases-sandia-gov-archives-by-year"
    base_url = "https://newsreleases.sandia.gov"

    _API_BASE = "https://newsreleases.sandia.gov/wp-json/wp/v2/posts"
    _PER_PAGE = 100

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, timeout=45):
        """Fetch URL via curl with 3-attempt exponential backoff (1s, 3s, 9s)."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json, text/html;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = f"exit={result.returncode} stderr={result.stderr[:120]}"
            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl retry {attempt}/{len(waits)} "
                    f"for {url}: {last_error}; wait {wait}s"
                )
                time.sleep(wait)
        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML / text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html_str):
        """Strip HTML tags and decode entities; return clean plain text."""
        if not html_str:
            return ""
        text = html_str
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(text, parser)
                text = soup.get_text(separator=" ", strip=True)
                break
            except Exception:
                continue
        else:
            # Regex fallback if all parsers fail
            text = re.sub(r"<[^>]+>", " ", text)
            text = unescape(text)
        text = unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _clean_title(raw):
        if not raw:
            return ""
        return unescape(re.sub(r"\s+", " ", raw)).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else "inf"
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        while True:
            if limit is not None and saved >= limit:
                break
            if page > MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_SECS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping cleanly.")
                break

            if page % 10 == 0 or page == 1:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            api_url = (
                f"{self._API_BASE}"
                f"?per_page={self._PER_PAGE}"
                f"&page={page}"
                f"&_embed=true"
                f"&orderby=id&order=desc"
            )

            try:
                raw = self._curl_get(api_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] page {page} fetch error: {exc}. Stopping.")
                break

            if not raw:
                print(f"[{_SITE_ID}] empty response on page {page}. Stopping.")
                break

            try:
                posts = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{_SITE_ID}] JSON parse error at page {page}: {exc}. Stopping.")
                break

            if not isinstance(posts, list) or not posts:
                if isinstance(posts, dict) and "code" in posts:
                    print(f"[{_SITE_ID}] API error at page {page}: {posts.get('message', posts['code'])}. Done.")
                else:
                    print(f"[{_SITE_ID}] No posts on page {page}. Done.")
                break

            new_posts = [p for p in posts if p.get("link", "") not in seen_urls]
            if not new_posts:
                print(f"[{_SITE_ID}] All items on page {page} already seen. Stopping.")
                break
            for p in new_posts:
                seen_urls.add(p.get("link", ""))

            for post in new_posts:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_SECS:
                    print(f"[{_SITE_ID}] time budget reached mid-page. Stopping.")
                    break

                post_id = post.get("id")
                post_label = f"id={post_id}"

                try:
                    title = self._clean_title(
                        (post.get("title") or {}).get("rendered", "")
                    )
                    if not title:
                        print(f"[{_SITE_ID}] item {post_label} skipped: no title")
                        continue

                    # Abstract: strip HTML from full content
                    content_html = (post.get("content") or {}).get("rendered", "") or ""
                    abstract = self._strip_html(content_html)

                    # Fallback to excerpt if content is empty/short
                    if len(abstract) < 50:
                        excerpt_html = (post.get("excerpt") or {}).get("rendered", "") or ""
                        abstract = self._strip_html(excerpt_html)

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] item {post_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Dates
                    raw_date = post.get("date", "") or ""
                    published_date = raw_date[:10] if raw_date else ""
                    listed_date = published_date

                    url = post.get("link", "")
                    slug = post.get("slug", "")
                    post_number = str(post_id) if post_id is not None else None

                    # Embedded categories and tags
                    categories: list[str] = []
                    tags: list[str] = []
                    embedded = post.get("_embedded") or {}
                    for term_group in (embedded.get("wp:term") or []):
                        for term in (term_group or []):
                            taxonomy = term.get("taxonomy", "")
                            name = (term.get("name") or "").strip()
                            if not name:
                                continue
                            if taxonomy == "category":
                                categories.append(name)
                            elif taxonomy == "post_tag":
                                tags.append(name)

                    category_str = ", ".join(categories)
                    keywords_str = ", ".join(tags) if tags else category_str

                    # ACF custom fields
                    acf = post.get("acf") or {}
                    subheader = (acf.get("subheader") or "").strip()
                    media_contacts = acf.get("media_contact") or []
                    if isinstance(media_contacts, list):
                        authors_str = "; ".join(
                            str(c).strip() for c in media_contacts if c
                        )
                    else:
                        authors_str = str(media_contacts).strip() if media_contacts else ""

                    metadata = {
                        "slug": slug,
                        "modified": post.get("modified", ""),
                        "subheader": subheader,
                        "post_id": post_id,
                        "categories": categories,
                        "tags": tags,
                        "posted_date": listed_date,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_number,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": None,
                        "doi": None,
                        "keywords": keywords_str or None,
                        "category": category_str or None,
                        "publisher": "Sandia National Laboratories",
                        "department": "Sandia National Laboratories",
                        "authors": authors_str or None,
                        "journal": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {post_label} failed: {exc}")
                    continue

            page += 1
            time.sleep(self._delay)

        print(f"[{_SITE_ID}] crawl complete: saved {saved} items across {page - 1} pages")
        return saved
