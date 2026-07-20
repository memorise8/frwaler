# -*- coding: utf-8 -*-
"""Crawler for kjonnsforskning.no — Tidsskrift for kjønnsforskning.

Strategy:
1. Fetch the main journal index page to collect all issue page paths.
2. For each issue page, extract article URLs from the field-contents block.
3. For each article, parse title/authors/abstract/keywords/DOI and save.
"""

import json
import re
import subprocess
import time
from html import unescape

from crawler.base_crawler import BaseCrawler

_SITE_ID = "kjonnsforskning-no-nb"
_BASE_URL = "https://kjonnsforskning.no"
_START_URL = f"{_BASE_URL}/nb/tidsskrift-for-kjonnsforskning"
_JOURNAL = "Tidsskrift for kjønnsforskning"
_PUBLISHER = "Kilden"
_MIN_ABSTRACT = 100  # chars; items below this are skipped


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with exponential-backoff retries. Returns body or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: nb,no;q=0.9,en;q=0.7",
        "-H", (
            "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
            "Gecko/20100101 Firefox/120.0"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=40)
            if result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[{_SITE_ID}] Empty response ({attempt+1}/{retries}), retry in {wait}s: {url}")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[{_SITE_ID}] curl error ({attempt+1}/{retries}): {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts ({url}): {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Build BeautifulSoup with fallback parser chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup  # noqa: PLC0415
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Page-level parsers
# ---------------------------------------------------------------------------

def _get_issue_paths(html: str) -> list[str]:
    """Return unique /nb/tidsskriftet/... paths from the main journal page."""
    paths: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="(/nb/tidsskriftet/[^"#?]+)"', html):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


def _get_article_urls_from_issue(html: str) -> list[str]:
    """Return full article URLs listed in an issue page's field-contents block."""
    urls: list[str] = []
    seen: set[str] = set()

    soup = _make_soup(html)
    if soup is not None:
        block = soup.find(class_="field--name-field-contents")
        if block:
            for a_tag in block.find_all("a", href=True):
                href = a_tag["href"]
                if href.startswith("/nb/") and "/tags/" not in href:
                    full = f"{_BASE_URL}{href}"
                    if full not in seen:
                        seen.add(full)
                        urls.append(full)
            if urls:
                return urls

    # Regex fallback when BS4 fails
    for m in re.finditer(
        r'class="[^"]*journal-byline[^"]*"[^<]{0,800}<a\s+href="(/nb/[^"]+)"',
        html, re.S
    ):
        full = f"{_BASE_URL}{m.group(1)}"
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


def _parse_article(html: str, url: str) -> dict | None:
    """Parse a journal-article detail page. Returns a dict or None on failure."""
    soup = _make_soup(html)
    if soup is None:
        return None

    # Drupal node ID from template comment (most reliable external_id)
    node_id: str | None = None
    m = re.search(r"node--(\d+)--full\.html", html)
    if m:
        node_id = m.group(1)
    if not node_id:
        m = re.search(r"page--node--(\d+)\.html", html)
        if m:
            node_id = m.group(1)

    # Title
    title = ""
    title_el = soup.find(class_="journal-article__title")
    if title_el:
        title = title_el.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og:
            title = (og.get("content") or "").strip()
    if not title:
        return None

    # Authors — <div class="journal-article__byline-text"><p><em>Name, affil og Name2...</em>
    authors = ""
    byline = soup.find(class_="journal-article__byline-text")
    if byline:
        em = byline.find("em")
        authors = (em or byline).get_text(strip=True)
    authors = unescape(authors)

    # Published date from JSON-LD (most reliable)
    published_date = ""
    for ld_tag in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(ld_tag.string or "")
            for node in ld.get("@graph", [ld]):
                dp = node.get("datePublished", "")
                if dp and len(dp) >= 10:
                    published_date = dp[:10]
                    break
        except Exception:
            pass
        if published_date:
            break

    # Issue label, e.g. "1/2025"
    issue = ""
    issue_el = soup.find(class_="journal-article__issue")
    if issue_el:
        issue = issue_el.get_text(separator=" ", strip=True)
        issue = re.sub(r"(?i)utgave\s*:\s*", "", issue).strip()

    # Abstract, keywords, DOI — inside field--name-field-text that contains "Sammendrag"
    abstract = ""
    keywords = ""
    doi = ""
    scup_url = ""

    for field_div in soup.find_all(class_="field--name-field-text"):
        inner = str(field_div)
        if "Sammendrag" not in inner and "abstract" not in inner.lower():
            continue

        # Collect paragraph text after the <h2>Sammendrag</h2> heading
        h2 = field_div.find("h2")
        if h2:
            parts = []
            for sib in h2.next_siblings:
                sib_str = sib.get_text(separator=" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if not sib_str:
                    continue
                if re.search(r"(Nøkkelord|Les hele artikkelen|DOI\s*:)", sib_str, re.I):
                    break
                parts.append(sib_str)
            abstract = " ".join(parts)

        # Fallback: strip heading and keyword lines from full text
        if not abstract:
            raw = field_div.get_text(separator=" ", strip=True)
            raw = re.sub(r"(?i)^sammendrag\s*", "", raw).strip()
            abstract = re.sub(r"(?i)nøkkelord.*", "", raw, flags=re.S).strip()

        # Keywords (after <strong>Nøkkelord:</strong>)
        kw_m = re.search(
            r"(?i)Nøkkelord\s*:</strong>\s*(.*?)(?:<(?:p|hr|/div)|Les hele|DOI)",
            inner, re.S
        )
        if kw_m:
            keywords = _strip_html(kw_m.group(1)).strip().rstrip(".")

        # DOI / scup.com link
        for a_tag in field_div.find_all("a", href=True):
            href = a_tag["href"]
            if "scup.com/doi/" in href or "doi.org/" in href:
                doi_m = re.search(r"(10\.\d{4,}/[^\s\"'<>]+)", href)
                if doi_m:
                    doi = doi_m.group(1).rstrip(".")
                scup_url = href
                break

        break  # only process the first matching field

    # Tags → category
    tags = []
    for tag_div in soup.find_all(class_="article-tag"):
        tag_text = re.sub(r"(?i)^mer om\s*", "", tag_div.get_text(strip=True)).strip()
        if tag_text and "Tidsskrift" not in tag_text:
            tags.append(tag_text)
    category = "; ".join(tags)

    return {
        "node_id": node_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "keywords": keywords,
        "doi": doi,
        "scup_url": scup_url,
        "published_date": published_date,
        "issue": issue,
        "category": category,
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class KjonnsforskningSiteCrawler(BaseCrawler):
    """Crawler for kjonnsforskning.no — Tidsskrift for kjønnsforskning."""

    site_id = "kjonnsforskning-no-nb"
    site_name = "Custom: kjonnsforskning-no-nb"
    base_url = "https://kjonnsforskning.no"

    def crawl(self, limit=None):
        """Crawl journal articles and save to DB.

        Walks issue pages from the main journal index, then fetches each
        article. Stops when ``limit`` items are saved, all articles are
        exhausted, or the 25-minute wall-clock budget is reached.
        """
        saved = 0
        seen_issue: set[str] = set()
        seen_article: set[str] = set()
        start_time = time.time()
        MAX_WALL_MIN = 25

        # ── Step 1: collect issue page paths ─────────────────────────────
        print(f"[{self.site_id}] Fetching main page: {_START_URL}")
        main_html = _curl_get(_START_URL)
        if not main_html:
            print(f"[{self.site_id}] Cannot fetch main page. Abort.")
            return 0

        issue_paths = _get_issue_paths(main_html)
        if not issue_paths:
            print(f"[{self.site_id}] No issue pages found. Abort.")
            return 0
        print(f"[{self.site_id}] Found {len(issue_paths)} issue pages")

        # ── Step 2: collect article URLs from each issue page ─────────────
        all_article_urls: list[str] = []
        for page_num, issue_path in enumerate(issue_paths, 1):
            if page_num > 200:
                print(f"[{self.site_id}] Safety cap of 200 issue pages reached.")
                break
            if limit is not None and len(all_article_urls) >= limit * 8:
                break  # have plenty; no need to keep fetching issue pages

            issue_url = f"{_BASE_URL}{issue_path}"
            if issue_url in seen_issue:
                continue
            seen_issue.add(issue_url)

            time.sleep(0.5)
            issue_html = _curl_get(issue_url)
            if not issue_html:
                print(f"[{self.site_id}] Failed to fetch issue: {issue_url}")
                continue

            art_urls = _get_article_urls_from_issue(issue_html)
            new_count = 0
            for au in art_urls:
                if au not in seen_article:
                    seen_article.add(au)
                    all_article_urls.append(au)
                    new_count += 1

            if page_num % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(
                    f"[{self.site_id}] page {page_num}: "
                    f"saved {saved}/{lim_str}, "
                    f"{len(all_article_urls)} articles queued"
                )

        print(f"[{self.site_id}] Total unique article URLs: {len(all_article_urls)}")

        # ── Step 3: fetch + parse + save each article ─────────────────────
        seen_detail: set[str] = set()
        for idx, article_url in enumerate(all_article_urls):
            if limit is not None and saved >= limit:
                break

            if (time.time() - start_time) / 60 >= MAX_WALL_MIN:
                print(f"[{self.site_id}] Wall-clock budget ({MAX_WALL_MIN}min) reached. Stopping.")
                break

            if article_url in seen_detail:
                continue
            seen_detail.add(article_url)

            try:
                time.sleep(self._delay)
                html = _curl_get(article_url)
                if not html:
                    print(f"[{self.site_id}] Failed to fetch: {article_url}")
                    continue

                art = _parse_article(html, article_url)
                if not art:
                    print(f"[{self.site_id}] Parse failed: {article_url}")
                    continue

                abstract = (art.get("abstract") or "").strip()
                if len(abstract) < _MIN_ABSTRACT:
                    print(
                        f"[{self.site_id}] Abstract too short ({len(abstract)} chars), "
                        f"skipping: {article_url}"
                    )
                    continue

                node_id = art.get("node_id") or ""
                external_id = node_id if node_id else article_url

                paper = {
                    "id": None,
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": node_id or None,
                    "title": art["title"],
                    "abstract": abstract,
                    "authors": art["authors"],
                    "department": _PUBLISHER,     # adapter: department → publisher
                    "published_date": art.get("published_date") or "",
                    "listed_date": art.get("published_date") or "",
                    "url": article_url,           # adapter: url → meta_url
                    "pdf_url": None,
                    "doi": art.get("doi") or "",
                    "keywords": art.get("keywords") or "",
                    "category": art.get("category") or "",
                    "original_filename": None,
                    "metadata": json.dumps({
                        "journal": _JOURNAL,      # adapter extracts → journal column
                        "journal_raw": _JOURNAL,
                        "issue": art.get("issue") or "",
                        "scup_url": art.get("scup_url") or "",
                        "node_id": node_id,
                        "posted_date": art.get("published_date") or "",
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] Saved {saved}/{lim_str}: {art['title'][:60]}")

                if (idx + 1) % 10 == 0:
                    print(
                        f"[{self.site_id}] page {idx+1}: "
                        f"saved {saved}/{lim_str}"
                    )

            except KeyboardInterrupt:
                print(f"[{self.site_id}] Interrupted at {saved} saved.")
                raise
            except Exception as exc:
                print(f"[{self.site_id}] Item {idx+1} failed ({article_url}): {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
