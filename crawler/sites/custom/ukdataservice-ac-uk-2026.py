# -*- coding: utf-8 -*-
"""UK Data Service news crawler — WordPress REST API.

Starting URL: https://ukdataservice.ac.uk/2026/03/20/latest-data-collections-and-new-editions-20-march-2026/
Strategy: Walk /wp-json/wp/v2/posts (100 per page) and extract full content + Yoast schema metadata.
"""

import html as html_mod
import json
import re
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "ukdataservice-ac-uk-2026"
_API_BASE = "https://ukdataservice.ac.uk/wp-json/wp/v2/posts"
_PER_PAGE = 100
_MAX_PAGES = 200
_WALL_BUDGET_S = 25 * 60  # 25 minutes

# Fields to request from WP REST API (content is large but needed for abstract)
_FIELDS = "id,slug,date,title,excerpt,content,tags,categories,link,yoast_head_json"


def _bs4_strip(raw: str) -> str:
    """Strip HTML with BeautifulSoup fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, parser)
            return soup.get_text(separator=" ", strip=True)
        except Exception:
            continue
    # Final fallback: regex
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = _bs4_strip(raw)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class UKDataService2026Crawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: ukdataservice-ac-uk-2026"
    base_url = "https://ukdataservice.ac.uk"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "inf"

        page = 1
        while page <= _MAX_PAGES:
            # Wall-clock budget check
            if time.time() - start_time > _WALL_BUDGET_S:
                print(f"[{_SITE_ID}] Wall-clock budget (25 min) reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 1 and page % 10 == 1:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            # Fetch page with retry + exponential backoff
            params = {
                "per_page": _PER_PAGE,
                "page": page,
                "_fields": _FIELDS,
            }
            posts = None
            for attempt in range(3):
                try:
                    resp = self._session.get(_API_BASE, params=params, timeout=30)
                    if resp.status_code == 400:
                        # WP returns 400 when page > total pages
                        print(f"[{_SITE_ID}] Page {page} returned 400 — no more pages.")
                        posts = []
                        break
                    resp.raise_for_status()
                    raw_text = resp.content.decode("utf-8", errors="replace")
                    posts = json.loads(raw_text)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    wait = (attempt + 1) ** 2  # 1, 4, 9
                    print(f"[{_SITE_ID}] Page {page} attempt {attempt + 1}/3 failed: {exc}")
                    if attempt < 2:
                        time.sleep(wait)

            if posts is None:
                print(f"[{_SITE_ID}] Failed to fetch page {page} after 3 attempts. Stopping.")
                break

            if not posts:
                print(f"[{_SITE_ID}] No more posts at page {page}. Done.")
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

                    post_id = post.get("id")
                    external_id = str(post_id) if post_id is not None else None
                    post_number = external_id

                    # Title
                    title_raw = post.get("title", {})
                    if isinstance(title_raw, dict):
                        title = _strip_html(title_raw.get("rendered", ""))
                    else:
                        title = _strip_html(str(title_raw or ""))
                    if not title:
                        print(f"[{_SITE_ID}] Skipping post {external_id}: empty title")
                        continue

                    # Date
                    raw_date = post.get("date", "")
                    published_date = raw_date[:10] if raw_date else None

                    # Abstract: prefer full content, fallback to excerpt
                    content_raw = post.get("content", {})
                    if isinstance(content_raw, dict):
                        content_raw = content_raw.get("rendered", "")
                    abstract = _strip_html(content_raw or "")

                    if len(abstract) < 50:
                        excerpt_raw = post.get("excerpt", {})
                        if isinstance(excerpt_raw, dict):
                            excerpt_raw = excerpt_raw.get("rendered", "")
                        abstract = _strip_html(excerpt_raw or "")

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] Skipping post {external_id} ({title[:40]}): "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    # Author, keywords, category from Yoast JSON-LD schema
                    authors = ""
                    keywords = ""
                    category = ""
                    yoast = post.get("yoast_head_json") or {}
                    schema = yoast.get("schema") or {}
                    for graph_node in schema.get("@graph", []):
                        if graph_node.get("@type") == "Article":
                            author_obj = graph_node.get("author") or {}
                            if isinstance(author_obj, dict):
                                authors = author_obj.get("name", "") or ""
                            kws = graph_node.get("keywords") or []
                            keywords = ",".join(kws) if kws else ""
                            sections = graph_node.get("articleSection") or []
                            category = ",".join(sections) if sections else ""
                            break

                    metadata = {
                        "post_id": post_id,
                        "slug": post.get("slug", ""),
                        "categories": post.get("categories", []),
                        "tags": post.get("tags", []),
                        "posted_date": raw_date,
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": post_url,
                        "pdf_url": None,
                        "authors": authors,
                        "publisher": "UK Data Service",
                        "department": "",
                        "journal": "",
                        "keywords": keywords,
                        "category": category,
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {post.get('id', '?')} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] All {len(posts)} posts on page {page} already seen. Stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            page += 1
            time.sleep(self._delay)

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
