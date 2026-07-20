# -*- coding: utf-8 -*-
"""Crawler for gov.ie — all publications at https://www.gov.ie/en/publications/."""

import json
import re
import subprocess
import time
import urllib.parse
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler

_BS_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str):
    """Try parsers in fallback order; return BeautifulSoup or None."""
    from bs4 import BeautifulSoup
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3):
    """Fetch URL with curl, retry with exponential backoff. Returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[gov-ie-en] Empty response for {url}, retrying in {wait}s...")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[gov-ie-en] curl error for {url}: {exc}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[gov-ie-en] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _parse_iso_date(raw: str) -> str:
    """Convert various date formats to YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    try:
        return datetime.strptime(raw, "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    return raw


def _slug_from_url(url: str) -> str:
    """Extract trailing slug from a gov.ie publication URL."""
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else ""


def _extract_jsonld(soup) -> dict:
    """Return first JSON-LD dict from page, or {}."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


def _extract_abstract(soup, jld_desc: str = "") -> str:
    """Extract body text from a detail page.

    Strategy:
    1. Collect substantive <p> tags from <main> (skip short/boilerplate lines).
    2. If that yields < 100 chars, fall back to full main text with boilerplate
       stripped via the 'Last updated on:' regex.
    3. Prepend JSON-LD description if result is still < 100 chars.
    """
    main = soup.find("main")
    if not main:
        return jld_desc

    # Remove structural noise in-place before text extraction
    for tag in main.find_all(["nav", "header", "footer", "script", "style", "noscript"]):
        tag.decompose()

    # Collect paragraphs (skip "From: …" boilerplate and very short fragments)
    para_texts = []
    for p in main.find_all("p"):
        txt = re.sub(r"\s+", " ", p.get_text(separator=" ", strip=True))
        if len(txt) > 40 and not re.match(r"^From:\s", txt):
            para_texts.append(txt)

    body = " ".join(para_texts).strip()

    # Fallback: full main text with leading boilerplate stripped
    if len(body) < 100:
        full = main.get_text(separator=" ", strip=True)
        full = re.sub(
            r"^.*?(?:Last updated on:\s*\d{1,2}\s+\w+\s+\d{4})\s*",
            "",
            full,
            flags=re.DOTALL,
        )
        full = re.sub(r"\s*View\s+the\s+file\s*View.*$", "", full, flags=re.DOTALL | re.IGNORECASE)
        full = re.sub(r"\s+", " ", full).strip()
        if len(full) > len(body):
            body = full

    # Supplement with JSON-LD description if still short
    if len(body) < 100 and jld_desc:
        body = (jld_desc + " " + body).strip() if body else jld_desc

    return body[:5000]


class GovIeEnCrawler(BaseCrawler):
    """Crawler for gov.ie — all publications at /en/publications/."""

    site_id = "gov-ie-en"
    site_name = "Custom: gov-ie-en"
    base_url = "https://www.gov.ie"

    _LIST_URL = (
        "https://www.gov.ie/en/search/"
        "?category=Press+release"
        "&organisation=Department+of+Transport"
        "&page={page}"
    )
    _MAX_PAGES = 200
    _CRAWL_TIMEOUT_SECS = 25 * 60

    def crawl(self, limit=None):
        """Crawl publications list + detail pages, save via _save_paper."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "inf"

        for page in range(1, self._MAX_PAGES + 1):
            if time.time() - start_time > self._CRAWL_TIMEOUT_SECS:
                print(f"[gov-ie-en] 25-minute budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[gov-ie-en] page {page}: saved {saved}/{limit_display}")

            list_url = self._LIST_URL.format(page=page)
            html = _curl_get(list_url)
            if not html:
                print(f"[gov-ie-en] Failed to fetch list page {page}. Stopping.")
                break

            soup = _make_soup(html)
            if soup is None:
                print(f"[gov-ie-en] Failed to parse list page {page}. Stopping.")
                break

            # BS4 lowercases all attribute names: data-createdAt → data-createdat
            cards = soup.find_all("div", attrs={"data-createdat": True})
            if not cards:
                print(f"[gov-ie-en] No cards on page {page}. End of results.")
                break

            new_on_page = 0
            for card in cards:
                if limit is not None and saved >= limit:
                    break

                try:
                    link_tag = card.find("a", class_="gi-link")
                    if not link_tag:
                        continue
                    detail_url = link_tag.get("href", "").strip()
                    if not detail_url:
                        continue
                    if not detail_url.startswith("http"):
                        detail_url = self.base_url + detail_url
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    title_list = (card.get("data-title") or link_tag.get_text(strip=True)).strip()
                    listed_date_raw = card.get("data-createdat", "").strip()
                    listed_date = _parse_iso_date(listed_date_raw)

                    # Subheading: "11 May 2026; Department of X; Press release"
                    department = ""
                    category = ""
                    sub_div = card.find(
                        "div", class_=lambda c: c and "gi-card-subheading" in c
                    )
                    if sub_div:
                        sub_text = sub_div.get_text(separator=";", strip=True)
                        parts = [p.strip() for p in sub_text.split(";") if p.strip()]
                        if len(parts) >= 2:
                            department = parts[1]
                        if len(parts) >= 3:
                            category = parts[2]

                    time.sleep(1.0)
                    item_saved = self._fetch_and_save(
                        detail_url=detail_url,
                        title_fallback=title_list,
                        listed_date=listed_date,
                        department=department,
                        category=category,
                    )
                    if item_saved:
                        saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[gov-ie-en] item failed ({detail_url}): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[gov-ie-en] Page {page}: all items already seen. Stopping.")
                break

        if page >= self._MAX_PAGES:
            print(f"[gov-ie-en] Safety cap of {self._MAX_PAGES} pages reached.")

        print(f"[gov-ie-en] Done. Total saved: {saved}")
        return saved

    def _fetch_and_save(
        self,
        detail_url: str,
        title_fallback: str,
        listed_date: str,
        department: str = "",
        category: str = "",
    ) -> bool:
        """Fetch detail page, parse, and save. Returns True if saved."""
        html = _curl_get(detail_url)
        if not html:
            print(f"[gov-ie-en] Failed to fetch {detail_url}, skipping.")
            return False

        soup = _make_soup(html)
        if soup is None:
            print(f"[gov-ie-en] Parse failed for {detail_url}, skipping.")
            return False

        jsonld = _extract_jsonld(soup)

        h1 = soup.find("h1")
        title = (
            (h1.get_text(strip=True) if h1 else "")
            or jsonld.get("name", "")
            or jsonld.get("headline", "")
            or title_fallback
        )

        published_raw = jsonld.get("datePublished", "")
        published_date = _parse_iso_date(published_raw) if published_raw else listed_date

        author_obj = jsonld.get("author") or {}
        if isinstance(author_obj, list):
            authors = "; ".join(
                a.get("name", "") for a in author_obj if isinstance(a, dict) and a.get("name")
            )
        elif isinstance(author_obj, dict):
            authors = author_obj.get("name", "")
        else:
            authors = ""

        publisher_obj = jsonld.get("publisher") or {}
        if isinstance(publisher_obj, dict):
            publisher = publisher_obj.get("name", "")
        else:
            publisher = str(publisher_obj) if publisher_obj else ""
        if not publisher:
            publisher = department or "Government of Ireland"

        jld_desc = jsonld.get("description", "")
        abstract = _extract_abstract(soup, jld_desc)

        if len(abstract) < 50:
            print(
                f"[gov-ie-en] Abstract too short ({len(abstract)} chars) for {detail_url}, skipping."
            )
            return False
        if len(abstract) < 100:
            print(
                f"[gov-ie-en] Abstract <100 chars ({len(abstract)}) for {detail_url}, skipping."
            )
            return False

        # Collect PDF links
        pdf_url = None
        original_filename = None
        all_pdfs = []
        for a in soup.find_all("a", href=re.compile(r"\.pdf", re.IGNORECASE)):
            href = a.get("href", "")
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = self.base_url + href
            all_pdfs.append(href)
        if all_pdfs:
            pdf_url = all_pdfs[0]
            original_filename = urllib.parse.unquote(pdf_url.rstrip("/").split("/")[-1].split("?")[0])

        slug = _slug_from_url(detail_url)
        post_number = slug or None

        meta_kw = soup.find("meta", attrs={"name": "keywords"})
        keywords = meta_kw.get("content", "").strip() if meta_kw else ""

        metadata = json.dumps(
            {
                "posted_date": listed_date,
                "originalFilename": original_filename,
                "jsonld_type": jsonld.get("@type", ""),
                "dateModified": jsonld.get("dateModified", ""),
                "all_pdf_urls": all_pdfs,
                "category": category,
                "department": department,
            },
            ensure_ascii=False,
        )

        paper = {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": authors or department,
            "publisher": publisher,
            "department": department,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": "",
            "original_filename": original_filename,
            "metadata": metadata,
        }

        self._save_paper(paper)
        print(f"[gov-ie-en] Saved: {title[:70]}")
        return True
