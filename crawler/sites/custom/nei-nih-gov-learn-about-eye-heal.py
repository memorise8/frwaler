# -*- coding: utf-8 -*-
"""Crawler for NEI NIH Outreach Materials.

Target: https://www.nei.nih.gov/about/education-and-outreach/outreach-materials
Listing: paginated HTML, ?page=N (0-indexed), 10 items per page, ~9 pages total.
Detail: each item has a dedicated page with title, description, material type, audience, PDF link.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "nei-nih-gov-learn-about-eye-heal"
_BASE_URL = "https://www.nei.nih.gov"
_LIST_PATH = "/about/education-and-outreach/outreach-materials"
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 100
_WALL_CLOCK_LIMIT = 25 * 60  # seconds


class NeiNihGovLearnAboutEyeHealCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: nei-nih-gov-learn-about-eye-heal"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=40):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                )
                if result.returncode == 0 and result.stdout:
                    try:
                        return result.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return result.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error (attempt {attempt}/{len(waits)}) {url}: {exc}")
            if attempt < len(waits):
                time.sleep(wait)
        print(f"[{_SITE_ID}] all retries exhausted for {url}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str | None):
        if not html:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_date(pdf_url: str | None) -> str | None:
        """Pull YYYY-MM from a Drupal files URL like /sites/default/files/2019-06/..."""
        if not pdf_url:
            return None
        m = re.search(r"/(\d{4}-\d{2})/", pdf_url)
        return m.group(1) + "-01" if m else None

    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------

    def _get_listing_items(self, page: int) -> list[tuple[str, str | None]]:
        """Return [(href, node_id_or_None), ...] for one listing page."""
        url = f"{_BASE_URL}{_LIST_PATH}?page={page}"
        html = self._curl(url)
        soup = self._make_soup(html)
        if not soup:
            return []

        items: list[tuple[str, str | None]] = []

        # Primary: teaser divs carry the node ID
        teasers = soup.find_all(
            "div",
            class_=lambda c: c and "outreach-materials-teaser" in " ".join(c),
        )
        if teasers:
            for t in teasers:
                link = t.find("a", class_="c-teaser__link") or t.find("a", href=True)
                if not link:
                    continue
                href = link.get("href", "").strip()
                if not href or _LIST_PATH + "/" not in href:
                    continue
                node_id = t.get("data-history-node-id")
                items.append((href, node_id))
        else:
            # Fallback: any link pointing to a detail page
            for a in soup.find_all("a", href=re.compile(rf"{re.escape(_LIST_PATH)}/[^?#]+")):
                href = a["href"].strip()
                items.append((href, None))

        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _parse_detail(self, href: str, node_id: str | None) -> dict | None:
        """Fetch and parse a detail page. Returns paper dict or None."""
        item_url = (_BASE_URL + href) if href.startswith("/") else href
        html = self._curl(item_url)
        soup = self._make_soup(html)
        if not soup:
            return None

        # Title
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        if not title:
            return None

        # Abstract — body-wrapper div
        body_div = soup.find("div", class_="body-wrapper")
        abstract = body_div.get_text(separator=" ", strip=True) if body_div else ""

        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{_SITE_ID}] skip {href}: abstract too short ({len(abstract)} chars)")
            return None

        # Material type — second c-item__meta--pub-type div (first has the label)
        material_type = ""
        for div in soup.find_all("div", class_="c-item__meta--pub-type"):
            if not div.find(class_="om-term-label"):
                material_type = div.get_text(strip=True)
                break

        # Audience — second c-item__meta--audience div
        audience = ""
        for div in soup.find_all("div", class_="c-item__meta--audience"):
            if not div.find(class_="om-term-label"):
                audience = "; ".join(
                    s.strip() for s in div.get_text(separator="\n").split("\n") if s.strip()
                )
                break

        # First PDF link (prefer English)
        pdf_url = None
        file_list = soup.find("ul", class_="om-lang-file-list")
        if file_list:
            first_a = file_list.find("a", href=True)
            if first_a:
                ph = first_a["href"].strip()
                pdf_url = (_BASE_URL + ph) if ph.startswith("/") else ph

        # Date from PDF URL date component
        published_date = self._extract_date(pdf_url)

        # External ID: prefer node_id from listing, else slug
        if not node_id:
            body_tag = soup.find("body")
            if body_tag:
                body_cls = " ".join(body_tag.get("class", []))
                m = re.search(r"node--(\d+)", body_cls)
                if m:
                    node_id = m.group(1)
        if not node_id:
            node_id = href.rstrip("/").split("/")[-1]

        return {
            "site_id": self.site_id,
            "external_id": str(node_id),
            "title": title,
            "authors": "[]",
            "abstract": abstract,
            "category": material_type,
            "keywords": "[]",
            "published_date": published_date,
            "url": item_url,
            "pdf_url": pdf_url,
            "doi": None,
            "department": "National Eye Institute",
            "metadata": json.dumps(
                {"audience": audience, "material_type": material_type},
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"
        limit_val = limit if limit is not None else float("inf")
        start_time = time.time()

        for page in range(_MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > _WALL_CLOCK_LIMIT:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached, stopping.")
                break

            if saved >= limit_val:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            items = self._get_listing_items(page)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no items returned, stopping.")
                break

            new_items = [(h, nid) for h, nid in items if h not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] page {page}: all items already seen, stopping.")
                break

            for href, node_id in new_items:
                if saved >= limit_val:
                    break
                seen_urls.add(href)

                try:
                    paper = self._parse_detail(href, node_id)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}: {paper['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {href} failed: {exc}")
                    continue

                time.sleep(self._delay)

        if page >= _MAX_PAGES - 1:
            print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{_SITE_ID}] crawl done: {saved} items saved.")
        return saved
