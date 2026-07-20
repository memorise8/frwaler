# -*- coding: utf-8 -*-
"""Crawler for ejournals.epublishing.ekt.gr (Open Journal Systems / EKT ePublishing).

Strategy:
  1. Fetch the journals catalog to get all journal slugs (93 journals).
  2. For each journal, walk the issue archive to get all issue URLs.
  3. For each issue page, collect article URLs.
  4. For each article page, parse <meta name="citation_*"> tags for metadata.
"""

import json
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "ejournals-epublishing-ekt-gr-indexphp"
_BASE = "https://ejournals.epublishing.ekt.gr"
_CATALOG_URL = f"{_BASE}/index.php/index/journals-catalog"

# Wall-clock budget (seconds) and page safety cap
_MAX_WALL = 25 * 60
_PAGE_CAP = 200


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with TLS-max 1.3, exponential backoff on failure."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: el,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error ({url}): {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts ({url}): {exc}")
    return None


def _get_journal_slugs() -> list[str]:
    """Return unique journal slugs from the catalog page."""
    raw = _curl_get(_CATALOG_URL)
    if not raw:
        return []
    found = re.findall(
        r'href="https://ejournals\.epublishing\.ekt\.gr/index\.php/([a-zA-Z0-9_\-]+)'
        r'(?:/issue/current)?"',
        raw,
    )
    seen: set[str] = set()
    result: list[str] = []
    for s in found:
        if s not in seen and s != "index":
            seen.add(s)
            result.append(s)
    return result


def _get_issue_urls(slug: str) -> list[str]:
    """Return all issue-view URLs for a journal slug (handles pagination)."""
    issue_urls: list[str] = []
    seen: set[str] = set()
    page = 1
    while True:
        url = (
            f"{_BASE}/index.php/{slug}/issue/archive"
            if page == 1
            else f"{_BASE}/index.php/{slug}/issue/archive?page={page}"
        )
        raw = _curl_get(url)
        if not raw:
            break
        found = re.findall(
            rf'href="(https://ejournals\.epublishing\.ekt\.gr'
            rf'/index\.php/{re.escape(slug)}/issue/view/(\d+))"',
            raw,
        )
        new = 0
        for full_url, _ in found:
            if full_url not in seen:
                seen.add(full_url)
                issue_urls.append(full_url)
                new += 1
        if new == 0:
            break
        # Only continue paging if a next-page link exists
        if f"page={page + 1}" not in raw and f"page%3D{page + 1}" not in raw:
            break
        page += 1
    return issue_urls


def _get_article_urls(issue_url: str) -> list[str]:
    """Return article-view URLs listed on an issue page."""
    m = re.match(r".*/index\.php/([^/]+)/issue/", issue_url)
    if not m:
        return []
    slug = m.group(1)
    raw = _curl_get(issue_url)
    if not raw:
        return []
    found = re.findall(
        rf'href="(https://ejournals\.epublishing\.ekt\.gr'
        rf'/index\.php/{re.escape(slug)}/article/view/(\d+))"',
        raw,
    )
    seen: set[str] = set()
    result: list[str] = []
    for full_url, _ in found:
        if full_url not in seen:
            seen.add(full_url)
            result.append(full_url)
    return result


def _parse_article(url: str, html: str) -> dict | None:
    """Parse an article page and return a paper dict, or None on failure."""
    soup = _make_soup(html)
    if not soup:
        # Fallback: raw regex on html
        soup = None

    def get_meta(name: str) -> str:
        if soup:
            tag = soup.find("meta", attrs={"name": name})
            return (tag.get("content") or "").strip() if tag else ""
        # regex fallback
        m = re.search(
            rf'<meta\s+name="{re.escape(name)}"\s+(?:xml:lang="[^"]*"\s+)?content="([^"]*)"',
            html,
        )
        return m.group(1).strip() if m else ""

    def get_all_meta(name: str) -> list[str]:
        if soup:
            tags = soup.find_all("meta", attrs={"name": name})
            return [(t.get("content") or "").strip() for t in tags if t.get("content")]
        return re.findall(
            rf'<meta\s+name="{re.escape(name)}"\s+(?:xml:lang="[^"]*"\s+)?content="([^"]+)"',
            html,
        )

    title = get_meta("citation_title") or get_meta("DC.Title")
    if not title:
        return None

    # Authors: multiple citation_author tags → "; " joined
    authors_list = get_all_meta("citation_author") or get_all_meta("DC.Creator.PersonalName")
    authors_str = "; ".join(a for a in authors_list if a)

    # Abstract: article-abstract div (HTML stripped), fallback to DC.Description
    abstract = ""
    if soup:
        abs_div = soup.find(class_="article-abstract")
        if abs_div:
            abstract = re.sub(r"\s+", " ", abs_div.get_text(separator=" ")).strip()
    if len(abstract) < 20:
        descs = get_all_meta("DC.Description")
        if descs:
            abstract = max(descs, key=len)

    # Published date: "YYYY/MM/DD" or "YYYY" → ISO
    raw_date = get_meta("citation_date")
    published_date = ""
    if raw_date:
        md = re.match(r"(\d{4})[/\-](\d{2})[/\-](\d{2})", raw_date)
        if md:
            published_date = f"{md.group(1)}-{md.group(2)}-{md.group(3)}"
        elif re.match(r"^\d{4}$", raw_date.strip()):
            published_date = f"{raw_date.strip()}-01-01"

    doi = get_meta("citation_doi") or get_meta("DC.Identifier.DOI")
    pdf_url = get_meta("citation_pdf_url") or None

    # Original filename from PDF URL last path segment
    original_filename = None
    if pdf_url:
        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
        if tail:
            original_filename = tail

    # Article numeric ID
    article_id = ""
    m_url = re.search(r"/article/view/(\d+)", url)
    if m_url:
        article_id = m_url.group(1)
    if not article_id:
        article_id = get_meta("DC.Identifier")

    journal = get_meta("citation_journal_title") or get_meta("DC.Source")

    # Publisher from DC.Rights "Copyright (c) YYYY Publisher Name"
    publisher = ""
    rights = get_meta("DC.Rights")
    if rights:
        mp = re.search(r"Copyright\s*\([^)]*\)\s*\d*\s*(.*)", rights)
        if mp:
            publisher = mp.group(1).strip()

    # Keywords from keyword section in page
    keywords = ""
    if soup:
        kw_sec = soup.find(class_=re.compile(r"keyword", re.I))
        if kw_sec:
            kw_text = re.sub(r"\s+", " ", kw_sec.get_text(separator=", ")).strip()
            kw_text = re.sub(r"^Keywords?[:\s]*", "", kw_text, flags=re.I).strip()
            keywords = kw_text

    volume = get_meta("citation_volume")
    issue_num = get_meta("citation_issue")
    firstpage = get_meta("citation_firstpage")
    lastpage = get_meta("citation_lastpage")
    issn = get_meta("citation_issn") or get_meta("DC.Source.ISSN")
    language = get_meta("citation_language")
    article_type = get_meta("DC.Type.articleType")
    date_submitted = get_meta("DC.Date.dateSubmitted")

    metadata_dict: dict = {
        "posted_date": raw_date or None,
        "journal_raw": journal or None,
        "volume": volume or None,
        "issue": issue_num or None,
        "series": None,
        "firstpage": firstpage or None,
        "lastpage": lastpage or None,
        "issn": issn or None,
        "language": language or None,
        "article_type": article_type or None,
        "date_submitted": date_submitted or None,
        "originalFilename": original_filename,
        "doi": doi or None,
    }
    metadata_dict = {k: v for k, v in metadata_dict.items() if v is not None}

    return {
        "id": None,
        "site_id": _SITE_ID,
        "external_id": article_id or None,
        "post_number": article_id or None,
        "title": title,
        "abstract": abstract,
        "published_date": published_date or None,
        "listed_date": published_date or None,
        "posted_date": raw_date or None,
        "authors": authors_str or None,
        "publisher": publisher or None,
        "journal": journal or None,
        "url": url,
        "pdf_url": pdf_url,
        "doi": doi or None,
        "keywords": keywords or None,
        "category": article_type or None,
        "original_filename": original_filename,
        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
    }


class EJournalsEKTCrawler(BaseCrawler):
    """Crawler for ejournals.epublishing.ekt.gr (OJS / EKT ePublishing)."""

    site_id = _SITE_ID
    site_name = "Custom: ejournals-epublishing-ekt-gr-indexphp"
    base_url = _BASE

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"
        page_count = 0

        print(f"[{_SITE_ID}] Fetching journal catalog...")
        journal_slugs = _get_journal_slugs()
        if not journal_slugs:
            print(f"[{_SITE_ID}] No journals found — aborting.")
            return 0
        print(f"[{_SITE_ID}] Found {len(journal_slugs)} journals")

        for slug in journal_slugs:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _MAX_WALL:
                print(f"[{_SITE_ID}] Wall-clock limit ({_MAX_WALL}s) reached, stopping")
                break
            if page_count >= _PAGE_CAP:
                print(f"[{_SITE_ID}] Safety page cap ({_PAGE_CAP}) reached, stopping")
                break

            try:
                issue_urls = _get_issue_urls(slug)
            except Exception as exc:
                print(f"[{_SITE_ID}] Failed to get issues for {slug}: {exc}")
                continue

            for issue_url in issue_urls:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL:
                    break
                if page_count >= _PAGE_CAP:
                    break

                try:
                    article_urls = _get_article_urls(issue_url)
                except Exception as exc:
                    print(f"[{_SITE_ID}] Failed to get articles for {issue_url}: {exc}")
                    continue

                page_count += 1
                if page_count % 10 == 0:
                    print(f"[{_SITE_ID}] page {page_count}: saved {saved}/{limit_str}")

                if not article_urls:
                    continue

                for article_url in article_urls:
                    if limit is not None and saved >= limit:
                        break
                    if article_url in seen_urls:
                        continue
                    seen_urls.add(article_url)

                    try:
                        time.sleep(self._delay)
                        html = _curl_get(article_url)
                        if not html:
                            print(f"[{_SITE_ID}] fetch failed: {article_url}")
                            continue

                        paper = _parse_article(article_url, html)
                        if not paper:
                            print(f"[{_SITE_ID}] parse failed: {article_url}")
                            continue

                        abstract = paper.get("abstract") or ""
                        if len(abstract) < 50:
                            print(
                                f"[{_SITE_ID}] skip short abstract "
                                f"({len(abstract)} chars): {article_url}"
                            )
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{_SITE_ID}] saved {saved}/{limit_str}: "
                            f"{paper['title'][:60]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{_SITE_ID}] item {article_url} failed: {exc}")
                        continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
