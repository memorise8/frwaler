# -*- coding: utf-8 -*-
"""NIN (Netherlands Institute for Neuroscience) publications crawler.

https://nin.nl/publications/ — WordPress site using FacetWP faceted search.
URL-based pagination: ?_paged=N (6 items per page, ~665 pages, ~3988 total).
Detail pages carry the abstract and a shortlink with the numeric post ID.
"""

import json
import re
import time

from crawler.base_crawler import BaseCrawler


def _make_soup(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    # Should never reach here, but just in case
    return BeautifulSoup(raw, "html.parser")


class NinNlPublicationsCrawler(BaseCrawler):
    """Crawler for Netherlands Institute for Neuroscience publications."""

    site_id = "nin-nl-publications"
    site_name = "Custom: nin-nl-publications"
    base_url = "https://nin.nl"

    _LIST_BASE = "https://nin.nl/publications/"
    _MAX_PAGES = 200
    _MIN_ABSTRACT = 100  # chars; skip items below this

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            # Limit check
            if limit is not None and saved >= limit:
                break

            # 25-minute wall-clock budget
            if time.time() - start_time > 25 * 60:
                print(f"[{self.site_id}] 25-minute budget reached, stopping.")
                break

            # Safety cap
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")
                break

            # Fetch list page (page 1 uses bare URL to avoid any redirect quirks)
            list_url = self._LIST_BASE if page == 1 else f"{self._LIST_BASE}?_paged={page}"
            resp = self._request(list_url)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping.")
                break

            raw = resp.content.decode("utf-8", errors="replace")

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{self.site_id}] HTML parse error at page {page}: {exc}")
                page += 1
                continue

            articles = soup.select("article.publications-big-card")
            if not articles:
                print(f"[{self.site_id}] No articles at page {page}. Done.")
                break

            new_on_page = 0

            for article in articles:
                if limit is not None and saved >= limit:
                    break

                try:
                    # --- List-page extraction ---
                    title_el = article.select_one("h3.h6")
                    detail_link_el = article.select_one("a.textlink")
                    pdf_link_el = article.select_one("a.iconlink")

                    if not title_el or not detail_link_el:
                        continue

                    title = title_el.get_text(strip=True)
                    detail_url = (detail_link_el.get("href") or "").strip()

                    if not detail_url:
                        continue
                    if detail_url in seen_urls:
                        continue

                    seen_urls.add(detail_url)
                    new_on_page += 1

                    pdf_url = (pdf_link_el.get("href") or "").strip() if pdf_link_el else ""

                    # Parse meta-lines (Research group / Publication year / Published in / Authors)
                    meta: dict = {}
                    for ml in article.select("div.meta-line"):
                        spans = ml.select("span")
                        if len(spans) >= 2:
                            key = spans[0].get_text(strip=True)
                            val = spans[1].get_text(strip=True)
                            meta[key] = val

                    research_group = meta.get("Research group", "")
                    year = meta.get("Publication year", "")
                    journal = meta.get("Published in", "")
                    authors_raw = meta.get("Authors", "")
                    # Spec: authors separated by ";"
                    authors = "; ".join(a.strip() for a in authors_raw.split(",") if a.strip())

                    # --- Fetch detail page for abstract + post ID ---
                    # Note: _request already sleeps self._delay before each call
                    detail_resp = self._request(
                        detail_url,
                        headers={"Referer": self._LIST_BASE},
                    )
                    if detail_resp is None:
                        print(f"[{self.site_id}] Failed detail fetch, skipping: {detail_url}")
                        continue

                    detail_raw = detail_resp.content.decode("utf-8", errors="replace")

                    try:
                        detail_soup = _make_soup(detail_raw)
                    except Exception as exc:
                        print(f"[{self.site_id}] Detail parse error for {detail_url}: {exc}")
                        continue

                    # Post number: WordPress numeric ID from <link rel="shortlink" href="?p=XXXX">
                    post_number = None
                    shortlink_tag = detail_soup.find("link", rel="shortlink")
                    if shortlink_tag:
                        href = shortlink_tag.get("href", "")
                        m = re.search(r'[?&]p=(\d+)', href)
                        if m:
                            post_number = m.group(1)

                    # Abstract from <div class="text">
                    text_div = detail_soup.select_one("div.text")
                    abstract = ""
                    if text_div:
                        abstract = text_div.get_text(separator=" ", strip=True)

                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Short abstract ({len(abstract)} chars), "
                            f"skipping: {title[:60]}"
                        )
                        continue

                    # Also try PDF link on the detail page if the list page had none
                    if not pdf_url:
                        pdf_el = detail_soup.select_one("a.btn.primary.has-icon-left[href*='.pdf']")
                        if pdf_el:
                            pdf_url = (pdf_el.get("href") or "").strip()

                    # --- Build record ---
                    slug = detail_url.rstrip("/").split("/")[-1]
                    external_id = post_number or slug

                    # ISO date: only year is available, so use YYYY-01-01
                    published_date = f"{year}-01-01" if year and year.isdigit() else None

                    # original_filename from PDF URL path segment
                    original_filename = None
                    if pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                        if "." in tail and len(tail) <= 200:
                            original_filename = tail

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": authors,
                        "publisher": "Netherlands Institute for Neuroscience",
                        "department": research_group,
                        "journal": journal,
                        "url": detail_url,
                        "pdf_url": pdf_url if pdf_url else None,
                        "keywords": "",
                        "category": research_group,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": year,
                            "originalFilename": original_filename,
                            "research_group": research_group,
                            "publication_year": year,
                            "post_id": post_number,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            # Detect end-of-pagination: no new URLs seen on this page (and not page 1)
            if new_on_page == 0 and page > 1:
                print(f"[{self.site_id}] No new items at page {page}. Done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
