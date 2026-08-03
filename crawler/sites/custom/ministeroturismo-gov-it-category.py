# -*- coding: utf-8 -*-
"""Crawler for Ministero del Turismo – Comunicati Stampa category.

Uses the WordPress REST API (category 499) — no per-item detail fetch needed.
Starting URL: https://www.ministeroturismo.gov.it/category/comunicati-stampa/
WP REST endpoint: /wp-json/wp/v2/posts?categories=499
"""

import html as _html
import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_CATEGORY_ID = 499
_PER_PAGE = 100
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _find_pdf_url(html_content: str):
    m = re.search(r'href=["\']([^"\']+\.pdf(?:[^"\']*)?)["\']', html_content, re.I)
    return m.group(1) if m else None


def _curl_json(url: str, retries: int = 3):
    """GET via curl, parse JSON. Returns parsed object or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-A", _UA,
        "-H", "Accept: application/json",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            body = result.stdout.decode("utf-8", errors="replace").strip()
            if body:
                return json.loads(body)
        except json.JSONDecodeError:
            pass
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep((attempt + 1) * 3)
    return None


class MinisteroTurismoGovItCategoryCrawler(BaseCrawler):
    """Crawler for Ministero del Turismo – Comunicati Stampa (WP REST API)."""

    site_id = "ministeroturismo-gov-it-category"
    site_name = "Custom: ministeroturismo-gov-it-category"
    base_url = "https://www.ministeroturismo.gov.it"

    _API = "https://www.ministeroturismo.gov.it/wp-json/wp/v2/posts"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")
                break

            url = (
                f"{self._API}"
                f"?categories={_CATEGORY_ID}"
                f"&per_page={_PER_PAGE}"
                f"&page={page}"
                f"&orderby=date&order=desc"
                f"&_fields=id,slug,title,content,excerpt,date,modified,link"
            )

            posts = _curl_json(url)

            if posts is None:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            if isinstance(posts, dict):
                code = posts.get("code", "")
                if "invalid_page_number" in code or "no_posts" in code:
                    print(f"[{self.site_id}] End of pages at {page}: {code}")
                else:
                    print(f"[{self.site_id}] WP API error at page {page}: {code}. Stopping.")
                break

            if not isinstance(posts, list) or len(posts) == 0:
                print(f"[{self.site_id}] No posts at page {page}. Done.")
                break

            new_on_page = sum(1 for p in posts if p.get("link", "") not in seen_urls)
            if new_on_page == 0:
                print(f"[{self.site_id}] Page {page}: all URLs already seen. Stopping.")
                break

            for post in posts:
                if limit is not None and saved >= limit:
                    break

                try:
                    post_id = post.get("id")
                    post_url = post.get("link", "")

                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)

                    title_raw = (post.get("title") or {}).get("rendered", "") or ""
                    title = _html.unescape(re.sub(r"<[^>]+>", "", title_raw)).strip()

                    content_html = (post.get("content") or {}).get("rendered", "") or ""
                    excerpt_html = (post.get("excerpt") or {}).get("rendered", "") or ""

                    abstract = _strip_html(content_html)
                    if len(abstract) < 100:
                        abstract = _strip_html(excerpt_html)
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping post {post_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    date_str = post.get("date", "")
                    modified_str = post.get("modified", "")
                    published_date = date_str[:10] if date_str else ""
                    slug = post.get("slug", "")

                    pdf_url = _find_pdf_url(content_html)
                    original_filename = None
                    if pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if tail:
                            original_filename = tail

                    paper = {
                        "site_id": self.site_id,
                        "external_id": str(post_id),
                        "post_number": str(post_id),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": published_date,
                        "listed_date": published_date,
                        "url": post_url,
                        "pdf_url": pdf_url or None,
                        "original_filename": original_filename,
                        "authors": None,
                        "publisher": "Ministero del Turismo",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "Comunicati Stampa",
                        "doi": None,
                        "metadata": json.dumps({
                            "posted_date": published_date,
                            "wp_id": post_id,
                            "slug": slug,
                            "modified": modified_str,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {post.get('id', '?')} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            time.sleep(self._delay)
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
