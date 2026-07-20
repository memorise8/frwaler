# -*- coding: utf-8 -*-
"""Crawler for Frontiers in — articles listing at frontiersin.org/articles.

Listing:  GET https://www.frontiersin.org/articles?page=N
          24 items/page, JSON-LD ItemList embedded in HTML.
Detail:   GET /articles/{doi}  (redirects to /journals/{journal}/articles/{doi}/full)
          JSON-LD ScholarlyArticle + HTML for abstract/keywords.
"""

import json
import os
import re
import subprocess
import sys
import time

# Ensure the project root is on sys.path so absolute imports work when this
# file is loaded via importlib.util.spec_from_file_location with no package.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


# ---------------------------------------------------------------------------
# BeautifulSoup parser fallback chain
# ---------------------------------------------------------------------------

_BS_PARSER_CACHE = [None]


def _make_soup(html):
    from bs4 import BeautifulSoup

    parsers = ["html5lib", "lxml", "html.parser"]
    cached = _BS_PARSER_CACHE[0]
    if cached:
        parsers = [cached] + [p for p in parsers if p != cached]
    for parser in parsers:
        try:
            soup = BeautifulSoup(html, parser)
            _BS_PARSER_CACHE[0] = parser
            return soup
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


class FrontiersOrgArticlesCrawler(BaseCrawler):
    site_id = "frontiersin-org-articles"
    site_name = "Custom: frontiersin-org-articles"
    base_url = "https://www.frontiersin.org"

    _LIST_URL = "https://www.frontiersin.org/articles"
    _ITEMS_PER_PAGE = 24
    _MAX_PAGES = 200
    _MAX_SECONDS = 25 * 60

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch URL via curl (TLS-tolerant, follow redirects).

        Returns (final_url, body_str).  On total failure returns ("", "").
        """
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-skL",
                        "-w", "\n__FINAL_URL__:%{url_effective}",
                        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.9",
                        url,
                    ],
                    capture_output=True,
                    timeout=45,
                )
                raw = result.stdout
                marker = b"\n__FINAL_URL__:"
                if marker in raw:
                    body_bytes, url_bytes = raw.rsplit(marker, 1)
                    final_url = url_bytes.decode("utf-8", errors="replace").strip()
                else:
                    body_bytes = raw
                    final_url = url
                body = body_bytes.decode("utf-8", errors="replace")
                if body.strip():
                    return final_url, body
            except Exception as exc:
                print(f"[frontiersin-org-articles] curl error (attempt {attempt+1}/{retries}) {url}: {exc}")
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1s, 3s, 9s
                time.sleep(wait)
        print(f"[frontiersin-org-articles] gave up after {retries} attempts: {url}")
        return "", ""

    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        """Return list of (title, url) from the article listing page N."""
        url = f"{self._LIST_URL}?page={page}"
        _, body = self._curl_get(url)
        if not body:
            return []

        items = []
        try:
            for jld_str in re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                body, re.DOTALL
            ):
                try:
                    data = json.loads(jld_str.strip())
                    if data.get("@type") == "ItemList":
                        for entry in data.get("itemListElement", []):
                            it = entry.get("item", {})
                            title = it.get("name", "")
                            art_url = it.get("url") or it.get("@id") or ""
                            if art_url:
                                items.append((title, art_url))
                        break
                except (json.JSONDecodeError, Exception):
                    continue
        except Exception as exc:
            print(f"[frontiersin-org-articles] list parse error page {page}: {exc}")

        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _parse_abstract(self, body):
        """Extract abstract text from article detail HTML."""
        # Method 1: text following "Abstract</hN>" heading
        m = re.search(
            r'Abstract</h\d[^>]*>(.*?)</(?:div|section|article)',
            body, re.DOTALL
        )
        if m:
            text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if len(text) >= 50:
                return text

        # Method 2: BeautifulSoup class search
        try:
            soup = _make_soup(body)
            if soup:
                for elem in soup.find_all(class_=re.compile(r'abstract', re.I)):
                    text = elem.get_text(separator=" ", strip=True)
                    if len(text) >= 50:
                        return text
        except Exception:
            pass

        # Method 3: meta description
        m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]{80,})"', body)
        if m:
            return m.group(1)
        m = re.search(r'<meta[^>]+content="([^"]{80,})"[^>]+name="description"', body)
        if m:
            return m.group(1)

        return ""

    def _parse_keywords(self, body):
        """Extract comma-joined keywords string from article detail HTML."""
        kw_list = []
        try:
            for kw_raw in re.findall(
                r'class="[^"]*keyword[^"]*"[^>]*>(.*?)</[a-zA-Z]+>',
                body, re.DOTALL | re.I
            ):
                clean = re.sub(r'<[^>]+>', '', kw_raw).strip()
                if clean and len(clean) < 200:
                    kw_list.append(clean)
        except Exception:
            pass
        return ",".join(kw_list) if kw_list else None

    def _fetch_article(self, article_url):
        """Fetch one article detail page; return dict or None on failure."""
        final_url, body = self._curl_get(article_url)
        if not body or len(body) < 200:
            return None

        result = {"_final_url": final_url, "_body_len": len(body)}

        # Derive PDF URL from redirect URL
        if "/full" in final_url:
            result["url"] = final_url
            result["pdf_url"] = final_url.replace("/full", "/pdf")
        else:
            result["url"] = final_url
            result["pdf_url"] = None

        # JSON-LD ScholarlyArticle
        try:
            for jld_str in re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                body, re.DOTALL
            ):
                try:
                    data = json.loads(jld_str.strip())
                    if data.get("@type") != "ScholarlyArticle":
                        continue

                    result["title"] = data.get("headline", "")
                    result["published_date"] = data.get("datePublished", "")

                    # Authors (;-separated)
                    authors = []
                    for a in data.get("author", []):
                        if isinstance(a, dict):
                            name = a.get("name", "")
                            if name:
                                authors.append(name)
                    result["authors"] = ";".join(authors) if authors else None

                    result["publisher"] = "Frontiers"

                    # Navigate isPartOf chain: PublicationIssue → PublicationVolume → Periodical
                    journal_name = None
                    volume = None
                    issue = None
                    level = data.get("isPartOf")
                    while level and isinstance(level, dict):
                        t = level.get("@type", "")
                        if t == "Periodical":
                            journal_name = level.get("name")
                        elif t == "PublicationVolume":
                            volume = level.get("volumeNumber")
                        elif t == "PublicationIssue":
                            issue = level.get("issueNumber")
                        level = level.get("isPartOf")
                    result["journal"] = journal_name

                    # DOI from final URL
                    doi_m = re.search(r'(10\.\d{4,}/[^\s"<>/]+)', final_url)
                    if doi_m:
                        doi = doi_m.group(1).rstrip(".")
                        result["doi"] = doi
                        result["external_id"] = doi
                        # post_number: last numeric segment of DOI
                        parts = doi.split(".")
                        result["post_number"] = parts[-1] if parts else None

                    result["metadata"] = json.dumps({
                        "volume": volume,
                        "issue": issue,
                        "journal_raw": journal_name,
                        "dateModified": data.get("dateModified"),
                        "inLanguage": data.get("inLanguage"),
                    }, ensure_ascii=False)
                    break
                except (json.JSONDecodeError, Exception):
                    continue
        except Exception as exc:
            print(f"[frontiersin-org-articles] JSON-LD parse error {article_url}: {exc}")

        # Abstract
        try:
            result["abstract"] = self._parse_abstract(body)
        except Exception as exc:
            print(f"[frontiersin-org-articles] abstract parse error {article_url}: {exc}")
            result["abstract"] = ""

        # Keywords
        try:
            result["keywords"] = self._parse_keywords(body)
        except Exception as exc:
            print(f"[frontiersin-org-articles] keyword parse error {article_url}: {exc}")

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        page = 1

        while saved < limit_or_inf and page <= self._MAX_PAGES:
            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._MAX_SECONDS:
                print(f"[frontiersin-org-articles] wall-clock budget ({self._MAX_SECONDS}s) reached after {page-1} pages")
                break

            if page == self._MAX_PAGES:
                print(f"[frontiersin-org-articles] safety cap of {self._MAX_PAGES} pages reached")

            # Fetch listing
            try:
                items = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[frontiersin-org-articles] list page {page} error: {exc}")
                break

            if not items:
                print(f"[frontiersin-org-articles] page {page}: no items, stopping")
                break

            new_items = [(t, u) for t, u in items if u not in seen_urls]
            if not new_items:
                print(f"[frontiersin-org-articles] page {page}: all {len(items)} items already seen, stopping")
                break

            # Process each article
            for title, art_url in new_items:
                if saved >= limit_or_inf:
                    break
                if art_url in seen_urls:
                    continue
                seen_urls.add(art_url)

                try:
                    article = self._fetch_article(art_url)
                    if not article:
                        print(f"[frontiersin-org-articles] item {art_url}: empty response, skipping")
                        continue

                    # Fallback title from listing
                    if not article.get("title") and title:
                        article["title"] = title

                    if not article.get("title"):
                        print(f"[frontiersin-org-articles] item {art_url}: no title, skipping")
                        continue

                    abstract = article.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[frontiersin-org-articles] item {art_url}: "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue

                    doi = article.get("doi", "")
                    paper = {
                        "site_id": self.site_id,
                        "external_id": doi or art_url,
                        "post_number": article.get("post_number"),
                        "title": article["title"],
                        "abstract": abstract,
                        "published_date": article.get("published_date"),
                        "listed_date": article.get("published_date"),
                        "authors": article.get("authors"),
                        "publisher": article.get("publisher", "Frontiers"),
                        "journal": article.get("journal"),
                        "url": article.get("url", art_url),
                        "pdf_url": article.get("pdf_url"),
                        "keywords": article.get("keywords"),
                        "doi": doi,
                        "metadata": article.get("metadata"),
                        "original_filename": None,
                    }

                    self._save_paper(paper)
                    saved += 1

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[frontiersin-org-articles] item {art_url} failed: {exc}")
                    continue

            if page % 10 == 0:
                lbl = str(limit) if limit is not None else "inf"
                print(f"[frontiersin-org-articles] page {page}: saved {saved}/{lbl}")

            page += 1
            time.sleep(0.3)

        print(f"[frontiersin-org-articles] done: saved {saved} items in {page-1} pages")
        return saved
