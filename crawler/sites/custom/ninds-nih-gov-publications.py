# -*- coding: utf-8 -*-
"""NINDS NIH Publications crawler.

Starting URL: https://www.ninds.nih.gov/publications
Strategy:
  Phase 1 — walk paginated listing (?page=N, 15/page, ~70 total)
             to collect publication URLs + basic card info.
  Phase 2 — fetch each detail page for the full body, ISO date, topics, PDF.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
)


class NINDSNIHPublicationsCrawler(BaseCrawler):
    site_id = "ninds-nih-gov-publications"
    site_name = "Custom: ninds-nih-gov-publications"
    base_url = "https://www.ninds.nih.gov"

    _ITEMS_PER_PAGE = 15
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch *url* via curl with exponential backoff. Returns text or None."""
        for attempt in range(retries):
            try:
                res = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk",
                        "-A", _UA,
                        "-L", "--max-time", "30",
                        url,
                    ],
                    capture_output=True,
                    timeout=40,
                )
                if res.returncode == 0 and res.stdout:
                    return res.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl error attempt {attempt + 1}/{retries} "
                    f"for {url}: {exc}"
                )
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)  # 1 s, 3 s, 9 s
                print(f"[{self.site_id}] Retrying in {wait}s…")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        """BeautifulSoup with parser fallback chain; returns None on total failure."""
        from bs4 import BeautifulSoup  # type: ignore

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Listing-page parser — collect card data from /publications?page=N
    # ------------------------------------------------------------------

    def _parse_listing(self, html: str) -> list[dict]:
        """Return list of card-dicts: slug, url, title, date_str, description, pdf_url."""
        items: list[dict] = []
        try:
            soup = self._make_soup(html)
            if soup is None:
                return items

            # The publications list lives in the view with display-id page_2
            view = soup.find("div", class_=re.compile(r"view-display-id-page_2"))
            if view is None:
                view = soup  # fallback: scan the whole page

            for row in view.find_all("div", class_=re.compile(r"views-row")):
                try:
                    # Primary anchor: first /publications/<slug> link
                    a = row.find(
                        "a", href=re.compile(r"^/publications/[a-z0-9]")
                    )
                    if a is None:
                        continue
                    href: str = a["href"]
                    slug = href.strip("/").split("/")[-1]
                    if slug in ("publications-help",):
                        continue
                    url = self.base_url + href
                    title = a.get_text(strip=True)

                    # Date from "Publication Date: MM/YYYY" inside <small>
                    date_str = ""
                    small = row.find("small")
                    if small:
                        m = re.search(r"(\d{2}/\d{4})", small.get_text())
                        if m:
                            date_str = m.group(1)

                    # Description: text inside <small>, minus the date label prefix
                    description = ""
                    if small:
                        text = small.get_text(separator=" ", strip=True)
                        text = re.sub(
                            r"Publication\s+Date:\s*\d{2}/\d{4}", "", text
                        ).strip()
                        text = re.sub(
                            r"Download\s+PDF.*$", "", text, flags=re.S
                        ).strip()
                        description = text

                    # PDF link directly on the card
                    pdf_url = ""
                    pdf_a = row.find("a", href=re.compile(r"\.pdf", re.I))
                    if pdf_a:
                        ph: str = pdf_a["href"]
                        pdf_url = ph if ph.startswith("http") else self.base_url + ph

                    items.append(
                        {
                            "slug": slug,
                            "url": url,
                            "title": title,
                            "date_str": date_str,
                            "description": description,
                            "pdf_url": pdf_url,
                        }
                    )
                except Exception as exc:
                    print(f"[{self.site_id}] row parse error: {exc}")
                    continue
        except Exception as exc:
            print(f"[{self.site_id}] listing parse error: {exc}")
        return items

    # ------------------------------------------------------------------
    # Detail-page parser — extract full body, date, topics, PDF
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str) -> dict:
        """Return dict with abstract, published_date, keywords, category, pdf_url, title."""
        result: dict = {}
        try:
            soup = self._make_soup(html)
            if soup is None:
                return result

            # Title (may be richer than the card title)
            title_div = soup.find(
                "div", class_=re.compile(r"field--name-title")
            )
            if title_div:
                result["title"] = title_div.get_text(strip=True)
            else:
                h1 = soup.find("h1")
                if h1:
                    result["title"] = h1.get_text(strip=True)

            # Full abstract / body text
            body_div = soup.find(
                "div", class_=re.compile(r"field--name-body")
            )
            if body_div:
                result["abstract"] = body_div.get_text(separator=" ", strip=True)

            # Publication date from <time datetime="YYYY-MM-DDT…">
            date_div = soup.find(
                "div",
                class_=re.compile(r"field--name-field-cecc-publication-date"),
            )
            if date_div:
                t = date_div.find("time")
                if t and t.get("datetime"):
                    result["published_date"] = t["datetime"][:10]

            # Topics / keywords from field--name-field-topic
            topics_div = soup.find(
                "div", class_=re.compile(r"field--name-field-topic")
            )
            if topics_div:
                topics = [
                    d.get_text(strip=True)
                    for d in topics_div.find_all("div", class_="field__item")
                ]
                result["keywords"] = json.dumps(topics)
                result["category"] = topics[0] if topics else ""

            # PDF download link
            pdf_a = soup.find("a", class_=re.compile(r"pdf-download"))
            if pdf_a is None:
                pdf_a = soup.find("a", href=re.compile(r"\.pdf", re.I))
            if pdf_a:
                ph: str = pdf_a["href"]
                result["pdf_url"] = (
                    ph if ph.startswith("http") else self.base_url + ph
                )

        except Exception as exc:
            print(f"[{self.site_id}] detail parse error for {url}: {exc}")
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        start_time = time.time()
        limit_val = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: set[str] = set()
        all_items: list[dict] = []

        # ── Phase 1: walk listing pages ───────────────────────────────
        total_known: int | None = None

        for page in range(self._MAX_PAGES):
            elapsed = time.time() - start_time
            if elapsed > self._WALL_BUDGET:
                print(
                    f"[{self.site_id}] wall-clock budget exhausted during "
                    "listing collection, stopping."
                )
                break

            list_url = f"{self.base_url}/publications?page={page}"
            html = self._curl_get(list_url)
            if not html:
                print(
                    f"[{self.site_id}] failed to fetch listing page {page}, stopping."
                )
                break

            # Parse total count from "Displaying X - Y of Z"
            if total_known is None:
                m = re.search(r"Displaying \d+ - \d+ of (\d+)", html)
                if m:
                    total_known = int(m.group(1))

            items = self._parse_listing(html)
            new_items = [it for it in items if it["url"] not in seen_urls]

            if not new_items:
                print(
                    f"[{self.site_id}] no new items on page {page}, "
                    "pagination complete."
                )
                break

            for it in new_items:
                seen_urls.add(it["url"])
                all_items.append(it)

            if page % 10 == 0:
                print(
                    f"[{self.site_id}] page {page}: "
                    f"collected {len(all_items)} URLs so far"
                )

            # Stop collecting listing pages early if we already have enough
            if limit is not None and len(all_items) >= limit:
                break

            # Detect last page from total
            if total_known is not None:
                max_page = (total_known - 1) // self._ITEMS_PER_PAGE
                if page >= max_page:
                    break

            time.sleep(self._delay)

        print(
            f"[{self.site_id}] collected {len(all_items)} publication URLs"
        )

        # ── Phase 2: fetch detail pages and save ──────────────────────
        to_fetch = all_items if limit is None else all_items[:limit]

        for idx, item in enumerate(to_fetch):
            if time.time() - start_time > self._WALL_BUDGET:
                print(
                    f"[{self.site_id}] wall-clock budget exhausted at "
                    f"{saved} saved, stopping."
                )
                break

            url = item["url"]
            slug = item["slug"]

            try:
                detail_html = self._curl_get(url)
                if not detail_html:
                    print(
                        f"[{self.site_id}] item {url} failed: "
                        "curl returned None, skipping"
                    )
                    continue

                detail = self._parse_detail(detail_html, url)

                title = detail.get("title") or item["title"]
                abstract = detail.get("abstract") or item["description"]

                # Normalise published_date to YYYY-MM-DD
                published_date = detail.get("published_date", "")
                if not published_date and item["date_str"]:
                    dm = re.match(r"(\d{2})/(\d{4})", item["date_str"])
                    if dm:
                        published_date = f"{dm.group(2)}-{dm.group(1)}-01"

                pdf_url = detail.get("pdf_url") or item["pdf_url"]
                keywords = detail.get("keywords", "[]")
                category = detail.get("category", "")

                if not title:
                    print(f"[{self.site_id}] item {url}: no title, skipping")
                    continue
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] item {url}: abstract too short "
                        f"({len(abstract)} chars), skipping"
                    )
                    continue

                paper = {
                    "id": f"{self.site_id}:{slug}",
                    "site_id": self.site_id,
                    "external_id": slug,
                    "title": title,
                    "authors": json.dumps([]),
                    "abstract": abstract,
                    "category": category,
                    "keywords": keywords,
                    "published_date": published_date,
                    "url": url,
                    "pdf_url": pdf_url,
                    "doi": "",
                    "department": (
                        "National Institute of Neurological Disorders and Stroke"
                    ),
                    "metadata": json.dumps(
                        {
                            "topics": (
                                json.loads(keywords) if keywords else []
                            ),
                            "source": "ninds.nih.gov/publications",
                        }
                    ),
                }

                self._save_paper(paper)
                saved += 1

                if saved % 10 == 0:
                    print(
                        f"[{self.site_id}] page {page}: "
                        f"saved {saved}/{limit_val}"
                    )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {url} failed: {exc}")
                continue

            time.sleep(self._delay)

        print(f"[{self.site_id}] done: saved {saved} publications")
        return saved
