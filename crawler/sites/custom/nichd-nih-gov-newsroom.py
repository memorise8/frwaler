# -*- coding: utf-8 -*-
"""NICHD newsroom crawler.

Discovery notes:
  - List endpoint: https://www.nichd.nih.gov/newsroom/news?page=N
  - Detail endpoint: each /newsroom/news/<slug> link in the server-rendered
    Drupal view. No JSON list API is needed for the public records exposed by
    this page.
"""

import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_SITE_ID = "nichd-nih-gov-newsroom"
_BASE_URL = "https://www.nichd.nih.gov"
_LIST_URL = "https://www.nichd.nih.gov/newsroom/news"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_MIN_ABSTRACT_CHARS = 50
_RETRY_DELAYS = (1, 3, 9)
_PUBLISHER = (
    "Eunice Kennedy Shriver National Institute of Child Health and Human "
    "Development; National Institutes of Health"
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _strip_text(value):
    if not value:
        return ""
    value = re.sub(r"[\xa0\u200b\u00ad]", " ", str(value))
    return re.sub(r"\s+", " ", value).strip()


def _make_soup(raw):
    """Parse HTML with html5lib -> lxml -> html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup import failed: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
    return None


def _curl_get(url, retries=3):
    """Fetch a URL with curl and retry using 1s, 3s, 9s backoff."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-sk",
        "-L",
        "--max-time",
        "45",
        "-H",
        f"User-Agent: {_USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        url,
    ]
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=55,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and text.strip():
                return text
            print(
                f"[{_SITE_ID}] curl empty/error attempt {attempt + 1}/{attempts} "
                f"for {url}: rc={result.returncode}"
            )
        except Exception as exc:
            print(f"[{_SITE_ID}] curl exception attempt {attempt + 1}/{attempts} for {url}: {exc}")

        if attempt < attempts - 1:
            time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])

    return None


def _absolute_url(href):
    if not href:
        return None
    return urljoin(_BASE_URL, href).split("#", 1)[0].rstrip("/")


def _filename_from_url(url):
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").split("/")[-1]
    if tail and "." in tail and len(tail) <= 200:
        return tail
    return None


def _iso_date(value):
    value = _strip_text(value)
    if not value:
        return None

    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if match:
        return match.group(0)

    cleaned = re.sub(r"^[A-Za-z]+,\s*", "", value)
    formats = (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y",
        "%B %d, %Y",
        "%b %d, %Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _category_from_title(title):
    title = _strip_text(title)
    if ":" in title:
        prefix = title.split(":", 1)[0].strip()
        if 2 <= len(prefix) <= 80:
            return prefix
    return "News"


def _extract_doi(text, soup=None):
    text = _strip_text(text)
    match = re.search(r"\bdoi\s*:?\s*(10\.\d{4,9}/[^\s;,.<>]+(?:[^\s;,.<>])?)", text, re.I)
    if match:
        return match.group(1).rstrip(").,;")

    if soup is not None:
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "doi.org/10." in href:
                return href.split("doi.org/", 1)[1].rstrip(").,;")
    return None


def _extract_reference_fields(body_el, body_text):
    """Best-effort authors/journal/series metadata from NICHD reference text."""
    authors = None
    journal = None
    journal_raw = None
    series = None
    volume = None
    issue = None

    reference_text = ""
    if body_el is not None:
        heading = body_el.find(["h2", "h3", "h4"], string=re.compile(r"^\s*Reference", re.I))
        if heading:
            pieces = []
            for sibling in heading.find_all_next(["p", "li"], limit=3):
                pieces.append(_strip_text(sibling.get_text(" ", strip=True)))
            reference_text = " ".join(pieces)

    if not reference_text:
        match = re.search(r"\bReference\b(.+)", body_text, re.I)
        if match:
            reference_text = match.group(1)

    reference_text = _strip_text(reference_text)
    if reference_text:
        first_sentence = reference_text.split(".", 1)[0].strip()
        if first_sentence and len(first_sentence) <= 200:
            authors = "; ".join(p.strip() for p in first_sentence.split(",") if p.strip())

        doi_pos = re.search(r"\bDOI\b", reference_text, re.I)
        before_doi = reference_text[: doi_pos.start()] if doi_pos else reference_text
        parts = [p.strip() for p in before_doi.split(".") if p.strip()]
        if len(parts) >= 2:
            candidate = parts[-1]
            if len(candidate) <= 120 and re.search(r"[A-Za-z]", candidate):
                journal = candidate
                journal_raw = candidate

        vi = re.search(r"\b(?:Vol(?:ume)?\.?\s*)?(\d+)\s*\((\d+)\)", reference_text, re.I)
        if vi:
            volume = vi.group(1)
            issue = vi.group(2)

    if not journal:
        match = re.search(r"\bappears in\s+(.+?)(?:\.|\s+DOI\b)", body_text, re.I)
        if match:
            journal = _strip_text(match.group(1)).strip(" .")
            journal_raw = journal

    return {
        "authors": authors,
        "journal": journal,
        "journal_raw": journal_raw,
        "series": series,
        "volume": volume,
        "issue": issue,
        "reference_text": reference_text or None,
    }


def _extract_pdf(body_el):
    if body_el is None:
        return None, None
    for link in body_el.find_all("a", href=True):
        href = link.get("href", "")
        if ".pdf" not in href.lower():
            continue
        pdf_url = _absolute_url(href)
        return pdf_url, _filename_from_url(pdf_url)
    return None, None


def _extract_keywords(body_el, category):
    values = []
    if category:
        values.append(category)
    if body_el is not None:
        for link in body_el.find_all("a", href=True):
            href = link.get("href", "")
            if "/health/topics/" not in href and "pubmed.ncbi.nlm.nih.gov" not in href:
                continue
            text = _strip_text(link.get_text(" ", strip=True))
            if text and len(text) <= 80:
                values.append(text)
    deduped = []
    seen = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return ", ".join(deduped) if deduped else None


def _parse_listing_page(raw, page_number):
    soup = _make_soup(raw)
    if soup is None:
        return [], False

    items = []
    selector = ".view-news-search.view-display-id-news_search .views-row > article"
    for article in soup.select(selector):
        link = article.select_one("h2 a[href]")
        if link is None:
            continue

        url = _absolute_url(link.get("href"))
        if not url or "/newsroom/news/" not in url:
            continue

        title = _strip_text(link.get_text(" ", strip=True))
        summary_el = article.select_one(".field--name-body")
        summary = _strip_text(summary_el.get_text(" ", strip=True)) if summary_el else ""
        node_id = _strip_text(article.get("data-history-node-id"))

        image = article.select_one("img")
        image_url = _absolute_url(image.get("src")) if image else None
        image_alt = _strip_text(image.get("alt")) if image else None

        items.append(
            {
                "url": url,
                "title": title,
                "listing_summary": summary,
                "node_id": node_id or None,
                "post_number": node_id or None,
                "category": _category_from_title(title),
                "page_number": page_number,
                "image_url": image_url,
                "image_alt": image_alt,
            }
        )

    has_next = soup.select_one('a[rel="next"], a.usa-pagination__next-page') is not None
    return items, has_next


def _parse_detail(raw, url, listing_item):
    soup = _make_soup(raw)
    if soup is None:
        return None

    article = soup.select_one("main article") or soup.find("article")
    meta = lambda name: soup.find("meta", attrs={"name": name})
    prop = lambda name: soup.find("meta", property=name)

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = _strip_text(h1.get_text(" ", strip=True))
    if not title:
        og_title = prop("og:title")
        title = _strip_text(og_title.get("content")) if og_title else listing_item.get("title", "")

    subtitle_el = article.select_one(".field--name-field-subtitle") if article else None
    subtitle = _strip_text(subtitle_el.get_text(" ", strip=True)) if subtitle_el else None

    date_el = article.select_one(".field--name-field-publication-display-date time") if article else None
    if date_el is None and article is not None:
        date_el = article.find("time")
    date_raw = _strip_text(date_el.get("datetime") or date_el.get_text(" ", strip=True)) if date_el else ""
    date_display_raw = _strip_text(date_el.get_text(" ", strip=True)) if date_el else ""
    sort_meta = meta("NICHDGoogleSortDate")
    sort_date_raw = _strip_text(sort_meta.get("content")) if sort_meta else ""
    published_date = _iso_date(date_raw) or _iso_date(sort_date_raw)
    listed_date = _iso_date(date_display_raw) or published_date
    posted_date_raw = date_display_raw or date_raw or sort_date_raw

    body_el = article.select_one(".field--name-body") if article else None
    body_text = _strip_text(body_el.get_text(" ", strip=True)) if body_el else ""
    if not body_text:
        og_desc = prop("og:description")
        body_text = _strip_text(og_desc.get("content")) if og_desc else ""

    abstract_parts = []
    if subtitle:
        abstract_parts.append(subtitle)
    if body_text:
        abstract_parts.append(body_text)
    abstract = _strip_text(" ".join(abstract_parts))

    node_id = None
    if article is not None:
        node_id = _strip_text(article.get("data-history-node-id"))
    if not node_id:
        nid_el = soup.select_one("#nichd_node_nid")
        node_id = _strip_text(nid_el.get("data-nid")) if nid_el else ""
    if not node_id:
        wrapper = soup.select_one("#page-wrapper")
        candidate = _strip_text(wrapper.get("data-menuapi")) if wrapper else ""
        node_id = candidate if candidate.isdigit() else ""

    canonical = soup.find("link", rel="canonical")
    canonical_url = _absolute_url(canonical.get("href")) if canonical else url
    slug = canonical_url.rstrip("/").split("/")[-1] if canonical_url else url.rstrip("/").split("/")[-1]
    category = listing_item.get("category") or _category_from_title(title)
    pdf_url, original_filename = _extract_pdf(body_el)
    ref = _extract_reference_fields(body_el, body_text)
    doi = _extract_doi(body_text, body_el)
    keywords = _extract_keywords(body_el, category)

    owner = None
    owner_el = soup.select_one(".views-field-field-content-owner .field-content")
    if owner_el:
        owner = _strip_text(owner_el.get_text(" ", strip=True))

    last_reviewed = None
    reviewed_el = soup.select_one(".views-field-field-last-reviewed-date2 time")
    if reviewed_el:
        last_reviewed = _iso_date(reviewed_el.get("datetime") or reviewed_el.get_text(" ", strip=True))

    image_url = listing_item.get("image_url")
    image_alt = listing_item.get("image_alt")
    detail_image = article.select_one(".field--name-field-image img") if article else None
    if detail_image:
        image_url = _absolute_url(detail_image.get("src")) or image_url
        image_alt = _strip_text(detail_image.get("alt")) or image_alt

    native_id = node_id or listing_item.get("node_id") or slug
    metadata = {
        "posted_date": posted_date_raw or None,
        "originalFilename": original_filename,
        "journal_raw": ref.get("journal_raw"),
        "series": ref.get("series"),
        "volume": ref.get("volume"),
        "issue": ref.get("issue"),
        "node_id": node_id or listing_item.get("node_id"),
        "slug": slug,
        "listing_page": listing_item.get("page_number"),
        "listing_summary": listing_item.get("listing_summary"),
        "published_date_raw": date_raw or sort_date_raw or None,
        "listed_date_raw": date_display_raw or None,
        "sort_date_raw": sort_date_raw or None,
        "subtitle": subtitle,
        "content_owner": owner,
        "last_reviewed_date": last_reviewed,
        "reference_text": ref.get("reference_text"),
        "image_url": image_url,
        "image_alt": image_alt,
        "canonical_url": canonical_url,
        "list_endpoint": _LIST_URL,
        "detail_endpoint": canonical_url or url,
        "drupal_view": "news_search.news_search",
    }

    return {
        "id": native_id,
        "external_id": native_id,
        "post_number": node_id or listing_item.get("post_number") or slug,
        "title": title or listing_item.get("title"),
        "abstract": abstract,
        "published_date": published_date,
        "listed_date": listed_date,
        "posted_date": listed_date,
        "authors": ref.get("authors"),
        "publisher": _PUBLISHER,
        "department": owner or "Office of Communications",
        "journal": ref.get("journal"),
        "url": canonical_url or url,
        "pdf_url": pdf_url,
        "keywords": keywords,
        "category": category,
        "doi": doi,
        "original_filename": original_filename,
        "metadata": metadata,
    }


class NichdNihGovNewsroomCrawler(BaseCrawler):
    site_id = "nichd-nih-gov-newsroom"
    site_name = "Custom: nichd-nih-gov-newsroom"
    base_url = "https://www.nichd.nih.gov"

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        seen_urls = set()
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page_number in range(_MAX_PAGES):
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed >= _MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break

            if page_number % 10 == 0:
                print(f"[{self.site_id}] page {page_number}: saved {saved}/{limit_or_inf}")

            list_url = _LIST_URL if page_number == 0 else f"{_LIST_URL}?page={page_number}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] page {page_number}: list fetch failed; stopping")
                break

            items, has_next = _parse_listing_page(raw, page_number)
            if not items:
                print(f"[{self.site_id}] page {page_number}: 0 records; stopping")
                break

            new_items = []
            for item in items:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page_number}: 0 new records; stopping")
                break

            for index, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break

                if time.monotonic() - start_time >= _MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    return saved

                url = item["url"]
                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(url, retries=3)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {url} failed: detail fetch failed")
                        continue

                    detail = _parse_detail(detail_raw, url, item)
                    if not detail:
                        print(f"[{self.site_id}] item {url} failed: detail parse failed")
                        continue

                    abstract = _strip_text(detail.get("abstract"))
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {url} skipped: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": detail.get("id"),
                        "site_id": self.site_id,
                        "external_id": detail.get("external_id"),
                        "post_number": detail.get("post_number"),
                        "title": detail.get("title"),
                        "abstract": abstract,
                        "published_date": detail.get("published_date"),
                        "listed_date": detail.get("listed_date"),
                        "posted_date": detail.get("posted_date"),
                        "authors": detail.get("authors"),
                        "publisher": detail.get("publisher"),
                        "department": detail.get("department"),
                        "journal": detail.get("journal"),
                        "url": detail.get("url"),
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords"),
                        "category": detail.get("category"),
                        "doi": detail.get("doi"),
                        "original_filename": detail.get("original_filename"),
                        "metadata": json.dumps(detail.get("metadata", {}), ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_label = item.get("url") or f"page {page_number} item {index}"
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if not has_next:
                print(f"[{self.site_id}] page {page_number}: next page link absent; stopping")
                break
        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")

        print(f"[{self.site_id}] done: saved {saved}")
        return saved
