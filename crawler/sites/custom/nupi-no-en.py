# -*- coding: utf-8 -*-
"""Crawler for NUPI (Norwegian Institute of International Affairs) – Research Papers (English).

Source:
  https://www.nupi.no/en/publications?pcform[publications_type]=pub_research_paper

Structure:
  - HTML list pages, paginated via &result-page=N
  - Each item: div.list_item_fullW_publication  →  detail page slug
  - Detail page: title in h1.title, abstract in div.publication_summary, metadata list
"""

from __future__ import annotations

import json
import os
import re
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.nupi.no"
_LIST_URL = (
    "https://www.nupi.no/en/publications"
    "?pcform%5Bpublications_type%5D=pub_research_paper"
    "&pcform%5Bfield%5D="
    "&pcform%5Bfrom_date_time%5D="
    "&pcform%5Bto_date_time%5D="
)


def _make_soup(html: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class NupiNoEnCrawler(BaseCrawler):
    site_id = "nupi-no-en"
    site_name = "Custom: nupi-no-en"
    base_url = "https://www.nupi.no"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch(self, url: str, retries: int = 3) -> str | None:
        for attempt in range(retries):
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                try:
                    return resp.text
                except Exception:
                    return resp.content.decode("utf-8", errors="replace")
            except Exception as exc:
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(
                        f"[nupi-no-en] fetch error attempt {attempt+1}/{retries} "
                        f"– {url}: {exc} – retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[nupi-no-en] fetch failed after {retries} attempts: {url}: {exc}")
                    return None

    def _parse_list(self, html: str):
        """Return (items, has_next).

        items = list of (url, date_str, authors_str)
        """
        soup = _make_soup(html)
        if not soup:
            return [], False

        items = []
        for div in soup.find_all("div", class_="list_item_fullW_publication"):
            link = div.find("a", href=re.compile(r"^/en/publications/"))
            if not link:
                continue
            url = _BASE + link["href"]

            time_el = div.find("time")
            date_str = time_el.get("datetime", "") if time_el else ""

            author_tags = div.find_all(["a", "span"], class_="author")
            authors_str = "; ".join(
                t.get_text(strip=True) for t in author_tags if t.get_text(strip=True)
            )

            items.append((url, date_str, authors_str))

        has_next = bool(soup.find("a", rel="next"))
        return items, has_next

    def _parse_detail(self, html: str, url: str) -> dict | None:
        soup = _make_soup(html)
        if not soup:
            return None

        h1 = soup.find("h1", class_="title")
        title = h1.get_text(strip=True) if h1 else ""
        if not title:
            return None

        # Published date from page header
        published_date = ""
        pub_date_div = soup.find("div", class_="published_date")
        if pub_date_div:
            t = pub_date_div.find("time")
            if t:
                published_date = t.get("datetime", "")

        # Summary block contains abstract + metadata list
        summary_div = soup.find("div", class_="publication_summary")
        abstract = ""
        doi = ""
        journal = ""
        pub_year = ""
        full_version_url = None

        if summary_div:
            text_div = summary_div.find("div", class_="eztext-field")
            if text_div:
                abstract = text_div.get_text(separator=" ", strip=True)

            for li in summary_div.find_all("li"):
                li_text = li.get_text()
                if "DOI:" in li_text:
                    a = li.find("a")
                    if a:
                        raw = a.get_text(strip=True)
                        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", raw)
                elif "Journal:" in li_text:
                    sp = li.find("span", class_="ezstring-field")
                    if sp:
                        journal = sp.get_text(strip=True)
                elif "Published year:" in li_text:
                    sp = li.find("span", class_="ezstring-field")
                    if sp:
                        pub_year = sp.get_text(strip=True)
                elif "Full version:" in li_text:
                    a = li.find("a")
                    if a:
                        full_version_url = a.get("href", "")

        if not published_date and pub_year:
            published_date = pub_year

        # Keywords/tags from sidebar
        tags_div = soup.find("div", class_="tags_container")
        keywords = ""
        if tags_div:
            tags = [
                sp.get_text(strip=True)
                for sp in tags_div.find_all("span", class_="tag")
                if sp.get_text(strip=True)
            ]
            keywords = ", ".join(tags)

        # Authors from first person_list_container (same people, less DOM noise)
        authors_str = ""
        person_list = soup.find("div", class_="person_list_container")
        if person_list:
            names = [
                sp.get_text(strip=True)
                for sp in person_list.find_all("span", class_="name")
                if sp.get_text(strip=True)
            ]
            authors_str = "; ".join(names)

        slug = url.rstrip("/").rsplit("/", 1)[-1]

        metadata: dict = {}
        if pub_year:
            metadata["pub_year"] = pub_year
        if full_version_url:
            metadata["full_version_url"] = full_version_url

        return {
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,   # adapter maps this to listed_date in libertree
            "authors": authors_str,
            "publisher": "NUPI",
            "journal": journal,
            "url": url,
            "pdf_url": None,
            "doi": doi,
            "keywords": keywords,
            "category": "Research paper",
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False) if metadata else None,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        MAX_PAGES = 200
        MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        start = time.time()

        saved = 0
        seen_urls: set = set()
        limit_str = str(limit) if limit is not None else "inf"

        for page in range(1, MAX_PAGES + 1):
            if time.time() - start > MAX_WALL_SECS:
                print(f"[nupi-no-en] wall-clock budget reached at page {page}, stopping")
                break

            list_url = _LIST_URL + (f"&result-page={page}" if page > 1 else "")

            if page == 1 or page % 10 == 0:
                print(f"[nupi-no-en] page {page}: saved {saved}/{limit_str}")

            html = self._fetch(list_url)
            if not html:
                print(f"[nupi-no-en] page {page}: fetch failed, stopping")
                break

            items, has_next = self._parse_list(html)
            if not items:
                print(f"[nupi-no-en] page {page}: no items found, stopping")
                break

            new_this_page = 0
            for item_url, listed_date, list_authors in items:
                if limit is not None and saved >= limit:
                    break
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_this_page += 1

                try:
                    time.sleep(self._delay)
                    detail_html = self._fetch(item_url)
                    if not detail_html:
                        print(f"[nupi-no-en] skipping {item_url}: fetch failed")
                        continue

                    paper = self._parse_detail(detail_html, item_url)
                    if not paper:
                        print(f"[nupi-no-en] skipping {item_url}: parse failed")
                        continue

                    if not paper.get("authors") and list_authors:
                        paper["authors"] = list_authors
                    if not paper.get("posted_date") and listed_date:
                        paper["posted_date"] = listed_date
                    if not paper.get("published_date") and listed_date:
                        paper["published_date"] = listed_date

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[nupi-no-en] skipping {item_url}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nupi-no-en] item {item_url} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            if new_this_page == 0:
                print(f"[nupi-no-en] page {page}: all items already seen, stopping")
                break

            if not has_next:
                print(f"[nupi-no-en] page {page}: no next page, done")
                break

            if page == MAX_PAGES:
                print(f"[nupi-no-en] safety cap of {MAX_PAGES} pages reached, stopping")

        print(f"[nupi-no-en] crawl complete: {saved} records saved")
        return saved
