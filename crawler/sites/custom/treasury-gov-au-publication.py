# -*- coding: utf-8 -*-
"""Australian Treasury publication crawler.

Starting URL: https://treasury.gov.au/publication
Pagination: ?page=N (0-indexed, ~239 pages, 10 items/page)
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://treasury.gov.au"
_LIST_URL = f"{_BASE}/publication"


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3):
    """Fetch URL via curl with TLS-max 1.3, retry with exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-AU,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[treasury-gov-au-publication] empty response for {url}, "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[treasury-gov-au-publication] curl error: {exc}, "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            else:
                print(
                    f"[treasury-gov-au-publication] curl failed after {retries} attempts: {exc}"
                )
    return None


def _parse_date(raw: str) -> str:
    """Convert '28 May 2026' or ISO datetime → 'YYYY-MM-DD'. Returns '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    # ISO datetime like "2026-05-28T12:00:00Z"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _strip_label(text: str, label: str) -> str:
    """Remove Drupal field label prefix: 'Author Treasury' → 'Treasury'."""
    if text.lower().startswith(label.lower()):
        return text[len(label):].strip()
    return text


_FOOTER_NOISE = (
    "reconciliation", "traditional custodians", "twitter", "facebook",
    "linkedin", "keep up to date", "© commonwealth",
)


def _parse_list_page(html: str) -> list:
    """Extract publication stubs from a list page."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[treasury-gov-au-publication] list parse error: {exc}")
        return []

    items = []
    for row in soup.find_all(class_="views-row"):
        try:
            title_el = row.find(class_=re.compile(r"\bfield--name-node-title\b"))
            if not title_el:
                title_el = row.find(class_=re.compile(r"\bfield--name-title\b"))
            if not title_el:
                continue
            link = title_el.find("a")
            if not link:
                continue
            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = _BASE + href

            title = link.get_text(strip=True)
            if not title:
                continue

            # Date from list row
            date_raw = ""
            date_iso = ""
            date_el = row.find(class_=re.compile(r"\bfield--name-field-date\b"))
            if date_el:
                time_el = date_el.find("time")
                if time_el:
                    # Prefer the human-readable text for parsing
                    date_raw = time_el.get("datetime", "") or time_el.get_text(strip=True)
                    date_iso = _parse_date(time_el.get_text(strip=True))
                    if not date_iso:
                        date_iso = _parse_date(time_el.get("datetime", ""))
                else:
                    date_raw = date_el.get_text(strip=True)
                    date_iso = _parse_date(date_raw)

            slug = href.rstrip("/").split("/")[-1]

            items.append({
                "title": title,
                "url": href,
                "slug": slug,
                "date_raw": date_raw,
                "date_iso": date_iso,
            })
        except Exception as exc:
            print(f"[treasury-gov-au-publication] list row parse error: {exc}")
            continue

    return items


def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract full metadata from a publication detail page."""
    result = {
        "abstract": "",
        "pdf_url": "",
        "authors": "",
        "category": "",
        "keywords": "",
        "published_date": "",
        "original_filename": "",
        "metadata": {},
    }

    try:
        soup = _make_soup(html)

        # Published date
        date_el = soup.find(class_=re.compile(r"\bfield--name-field-date\b"))
        if date_el:
            time_el = date_el.find("time")
            if time_el:
                result["published_date"] = _parse_date(time_el.get_text(strip=True))
                result["metadata"]["date_raw"] = time_el.get("datetime", "")
            else:
                result["published_date"] = _parse_date(date_el.get_text(strip=True))

        # Author — "Author Treasury" → "Treasury"
        author_el = soup.find(class_=re.compile(r"\bfield--name-field-author\b"))
        if author_el:
            txt = re.sub(r"\s+", " ", author_el.get_text(" ", strip=True)).strip()
            result["authors"] = _strip_label(txt, "Author")

        # Topics → keywords + category
        topics_el = soup.find(class_=re.compile(r"\bfield--name-field-topics\b"))
        if topics_el:
            txt = re.sub(r"\s+", " ", topics_el.get_text(" ", strip=True)).strip()
            txt = _strip_label(txt, "Topic")
            result["keywords"] = txt
            result["category"] = txt

        # Publication type → fallback category
        pubtype_el = soup.find(class_=re.compile(r"\bfield--name-field-publication-type\b"))
        if pubtype_el:
            txt = re.sub(r"\s+", " ", pubtype_el.get_text(" ", strip=True)).strip()
            txt = _strip_label(txt, "Publication type")
            result["metadata"]["publication_type"] = txt
            if not result["category"]:
                result["category"] = txt

        # ISBN (class is field-isbn-new, not field-isbn)
        isbn_el = soup.find(class_=re.compile(r"\bfield--name-field-isbn-new\b"))
        if isbn_el:
            txt = re.sub(r"\s+", " ", isbn_el.get_text(" ", strip=True)).strip()
            txt = _strip_label(txt, "ISBN")
            result["metadata"]["isbn"] = txt

        # PDF attachment — check field-attachments then field-media-file
        for attach_cls in (
            re.compile(r"\bfield--name-field-attachments\b"),
            re.compile(r"\bfield--name-field-media-file\b"),
        ):
            attach_el = soup.find(class_=attach_cls)
            if attach_el:
                pdf_link = attach_el.find("a", href=re.compile(r"\.pdf", re.I))
                if pdf_link:
                    href = pdf_link.get("href", "")
                    if href:
                        if not href.startswith("http"):
                            href = _BASE + href
                        result["pdf_url"] = href
                        result["original_filename"] = (
                            href.rstrip("/").split("/")[-1].split("?")[0]
                        )
                        break

        # Fallback PDF: any link on the page
        if not result["pdf_url"]:
            for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
                href = a.get("href", "")
                if href:
                    if not href.startswith("http"):
                        href = _BASE + href
                    result["pdf_url"] = href
                    result["original_filename"] = (
                        href.rstrip("/").split("/")[-1].split("?")[0]
                    )
                    break

        # Abstract: collect from field--name-body, skip footer noise
        abstract_parts = []
        for body_el in soup.find_all(class_=re.compile(r"\bfield--name-body\b")):
            for tag in body_el.find_all(["script", "style"]):
                tag.decompose()
            text = re.sub(r"\s+", " ", body_el.get_text(" ", strip=True)).strip()
            if not text or len(text) < 30:
                continue
            lower = text.lower()
            if any(kw in lower for kw in _FOOTER_NOISE):
                continue
            abstract_parts.append(text)

        if abstract_parts:
            result["abstract"] = " ".join(abstract_parts)

        # Fallback: scrape main content paragraphs
        if len(result["abstract"]) < 50:
            main = (
                soup.find("main")
                or soup.find(id="main-content")
                or soup.find("article")
                or soup.body
            )
            if main:
                for tag in main.find_all(
                    ["nav", "header", "footer", "script", "style", "form", "aside"]
                ):
                    tag.decompose()
                parts = []
                for el in main.find_all(["p", "li"]):
                    t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
                    if t and len(t) > 30:
                        lower = t.lower()
                        if any(kw in lower for kw in _FOOTER_NOISE):
                            continue
                        parts.append(t)
                if parts:
                    result["abstract"] = " ".join(parts)

    except Exception as exc:
        print(f"[treasury-gov-au-publication] detail parse error ({page_url}): {exc}")
        # Regex fallback
        try:
            m = re.search(
                r'field--name-body[^>]*>.*?<div[^>]*>(.*?)</div>',
                html,
                re.S,
            )
            if m:
                text = re.sub(r"<[^>]+>", " ", m.group(1))
                result["abstract"] = re.sub(r"\s+", " ", text).strip()
            pm = re.search(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
            if pm:
                href = pm.group(1)
                if not href.startswith("http"):
                    href = _BASE + href
                result["pdf_url"] = href
                result["original_filename"] = (
                    href.rstrip("/").split("/")[-1].split("?")[0]
                )
        except Exception:
            pass

    return result


class TreasuryGovAuPublicationCrawler(BaseCrawler):
    """Crawler for Australian Treasury publications."""

    site_id = "treasury-gov-au-publication"
    site_name = "Custom: treasury-gov-au-publication"
    base_url = "https://treasury.gov.au"

    def crawl(self, limit=None):
        """Crawl Treasury publications from ?page=N (0-indexed).

        Fetches each detail page for full metadata. Respects `limit` for any
        value — small (3) or large (500+).
        """
        saved = 0
        seen_urls: set = set()
        crawl_start = time.time()
        max_wall = 25 * 60  # 25-minute budget
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(max_pages):
            # Wall-clock budget
            if time.time() - crawl_start > max_wall:
                print(
                    "[treasury-gov-au-publication] 25-minute wall clock budget reached. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            list_url = f"{_LIST_URL}?page={page_num}"
            raw = _curl_get(list_url)
            if not raw:
                print(
                    f"[treasury-gov-au-publication] page {page_num}: failed to fetch, skipping"
                )
                continue

            items = _parse_list_page(raw)
            if not items:
                print(
                    f"[treasury-gov-au-publication] page {page_num}: no items found. Done."
                )
                break

            # Progress log every 10 pages
            if page_num % 10 == 0:
                print(
                    f"[treasury-gov-au-publication] page {page_num}: "
                    f"saved {saved}/{limit_display}"
                )

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]

                # URL dedup — prevents infinite loops if paginator wraps
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)

                    detail_html = _curl_get(item_url)
                    if detail_html:
                        detail = _parse_detail_page(detail_html, item_url)
                    else:
                        detail = {
                            "abstract": "",
                            "pdf_url": "",
                            "authors": "",
                            "category": "",
                            "keywords": "",
                            "published_date": item.get("date_iso", ""),
                            "original_filename": "",
                            "metadata": {},
                        }

                    abstract = detail["abstract"].strip()

                    if len(abstract) < 50:
                        print(
                            f"[treasury-gov-au-publication] skipping (abstract <50 chars): "
                            f"{item['title'][:60]}"
                        )
                        continue

                    slug = item["slug"]
                    published_date = detail.get("published_date") or item.get("date_iso", "")
                    listed_date = item.get("date_iso", "")

                    metadata = {
                        "posted_date": item.get("date_raw", ""),
                        "slug": slug,
                    }
                    metadata.update(detail.get("metadata", {}))

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "title": item["title"],
                        "abstract": abstract,
                        "authors": detail.get("authors", ""),
                        "department": "Australian Treasury",
                        "category": detail.get("category", ""),
                        "keywords": detail.get("keywords", ""),
                        "published_date": published_date,
                        # posted_date in paper dict → listed_date in libertree v2
                        "posted_date": listed_date,
                        "url": item_url,
                        "pdf_url": detail.get("pdf_url", ""),
                        "original_filename": detail.get("original_filename", ""),
                        "doi": "",
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[treasury-gov-au-publication] saved {saved}/{limit_display}: "
                        f"{item['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[treasury-gov-au-publication] item failed ({item_url}): {exc}"
                    )
                    continue

            # End-of-pagination: all items on this page already seen
            if new_on_page == 0:
                print(
                    f"[treasury-gov-au-publication] page {page_num}: "
                    "all results already seen. Done."
                )
                break

        if page_num == max_pages - 1:
            print(
                f"[treasury-gov-au-publication] safety cap of {max_pages} pages reached."
            )

        print(f"[treasury-gov-au-publication] Done. Total saved: {saved}")
        return saved
