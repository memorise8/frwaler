# -*- coding: utf-8 -*-
"""Crawler for Public Safety Canada news releases.

Starting URL:
https://www.publicsafety.gc.ca/cnt/nws/nws-rlss/index-en.aspx

The listing is server-rendered HTML. Items link mostly to Canada.ca AEM
news-release detail pages, with older legacy Public Safety ASP.NET pages also
possible. There is no JSON endpoint exposed by the page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

# Absolute import: spec_from_file_location has no package context.
from crawler.base_crawler import BaseCrawler


SITE_ID = "publicsafety-gc-ca-cnt"
BASE_URL = "https://www.publicsafety.gc.ca"
LIST_URL = f"{BASE_URL}/cnt/nws/nws-rlss/index-en.aspx"
# The index page itself has no query-string pagination — it is one flat list
# (~900 entries, back to ~2015). Older history lives on separate per-year
# "archived" pages linked from the top of the index. Queuing them after the
# main list gives a genuine full-depth crawl for limits beyond what the
# single index page holds.
ARCHIVE_YEAR_URLS = [
    f"{BASE_URL}/cnt/nws/nws-rlss/{year}/index-eng.aspx"
    for year in (2015, 2014, 2013, 2012, 2011)
]
MAX_PAGES = 200
MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
WALL_CLOCK_GRACE_SECONDS = 15


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _make_soup(raw: str) -> BeautifulSoup | None:
    """Parse HTML with html5lib -> lxml -> html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[{SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
            continue
    return None


def _fetch(url: str, *, retries: int = 3, timeout: int = 40) -> str | None:
    """Fetch URL using curl with exponential-backoff retries."""
    backoffs = (1, 3, 9)
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--tls-max",
                    "1.3",
                    "-sk",
                    "-L",
                    "--max-time",
                    str(timeout),
                    url,
                ],
                capture_output=True,
                timeout=timeout + 5,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[{SITE_ID}] curl failed (attempt {attempt + 1}/{retries}) "
                f"for {url}: code={result.returncode} {stderr}"
            )
        except Exception as exc:
            print(f"[{SITE_ID}] curl error (attempt {attempt + 1}/{retries}) for {url}: {exc}")

        if attempt < retries - 1:
            time.sleep(backoffs[attempt])

    return None


def _date_to_iso(raw: str | None) -> str | None:
    raw = _clean_text(raw)
    if not raw:
        return None

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)

    # Detail-page first paragraph often looks like:
    # "May 28, 2026 - Ottawa, Ontario" or uses an en dash.
    raw_date = re.split(r"\s+[–-]\s+", raw, maxsplit=1)[0].strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    # OpenGraph-style tags (og:title, og:description, ...) use `property=`
    # rather than `name=` — check both so pm.gc.ca (which only sets the
    # `property` variant) isn't silently skipped.
    tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    if not tag:
        return None
    return _clean_text(tag.get("content"))


def _tag_text(tag) -> str:
    if not tag:
        return ""
    return _clean_text(tag.get_text(" ", strip=True))


def _first_meta(soup: BeautifulSoup, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _meta_content(soup, name)
        if value:
            return value
    return None


def _canonical_url(soup: BeautifulSoup, fallback_url: str) -> str:
    link = soup.find("link", rel=lambda rel: rel and "canonical" in rel)
    href = link.get("href") if link else None
    if href:
        return urljoin(fallback_url, href)
    return fallback_url


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else url
    tail = re.sub(r"\.(html?|aspx)$", "", tail, flags=re.I)
    return unquote(tail) or url


def _post_number_from_url(url: str, slug: str) -> str:
    legacy = re.search(r"/(\d{8})(?:-[a-z]{2,3})?\.aspx", url, flags=re.I)
    if legacy:
        return legacy.group(1)
    # Canada.ca news URLs do not expose a numeric post id. Use native slug.
    return slug


def _filename_from_pdf_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if tail and "." in tail and len(tail) <= 220:
        return tail
    return None


def _filename_from_content_disposition(header_value: str | None) -> str | None:
    if not header_value:
        return None
    msg = Message()
    msg["Content-Disposition"] = header_value
    filename = msg.get_filename()
    return _clean_text(filename) if filename else None


def _find_pdf(soup: BeautifulSoup, page_url: str) -> tuple[str | None, str | None]:
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        if ".pdf" not in href.lower():
            continue
        pdf_url = urljoin(page_url, href)
        return pdf_url, _filename_from_pdf_url(pdf_url)
    return None, None


def _normalize_keywords(*values: str | None) -> str | None:
    parts: list[str] = []
    for value in values:
        if not value:
            continue
        for piece in re.split(r"[,;]", value):
            piece = _clean_text(piece)
            if piece and piece not in parts:
                parts.append(piece)
    return ", ".join(parts) if parts else None


_PUBLISHER_BY_DOMAIN = (
    ("publicsafety.gc.ca", "Public Safety Canada"),
    ("securitepublique.gc.ca", "Public Safety Canada"),
    ("pm.gc.ca", "Office of the Prime Minister"),
    ("scics.ca", "Canadian Intergovernmental Conference Secretariat"),
    ("canada.ca", "Government of Canada"),
)


def _publisher_from_domain(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    for needle, publisher in _PUBLISHER_BY_DOMAIN:
        if needle in host:
            return publisher
    return None


def _publisher_from_byline(soup: BeautifulSoup) -> str | None:
    byline = soup.find(class_="gc-byline")
    if not byline:
        return None
    text = _clean_text(byline.get_text(" ", strip=True))
    text = re.sub(r"^From:\s*", "", text, flags=re.I).strip()
    return text or None


def _find_content_container(soup: BeautifulSoup):
    """Locate the real article/body container across the several distinct
    government templates linked from the index (canada.ca, publicsafety.gc.ca,
    pm.gc.ca, scics.ca, ...). Falls back to ``<body>`` rather than ``None`` so
    off-template pages still yield *something* instead of an empty abstract.
    """
    return (
        soup.find(id="news-release-container")
        or soup.find(attrs={"property": "schema:text"})
        or soup.find("main", attrs={"property": "mainContentOfPage"})
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find(id="main-content")
        or soup.find("article")
        or soup.find(class_="col-md-9")
        or soup.find("body")
    )


def _extract_body_abstract(soup: BeautifulSoup) -> str:
    container = _find_content_container(soup)
    if not container:
        return ""

    for dead in container.find_all(
        ["script", "style", "noscript", "nav", "aside", "footer", "form", "dl"]
    ):
        dead.decompose()
    for noisy in container.find_all(class_=re.compile(r"(breadcrumb|pagedetails|wb-dtmd)", re.I)):
        noisy.decompose()

    paragraphs: list[str] = []
    for node in container.find_all(["p", "li"]):
        text = _clean_text(node.get_text(" ", strip=True))
        if not text:
            continue
        if text.lower() in {"news release", "archived content"}:
            continue
        if text.startswith("Information identified as archived"):
            continue
        if text.startswith("This page has been archived"):
            continue
        if text.startswith("From:"):
            continue
        if len(text) < 30 and not paragraphs:
            continue
        paragraphs.append(text)
        if sum(len(p) for p in paragraphs) >= 1600:
            break

    return " ".join(paragraphs).strip()


def _extract_first_body_date(soup: BeautifulSoup) -> str | None:
    container = _find_content_container(soup)
    if not container:
        return None
    # Only look at the first handful of paragraphs (the dateline is always
    # near the top) — scanning the whole container risks picking up an
    # unrelated "Date modified" footer date instead of the real one.
    for p_tag in container.find_all("p", limit=6):
        text = _clean_text(p_tag.get_text(" ", strip=True))
        parsed = _date_to_iso(text)
        if parsed:
            return parsed
    return None


_MONTH_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s+\d{4}\b"
)


def _extract_list_items(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Extract news-release rows from a list page.

    The three markup variants seen across the current index and the
    per-year archive pages (``<li class="list-group-item">`` with a
    ``<small class="text-muted">`` date, ``<li class="list-group-item">``
    with a bare ``<small>`` date, and a plain ``<li>`` with the date as
    trailing text) all share one trait: the row's text contains a
    "Month D, YYYY" date next to the link. Matching on that date pattern
    instead of specific classes/tags is what makes this work uniformly,
    and it naturally excludes nav-only ``<li>`` rows (breadcrumbs, the
    "20xx (archived)" links) since those never contain a date.
    """
    items: list[dict] = []
    main = soup.find(id="wb-cont") or soup.find("main") or soup

    for li_tag in main.find_all("li"):
        a_tag = li_tag.find("a", href=True)
        if not a_tag:
            continue

        href = a_tag.get("href", "")
        title = _clean_text(a_tag.get_text(" ", strip=True))
        if not href or not title:
            continue

        text_all = _clean_text(li_tag.get_text(" ", strip=True))
        date_match = _MONTH_DATE_RE.search(text_all)
        if not date_match:
            # Not a news-release row (nav link, breadcrumb, archive-year link, ...).
            continue

        url = urljoin(page_url, href)
        if not url.lower().startswith("http"):
            continue

        raw_date = date_match.group(0)
        listed_date = _date_to_iso(raw_date)
        slug = _slug_from_url(url)

        items.append(
            {
                "title": title,
                "url": url,
                "raw_listed_date": raw_date,
                "listed_date": listed_date,
                "external_id": slug,
                "post_number": _post_number_from_url(url, slug),
                "source_list_url": page_url,
            }
        )

    return items


def _extract_next_page_url(soup: BeautifulSoup, page_url: str) -> str | None:
    for a_tag in soup.find_all("a", href=True):
        rel = " ".join(a_tag.get("rel") or [])
        text = _clean_text(a_tag.get_text(" ", strip=True)).lower()
        aria = _clean_text(a_tag.get("aria-label")).lower()
        if "next" in rel.lower() or text in {"next", "next page"} or "next" in aria:
            return urljoin(page_url, a_tag["href"])
    return None


def _parse_detail(detail_html: str, url: str, list_item: dict) -> dict:
    soup = _make_soup(detail_html)
    if soup is None:
        raise ValueError("detail parser unavailable")

    canonical_url = _canonical_url(soup, url)
    title = (
        _first_meta(soup, ("dcterms.title", "og:title"))
        or _tag_text(soup.find("h1"))
        or list_item.get("title")
    )
    abstract = (
        _first_meta(soup, ("dcterms.description", "description", "og:description"))
        or _tag_text(soup.find("p", class_=lambda cls: cls and "teaser" in cls))
    )
    body_abstract = _extract_body_abstract(soup)
    if len(body_abstract) > len(abstract or ""):
        abstract = body_abstract

    published_date = (
        _date_to_iso(_meta_content(soup, "dcterms.issued"))
        or _extract_first_body_date(soup)
        or list_item.get("listed_date")
    )
    listed_date = list_item.get("listed_date") or published_date

    publisher = (
        _publisher_from_byline(soup)
        or _first_meta(soup, ("dcterms.creator", "author"))
        or _publisher_from_domain(canonical_url)
        or "Public Safety Canada"
    )
    category = _first_meta(soup, ("dcterms.type",)) or "News release"
    keywords = _normalize_keywords(
        _meta_content(soup, "keywords"),
        _meta_content(soup, "dcterms.subject"),
    )
    pdf_url, original_filename = _find_pdf(soup, canonical_url)
    if pdf_url and not original_filename:
        original_filename = _filename_from_content_disposition(None)

    slug = _slug_from_url(canonical_url)
    external_id = slug or list_item.get("external_id")
    post_number = _post_number_from_url(canonical_url, external_id)

    raw_meta = {}
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property")
        content = tag.get("content")
        if name and content:
            raw_meta[name] = _clean_text(content)

    metadata = {
        "posted_date": list_item.get("raw_listed_date") or listed_date,
        "listed_date": listed_date,
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "native_slug": external_id,
        "native_url_path": urlparse(canonical_url).path,
        "post_number": post_number,
        "source_list_url": list_item.get("source_list_url"),
        "source_host": urlparse(canonical_url).netloc,
        "canonical_url": canonical_url,
        "raw_list_title": list_item.get("title"),
        "raw_listed_date": list_item.get("raw_listed_date"),
        "detail_meta": raw_meta,
    }

    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, canonical_url)),
        "site_id": SITE_ID,
        "external_id": external_id,
        "post_number": post_number,
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "listed_date": listed_date,
        "posted_date": listed_date,
        "authors": None,
        "publisher": publisher,
        "department": publisher,
        "journal": None,
        "url": canonical_url,
        "pdf_url": pdf_url,
        "keywords": keywords,
        "category": category,
        "doi": None,
        "original_filename": original_filename,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


class PublicSafetyGcCaCntCrawler(BaseCrawler):
    site_id = "publicsafety-gc-ca-cnt"
    site_name = "Custom: publicsafety-gc-ca-cnt"
    base_url = "https://www.publicsafety.gc.ca"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        seen_page_urls: set[str] = set()
        # Queue starts with the flat current-list index, then falls back to
        # the per-year archive pages for full depth. Any genuine "next"
        # link discovered on a page (in case the site adds real pagination
        # later) is appended to the same queue rather than followed ad hoc.
        pending_pages = [LIST_URL] + list(ARCHIVE_YEAR_URLS)
        page = 0
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while pending_pages:
            if limit is not None and saved >= limit:
                break
            if page >= MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached; stopping.")
                break
            elapsed = time.time() - start_time
            if elapsed >= MAX_WALL_SECONDS - WALL_CLOCK_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly.")
                break

            page_url = pending_pages.pop(0)
            if page_url in seen_page_urls:
                continue
            seen_page_urls.add(page_url)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_html = _fetch(page_url)
            if not list_html:
                print(f"[{self.site_id}] page {page}: fetch failed; skipping.")
                page += 1
                continue

            soup = _make_soup(list_html)
            if soup is None:
                print(f"[{self.site_id}] page {page}: parse failed; skipping.")
                page += 1
                continue

            items = _extract_list_items(soup, page_url)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; skipping.")
                page += 1
                continue

            new_items = [item for item in items if item["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page}: all item URLs already seen; skipping.")
                page += 1
                continue
            for item in new_items:
                seen_urls.add(item["url"])

            for item_number, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= MAX_WALL_SECONDS - WALL_CLOCK_GRACE_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly.")
                    return saved

                try:
                    time.sleep(getattr(self, "_delay", 1.0))
                    detail_html = _fetch(item["url"])
                    if not detail_html:
                        print(f"[{self.site_id}] item {item_number} failed: detail fetch returned no body")
                        continue

                    paper = _parse_detail(detail_html, item["url"], item)
                    abstract = _clean_text(paper.get("abstract"))
                    paper["abstract"] = abstract
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] skipping (abstract <50 chars): {item['url']}")
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_number} failed: {exc}")
                    continue

            next_url = _extract_next_page_url(soup, page_url)
            if next_url and next_url not in seen_page_urls and next_url not in pending_pages:
                pending_pages.append(next_url)
            page += 1

        print(f"[{self.site_id}] done. saved={saved}")
        return saved
