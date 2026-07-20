# -*- coding: utf-8 -*-
"""Policy Alternatives Canada — Reports crawler.

Target: https://www.policyalternatives.ca/news-research/category/reports/
API:    https://www.policyalternatives.ca/wp-json/wp/v2/posts
        ?categories=2812&per_page=100&page=N&_embed

Uses curl subprocess because the site is behind Cloudflare which fingerprint-
blocks the Python requests library on TLS negotiation.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.policyalternatives.ca"
_API_BASE = f"{_BASE_URL}/wp-json/wp/v2"
_CATEGORY_ID = 2812  # "Reports" (confirmed via WP REST API)
_PER_PAGE = 20  # keep response <1.5 MB; Cloudflare times out at 100+_embed
_PUBLISHER = "Canadian Centre for Policy Alternatives"
_PDF_RE = re.compile(
    r'https?://(?:www\.)?policyalternatives\.ca/wp-content/uploads/[^\s"\'<>]+\.pdf',
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_BACKOFF = (1, 3, 9)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _curl_json(url: str, params: dict | None = None, retries: int = 3) -> list | dict | None:
    """GET via curl; returns parsed JSON or None on failure."""
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"

    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "-sk", "--tls-max", "1.3", "--max-time", "40",
                    "-A", _UA,
                    "-H", "Accept: application/json",
                    full_url,
                ],
                capture_output=True,
                timeout=45,
            )
            body = result.stdout
            if not body:
                raise ValueError("empty response")
            return json.loads(body.decode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"[policyalternatives-ca-news-research] curl error (attempt {attempt+1}/{retries}): {exc}")
            if attempt < retries - 1:
                time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(html: str):
    """Return BeautifulSoup with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _extract_abstract(excerpt_html: str, content_html: str) -> str:
    """Best-effort abstract: stripped excerpt → first substantial paragraph."""
    abstract = _strip_html(excerpt_html)
    if len(abstract) >= 80:
        return abstract
    if content_html:
        try:
            soup = _make_soup(content_html)
            if soup:
                for p in soup.find_all("p"):
                    text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
                    if len(text) >= 80:
                        return text
        except Exception:
            pass
        for m in re.finditer(r"<p[^>]*>(.*?)</p>", content_html, re.DOTALL | re.IGNORECASE):
            text = _strip_html(m.group(1))
            if len(text) >= 80:
                return text
    return abstract


class PolicyAlternativesCaNewsResearchCrawler(BaseCrawler):
    """Crawler for CCPA News & Commentary via WordPress REST API (curl)."""

    site_id = "policyalternatives-ca-news-research"
    site_name = "Custom: policyalternatives-ca-news-research"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay: float = 1.0):
        super().__init__(db_conn, delay)
        self._author_cache: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_author_name(name: str) -> str:
        """Normalize slug-style names to display names ('peggy-nash' → 'Peggy Nash')."""
        if name and name == name.lower() and "-" in name:
            return name.replace("-", " ").title()
        return name

    def _resolve_authors(self, coauthor_ids: list[int]) -> str:
        """Batch-resolve coauthor taxonomy IDs → semicolon-joined name string."""
        if not coauthor_ids:
            return ""
        to_fetch = [i for i in coauthor_ids if i not in self._author_cache]
        if to_fetch:
            data = _curl_json(
                f"{_API_BASE}/coauthors",
                params={"include": ",".join(str(i) for i in to_fetch), "per_page": 100},
            )
            if isinstance(data, list):
                for author in data:
                    aid = author.get("id")
                    name = self._format_author_name((author.get("name") or "").strip())
                    if aid and name:
                        self._author_cache[aid] = name
        names = [self._author_cache[i] for i in coauthor_ids if i in self._author_cache]
        return "; ".join(names)

    def _extract_pdf_url(self, content_html: str) -> str | None:
        """Extract first own-domain PDF URL from post HTML content."""
        if not content_html:
            return None
        m = _PDF_RE.search(content_html)
        if not m:
            return None
        return m.group(0).split("?")[0].split("#")[0]

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl News & Commentary posts via WP REST API with full pagination."""
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.time()
        max_wall_seconds = 25 * 60
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            # Wall-clock budget
            if time.time() - start_time > max_wall_seconds:
                print(
                    f"[{self.site_id}] Wall-clock budget (25 min) reached at page {page}. Stopping."
                )
                break

            # Safety cap
            if page > 200:
                print(f"[{self.site_id}] Safety cap of 200 pages reached. Stopping.")
                break

            # Limit check
            if limit is not None and saved >= limit:
                break

            # Progress every 10 pages
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            params = {
                "categories": _CATEGORY_ID,
                "per_page": _PER_PAGE,
                "page": page,
                "_embed": "wp:term",
                "orderby": "date",
                "order": "desc",
            }

            posts = _curl_json(f"{_API_BASE}/posts", params=params)

            if posts is None:
                print(f"[{self.site_id}] Page {page}: failed to fetch. Stopping.")
                break

            if not isinstance(posts, list):
                # WP returns a dict error object when page is out of range
                print(f"[{self.site_id}] Page {page}: unexpected response. Done.")
                break

            if not posts:
                print(f"[{self.site_id}] Page {page}: no posts returned. Done.")
                break

            # Batch-resolve all coauthor IDs on this page for efficiency
            page_coauthor_ids: list[int] = []
            for post in posts:
                for cid in (post.get("coauthors") or []):
                    if isinstance(cid, int) and cid not in self._author_cache:
                        page_coauthor_ids.append(cid)
            if page_coauthor_ids:
                self._resolve_authors(list(set(page_coauthor_ids)))

            new_on_page = 0

            for post in posts:
                if limit is not None and saved >= limit:
                    break

                try:
                    post_url = post.get("link", "")
                    if not post_url:
                        continue

                    # URL deduplication — guard against paginator loops
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)
                    new_on_page += 1

                    post_id = post.get("id")
                    external_id = str(post_id) if post_id else None

                    title = _strip_html((post.get("title") or {}).get("rendered", ""))

                    raw_date = post.get("date", "")
                    published_date = raw_date[:10] if raw_date else ""

                    excerpt_html = (post.get("excerpt") or {}).get("rendered", "")
                    content_html = (post.get("content") or {}).get("rendered", "")

                    # Abstract: post_subheadline (plain text) → excerpt → first paragraph
                    meta = post.get("meta") or {}
                    subheadline = (meta.get("post_subheadline") or "").strip()
                    if subheadline and len(subheadline) >= 80:
                        abstract = subheadline
                    else:
                        abstract = _extract_abstract(excerpt_html, content_html)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {post_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # PDF
                    pdf_url = self._extract_pdf_url(content_html)
                    original_filename = None
                    if pdf_url:
                        original_filename = pdf_url.rstrip("/").split("/")[-1]

                    # Taxonomy terms from _embedded
                    embedded = post.get("_embedded") or {}
                    wp_terms = embedded.get("wp:term") or []
                    author_names_from_terms: list[str] = []
                    categories: list[str] = []
                    tags: list[str] = []
                    region = ""

                    for term_group in wp_terms:
                        for term in (term_group or []):
                            taxonomy = term.get("taxonomy", "")
                            term_name = (term.get("name") or "").strip()
                            if not term_name:
                                continue
                            if taxonomy == "category":
                                categories.append(term_name)
                            elif taxonomy == "post_tag":
                                tags.append(term_name)
                            elif taxonomy == "region":
                                region = term_name

                    # Authors: always use coauthor API (embedded terms return slugs, not display names)
                    coauthor_ids = [
                        cid for cid in (post.get("coauthors") or [])
                        if isinstance(cid, int)
                    ]
                    authors = self._resolve_authors(coauthor_ids) if coauthor_ids else ""

                    category_str = "; ".join(categories) if categories else "News & Commentary"

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": title or f"Post {external_id}",
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "posted_date": published_date,
                        "authors": authors or None,
                        "publisher": _PUBLISHER,
                        "department": region or None,
                        "journal": None,
                        "url": post_url,
                        "pdf_url": pdf_url,
                        "doi": None,
                        "keywords": ", ".join(tags) if tags else None,
                        "category": category_str,
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": published_date,
                                "node_id": external_id,
                                "region": region or None,
                                "categories": categories,
                                "tags": tags,
                                "coauthor_ids": post.get("coauthors") or [],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {post.get('link', '?')} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] Page {page}: no new URLs seen. Done.")
                break

            # Fewer items than per_page means this was the last page
            if len(posts) < _PER_PAGE:
                print(f"[{self.site_id}] Page {page}: last page ({len(posts)} items < {_PER_PAGE}). Done.")
                break

            page += 1
            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
