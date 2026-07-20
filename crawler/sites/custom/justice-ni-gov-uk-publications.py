# -*- coding: utf-8 -*-
"""Crawler for justice-ni.gov.uk/publications (Drupal 10, HTML pagination)."""

import json
import re
import subprocess
import time
from urllib.parse import unquote, urlparse

from crawler.base_crawler import BaseCrawler


class JusticeNiPublicationsCrawler(BaseCrawler):
    site_id = "justice-ni-gov-uk-publications"
    site_name = "Custom: justice-ni-gov-uk-publications"
    base_url = "https://www.justice-ni.gov.uk"

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                body = r.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)
        return None

    def _make_soup(self, html: str):
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_iso_date(dt: str) -> str:
        return dt[:10] if dt and len(dt) >= 10 else (dt or "")

    def crawl(self, limit=None):
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        page = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_PAGES = 200
        MAX_WALL = 25 * 60  # 25 minutes

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_WALL:
                print(f"[{self.site_id}] 25-minute wall-clock budget exceeded. Stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = f"{self.base_url}/publications?page={page}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = self._make_soup(raw)
            except Exception as exc:
                print(f"[{self.site_id}] Parse error on page {page}: {exc}. Stopping.")
                break

            if not soup:
                print(f"[{self.site_id}] Could not parse page {page}. Stopping.")
                break

            ul = soup.find("ul", class_="card-deck--search-results")
            if not ul:
                print(f"[{self.site_id}] No results list on page {page}. Stopping.")
                break

            items = ul.find_all("li", recursive=False)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_on_page = 0
            for li in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    a = li.find("a", class_="card")
                    if not a:
                        continue
                    detail_url = a.get("href", "")
                    if not detail_url:
                        continue
                    if not detail_url.startswith("http"):
                        detail_url = self.base_url + detail_url

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    # Extract from list card
                    h3 = li.find("h3", class_="card__title")
                    title = h3.get_text(strip=True) if h3 else a.get_text(strip=True)[:200]
                    if not title:
                        title = "(untitled)"

                    t_el = li.find("time")
                    listed_date = self._parse_iso_date(t_el.get("datetime", "") if t_el else "")

                    summ_el = li.find("p", class_="card__content")
                    card_summary = summ_el.get_text(strip=True) if summ_el else ""

                    cat_el = li.find("span", class_="field-publication-type")
                    category = cat_el.get_text(strip=True) if cat_el else ""

                    slug = urlparse(detail_url).path.rstrip("/").rsplit("/", 1)[-1]

                    # Fetch detail page
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(detail_url)

                    node_id = None
                    meta_desc = ""
                    pdf_url = None
                    original_filename = None
                    published_date = listed_date
                    topics: list = []

                    if detail_raw:
                        try:
                            dsoup = self._make_soup(detail_raw)
                            if dsoup:
                                # Node ID from shortlink
                                sl = dsoup.find("link", {"rel": "shortlink"})
                                if sl:
                                    m = re.search(r"/node/(\d+)", sl.get("href", ""))
                                    if m:
                                        node_id = m.group(1)

                                # Meta description
                                mt = dsoup.find("meta", {"name": "description"})
                                if mt:
                                    meta_desc = mt.get("content", "").strip()

                                # Published date from detail page
                                dt_el = dsoup.find("time")
                                if dt_el:
                                    published_date = self._parse_iso_date(
                                        dt_el.get("datetime", "") or ""
                                    )

                                # Topics / keywords
                                topics_div = dsoup.find(
                                    class_=lambda c: bool(
                                        c and "field--name-field-site-topics" in " ".join(c)
                                    )
                                )
                                if topics_div:
                                    topics = [
                                        lnk.get_text(strip=True)
                                        for lnk in topics_div.find_all("a")
                                        if lnk.get_text(strip=True)
                                    ]

                                # PDF URL — first PDF link in article-content, else anywhere
                                art = dsoup.find("div", class_="article-content")
                                search_scope = art if art else dsoup
                                for lnk in search_scope.find_all("a", href=True):
                                    href = lnk.get("href", "")
                                    if ".pdf" in href.lower() or "/sites/default/files/" in href:
                                        if not href.startswith("http"):
                                            href = self.base_url + href
                                        pdf_url = href
                                        fn = urlparse(href).path.split("/")[-1]
                                        if fn:
                                            original_filename = unquote(fn)
                                        break
                        except Exception as exc:
                            print(
                                f"[{self.site_id}] Detail parse error for {detail_url}: {exc}"
                            )

                    # Build abstract: prefer whichever is longer
                    abstract = meta_desc if len(meta_desc) >= len(card_summary) else card_summary
                    if not abstract:
                        abstract = card_summary or meta_desc

                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] Skipping '{title[:50]}' "
                            f"(abstract {len(abstract)} chars < 100)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": node_id or slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": ",".join(topics) if topics else None,
                        "category": category,
                        "publisher": "Department of Justice Northern Ireland",
                        "authors": None,
                        "department": None,
                        "doi": None,
                        "journal": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "node_id": node_id,
                                "card_summary_raw": card_summary,
                                "publication_type": category,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page}. Stopping.")
                break

            nxt = soup.find("a", rel="next")
            if not nxt:
                print(f"[{self.site_id}] No next-page link on page {page}. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
