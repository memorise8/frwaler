# -*- coding: utf-8 -*-
"""ALRC final reports crawler.

Starting URL: https://www.alrc.gov.au/publications/final-report/

The ALRC site is WordPress, but its custom ``publication`` post type is not
publicly exposed through ``/wp-json/wp/v2``. The real listing is rendered as
HTML at ``/publications/final-report/`` and paginated with ``/page/N/`` URLs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

# Absolute import is required because spec_from_file_location has no package
# context when these custom crawlers are loaded by tests/tools.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


SITE_ID = "alrc-gov-au-publications"
BASE_URL = "https://www.alrc.gov.au"
START_URL = f"{BASE_URL}/publications/final-report/"
PUBLISHER = "Australian Law Reform Commission"
SAFETY_PAGE_CAP = 200
MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
ABSTRACT_MIN_CHARS = 50
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL with curl and retry 1s, 3s, 9s before giving up."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-AU,en;q=0.9",
        url,
    ]
    waits = (1, 3, 9)
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(waits[min(attempt - 1, len(waits) - 1)])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout + 5,
                check=False,
            )
            raw = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and raw.strip():
                return raw
            print(
                f"[{SITE_ID}] fetch failed for {url}: "
                f"exit={result.returncode}, attempt {attempt + 1}/{retries}"
            )
        except Exception as exc:
            print(
                f"[{SITE_ID}] curl error for {url}: {exc}, "
                f"attempt {attempt + 1}/{retries}"
            )
    return None


def _make_soup(raw: str):
    """Parse HTML with html5lib first, then lxml, then html.parser."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[{SITE_ID}] BeautifulSoup {parser} failed: {exc}")
            continue
    return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_date(raw: str | None) -> str | None:
    """Normalize common ALRC/WordPress dates to YYYY-MM-DD when possible."""
    if not raw:
        return None
    text = _clean_text(raw)
    if not text:
        return None

    match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)

    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    for fmt in (
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _date_from_text(text: str) -> str | None:
    """Find a readable date inside prose such as 'on 6th March 2025'."""
    if not text:
        return None
    match = re.search(
        r"\b(\d{1,2}(?:st|nd|rd|th)?\s+"
        r"(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|"
        r"Jul|July|Aug|August|Sep|September|Oct|October|Nov|November|"
        r"Dec|December)\s+\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    return _parse_date(match.group(1)) if match else None


def _list_page_url(page: int) -> str:
    if page <= 1:
        return START_URL
    return f"{START_URL}page/{page}/"


def _slug_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if tail and "." in tail and len(tail) <= 240:
        return tail
    return None


def _extract_wp_post_id(html: str, soup) -> str | None:
    """Extract native WordPress post ID from shortlink or body class."""
    patterns = (
        r"rel=['\"]shortlink['\"][^>]+href=['\"]https?://www\.alrc\.gov\.au/\?p=(\d+)",
        r"href=['\"]https?://www\.alrc\.gov\.au/\?p=(\d+)['\"][^>]+rel=['\"]shortlink['\"]",
        r"\bpostid-(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    if soup and soup.body:
        classes = soup.body.get("class") or []
        for cls in classes:
            match = re.match(r"postid-(\d+)", str(cls))
            if match:
                return match.group(1)
    return None


def _json_ld_graph(soup) -> list[dict]:
    records: list[dict] = []
    if not soup:
        return records
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except Exception:
            continue
        if isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                records.extend(x for x in graph if isinstance(x, dict))
            else:
                records.append(data)
        elif isinstance(data, list):
            records.extend(x for x in data if isinstance(x, dict))
    return records


def _meta_content(soup, *names: str) -> str:
    if not soup:
        return ""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return _clean_text(tag.get("content"))
    return ""


def _parse_list_page(html: str) -> tuple[list[dict], bool]:
    """Return list-page stubs and whether a next-page link exists."""
    soup = _make_soup(html)
    if not soup:
        return [], False

    stubs: list[dict] = []
    for row in soup.select(".section-listing-item"):
        try:
            link = row.select_one("h2 a[href]") or row.select_one("a[href]")
            if not link:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(BASE_URL, href)
            title = _clean_text(link.get_text(" ", strip=True)) or _clean_text(
                link.get("title")
            )
            if not title:
                continue

            date_el = row.select_one(".section-listing-item-date")
            listed_raw = _clean_text(date_el.get_text(" ", strip=True) if date_el else "")

            paragraphs = [
                _clean_text(p.get_text(" ", strip=True))
                for p in row.find_all("p")
                if _clean_text(p.get_text(" ", strip=True))
            ]
            snippet = max(paragraphs, key=len) if paragraphs else ""

            stubs.append(
                {
                    "url": url,
                    "title": title,
                    "slug": _slug_from_url(url),
                    "listed_date_raw": listed_raw,
                    "listed_date": _parse_date(listed_raw),
                    "snippet": snippet,
                    "source": "html.section-listing-item",
                }
            )
        except Exception as exc:
            print(f"[{SITE_ID}] list item parse failed: {exc}")
            continue

    has_next = soup.select_one(".tw-pagination a.next[href]") is not None
    return stubs, has_next


def _best_pdf_link(soup) -> str | None:
    """Prefer the final report PDF, then the explicit Download PDF widget."""
    if not soup:
        return None

    pdf_links: list[tuple[int, str]] = []
    for index, link in enumerate(soup.find_all("a", href=True)):
        href = link.get("href") or ""
        if ".pdf" not in href.lower():
            continue
        text = _clean_text(" ".join([link.get_text(" ", strip=True), link.get("title") or ""]))
        score = 0
        low = text.lower()
        if "final report" in low:
            score += 100
        if "download pdf" in low:
            score += 50
        if "summary" in low or "appendix" in low or "appendices" in low:
            score -= 40
        pdf_links.append((score - index, urljoin(BASE_URL, href)))

    if not pdf_links:
        return None
    pdf_links.sort(reverse=True)
    return pdf_links[0][1]


def _content_root(soup):
    if not soup:
        return None
    return (
        soup.select_one("#main-content main.content")
        or soup.select_one("#main-content")
        or soup.find("main")
        or soup.find("article")
        or soup.body
    )


def _extract_abstract(soup, fallback: str = "") -> str:
    root = _content_root(soup)
    parts: list[str] = []
    if root:
        for noisy in root.find_all(["script", "style", "nav", "form", "aside", "footer"]):
            try:
                noisy.decompose()
            except Exception:
                pass
        for tag in root.find_all(["p", "li"]):
            text = _clean_text(tag.get_text(" ", strip=True))
            if not text:
                continue
            low = text.lower()
            if low.startswith("download ") or low in {"download pdf"}:
                continue
            if low.startswith("filed under:"):
                continue
            if "quick exit" in low or "stay informed" in low:
                continue
            if len(text) >= 30:
                parts.append(text)

    abstract = _clean_text(" ".join(parts))
    if fallback and len(fallback) > len(abstract):
        abstract = _clean_text(fallback)
    return abstract


def _parse_detail_page(html: str, url: str, stub: dict) -> dict:
    soup = _make_soup(html)
    if not soup:
        clean = _clean_text(re.sub(r"<[^>]+>", " ", html))
        return {
            "title": stub.get("title") or "",
            "abstract": clean or stub.get("snippet") or "",
            "published_date": stub.get("listed_date"),
            "authors": "",
            "wp_post_id": None,
            "pdf_url": None,
            "original_filename": None,
            "category": "Final Report",
            "keywords": "",
            "metadata": {"detail_parser": "regex-fallback"},
        }

    graph = _json_ld_graph(soup)
    article = next((x for x in graph if x.get("@type") == "Article"), {})
    webpage = next((x for x in graph if x.get("@type") == "WebPage"), {})

    h1 = soup.select_one("h1.post-title") or soup.find("h1")
    title = (
        _clean_text(h1.get_text(" ", strip=True) if h1 else "")
        or _clean_text(article.get("headline"))
        or _meta_content(soup, "og:title", "twitter:title")
        or stub.get("title")
        or ""
    )

    og_description = _meta_content(soup, "og:description", "description")
    fallback_abstract = stub.get("snippet") or og_description
    abstract = _extract_abstract(soup, fallback=fallback_abstract)
    if not abstract and og_description:
        abstract = og_description

    pdf_url = _best_pdf_link(soup)
    original_filename = _filename_from_url(pdf_url)
    wp_post_id = _extract_wp_post_id(html, soup)

    published_date = (
        _parse_date(article.get("datePublished"))
        or _parse_date(webpage.get("datePublished"))
        or stub.get("listed_date")
    )
    modified_date = _parse_date(article.get("dateModified")) or _parse_date(
        webpage.get("dateModified")
    )

    tabled_date = None
    if abstract and "tabled" in abstract.lower():
        tabled_date = _date_from_text(abstract)

    author = article.get("author")
    page_authors = ""
    if isinstance(author, dict):
        page_authors = _clean_text(author.get("name"))
    elif isinstance(author, list):
        page_authors = "; ".join(
            _clean_text(a.get("name")) for a in author if isinstance(a, dict) and a.get("name")
        )

    metadata = {
        "posted_date": stub.get("listed_date_raw"),
        "listed_date": stub.get("listed_date"),
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "wp_post_id": wp_post_id,
        "node_id": wp_post_id,
        "slug": stub.get("slug") or _slug_from_url(url),
        "source_list_url": START_URL,
        "list_source": stub.get("source"),
        "list_snippet": stub.get("snippet"),
        "date_published_raw": article.get("datePublished") or webpage.get("datePublished"),
        "date_modified_raw": article.get("dateModified") or webpage.get("dateModified"),
        "date_modified": modified_date,
        "tabled_date": tabled_date,
        "og_description": og_description,
        "page_authors": page_authors,
        "json_ld_article": article,
    }

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "authors": "",
        "wp_post_id": wp_post_id,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "category": "Final Report",
        "keywords": "",
        "metadata": metadata,
    }


class ALRCGovAuPublicationsCrawler(BaseCrawler):
    """Crawler for ALRC final-report publications."""

    site_id = "alrc-gov-au-publications"
    site_name = "Custom: alrc-gov-au-publications"
    base_url = "https://www.alrc.gov.au"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"
        page = 0

        for page in range(1, SAFETY_PAGE_CAP + 1):
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - started_at >= MAX_WALL_SECONDS - 30:
                print(f"[{SITE_ID}] approaching 25-minute wall-clock budget; stopping")
                break

            if page % 10 == 0:
                print(f"[{SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = _list_page_url(page)
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{SITE_ID}] page {page}: fetch failed; stopping")
                break

            items, has_next = _parse_list_page(raw)
            if not items:
                print(f"[{SITE_ID}] page {page}: no records; stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                item_url = item.get("url")
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(getattr(self, "_delay", 1.0))
                    detail_html = _curl_get(item_url)
                    if not detail_html:
                        print(f"[{SITE_ID}] item {item_url} failed: empty detail response")
                        continue

                    detail = _parse_detail_page(detail_html, item_url, item)
                    title = detail.get("title") or item.get("title") or ""
                    abstract = _clean_text(detail.get("abstract") or item.get("snippet") or "")
                    if len(abstract) < ABSTRACT_MIN_CHARS:
                        print(f"[{SITE_ID}] skipping {item_url}: abstract <50 chars")
                        continue

                    native_id = detail.get("wp_post_id") or item.get("slug") or item_url
                    post_number = detail.get("wp_post_id") or item.get("slug")
                    listed_date = item.get("listed_date")
                    published_date = detail.get("published_date") or listed_date
                    original_filename = detail.get("original_filename")

                    metadata = dict(detail.get("metadata") or {})
                    metadata.update(
                        {
                            "posted_date": item.get("listed_date_raw"),
                            "post_number": post_number,
                            "external_id": native_id,
                            "detail_url": item_url,
                        }
                    )
                    if original_filename and not metadata.get("originalFilename"):
                        metadata["originalFilename"] = original_filename

                    paper = {
                        "id": f"{self.site_id}:{native_id}",
                        "site_id": self.site_id,
                        "external_id": str(native_id) if native_id is not None else item_url,
                        "post_number": str(post_number) if post_number is not None else None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": detail.get("authors") or "",
                        "publisher": PUBLISHER,
                        "department": "",
                        "journal": "",
                        "url": item_url,
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords") or "",
                        "category": detail.get("category") or "Final Report",
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{SITE_ID}] saved {saved}/{limit_or_inf}: {title[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{SITE_ID}] item {item_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{SITE_ID}] page {page}: 0 new records; stopping")
                break
            if not has_next:
                print(f"[{SITE_ID}] page {page}: next page link absent; stopping")
                break

        if page >= SAFETY_PAGE_CAP:
            print(f"[{SITE_ID}] safety cap of {SAFETY_PAGE_CAP} pages reached")

        print(f"[{SITE_ID}] done: saved {saved}/{limit_or_inf}")
        return saved
