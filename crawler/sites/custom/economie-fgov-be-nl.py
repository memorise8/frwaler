# -*- coding: utf-8 -*-
"""Crawler for FOD Economie (economie.fgov.be) NL publications.

Starting URL: https://economie.fgov.be/nl/publicaties
Bot protection: TSPD challenge — uses a single Playwright stealth session.
Listing: 6 items/page, ?page=0..100 (~606 items total).
Detail: fetched only when listing teaser < 100 chars, for full abstract.
"""

import json
import re
import sys
import time
from pathlib import Path

_CRAWLER_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_BASE_URL = "https://economie.fgov.be"
_LIST_URL = f"{_BASE_URL}/nl/publicaties"


class EconomieFgovBeNlCrawler(BaseCrawler):
    """Crawler for FOD Economie NL publications (TSPD-protected Drupal 10)."""

    site_id = "economie-fgov-be-nl"
    site_name = "Custom: economie-fgov-be-nl"
    base_url = "https://economie.fgov.be"

    _MAX_PAGES = 200
    _MAX_WALL_SECS = 25 * 60  # 25-minute budget

    # ------------------------------------------------------------------
    # BeautifulSoup helper
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html):
        """html5lib → lxml → html.parser fallback."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Playwright page management
    # ------------------------------------------------------------------

    @staticmethod
    def _is_real_page(html):
        """True if html has real Drupal NL content (not a bare TSPD challenge)."""
        return 'lang="nl"' in html and 'field--name' in html

    def _fetch_page(self, page_obj, url, retries=3):
        """Navigate to url using the shared Playwright page. Returns HTML or None."""
        waits = [1, 3, 9]
        for attempt in range(retries):
            try:
                try:
                    page_obj.goto(url, wait_until="networkidle", timeout=45000)
                except Exception:
                    # networkidle timeout is common; continue and check content
                    pass
                time.sleep(2)
                html = page_obj.content()
                if self._is_real_page(html):
                    return html
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] challenge not cleared "
                      f"(attempt {attempt+1}/{retries}), waiting {wait}s: {url}")
                time.sleep(wait)
            except Exception as exc:
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] fetch error "
                      f"(attempt {attempt+1}/{retries}): {exc}")
                if attempt < retries - 1:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(el):
        """Normalised plain text from a BS4 tag or None."""
        if el is None:
            return ""
        return re.sub(r"\s+", " ", el.get_text(separator=" ", strip=True)).strip()

    @staticmethod
    def _parse_date(s):
        """Extract YYYY-MM-DD from ISO or raw datetime string."""
        if not s:
            return ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s.strip())
        return m.group(1) if m else ""

    # ------------------------------------------------------------------
    # Listing page parser
    # ------------------------------------------------------------------

    def _parse_listing(self, html):
        """Return list of item-dicts from one listing page."""
        soup = self._make_soup(html)
        if not soup:
            return []
        items = []
        for li in soup.select("li.media"):
            try:
                title_el = li.select_one(".field--name-node-title a")
                if not title_el:
                    continue
                title = self._clean(title_el)
                rel_path = title_el.get("href", "")
                if not rel_path:
                    continue
                detail_url = _BASE_URL + rel_path if rel_path.startswith("/") else rel_path
                slug = rel_path.rstrip("/").rsplit("/", 1)[-1]

                date_el = li.select_one("time[datetime]")
                listed_date = self._parse_date(
                    date_el.get("datetime", "") if date_el else ""
                )

                # Prefer body with field--parent-type-node (not site notice)
                teaser = ""
                for b in li.select(".field--name-body"):
                    cls = b.get("class", [])
                    if "field--parent-type-node" in cls:
                        teaser = self._clean(b)
                        break
                if not teaser:
                    # Fallback: longest body field on this item
                    for b in li.select(".field--name-body"):
                        txt = self._clean(b)
                        if len(txt) > len(teaser):
                            teaser = txt

                dl_el = li.select_one(".field--name-field-downloads a[href]")
                dl_href = dl_el.get("href", "") if dl_el else ""
                dl_text = self._clean(dl_el) if dl_el else ""

                items.append({
                    "title": title,
                    "slug": slug,
                    "detail_url": detail_url,
                    "listed_date": listed_date,
                    "teaser": teaser,
                    "dl_href": dl_href,
                    "dl_text": dl_text,
                })
            except Exception as exc:
                print(f"[{self.site_id}] listing item parse error: {exc}")
                continue
        return items

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, html, item):
        """Extract full abstract, authors, publisher, and PDF info from detail page."""
        soup = self._make_soup(html)
        if not soup:
            return {}

        # Full abstract: body field that belongs to the publication node
        abstract = ""
        for b in soup.select(".field--name-body"):
            cls = b.get("class", [])
            if "field--parent-type-node" in cls:
                txt = self._clean(b)
                if len(txt) > len(abstract):
                    abstract = txt
        if not abstract:
            for b in soup.select(".field--name-body"):
                txt = self._clean(b)
                if len(txt) > len(abstract):
                    abstract = txt

        # Publication date
        pub_date = item.get("listed_date", "")
        date_el = soup.select_one(".field--name-field-publication-date time[datetime]")
        if date_el:
            d = self._parse_date(date_el.get("datetime", ""))
            if d:
                pub_date = d

        # Authors — strip "Auteur(s)" label
        authors = ""
        authors_el = soup.select_one(".field--name-field-authors")
        if authors_el:
            txt = self._clean(authors_el)
            txt = re.sub(r"^Auteur\(s\)\s*", "", txt)
            authors = txt

        # Publisher — strip "Uitgever" label
        publisher = ""
        pub_el = soup.select_one(".field--name-field-publisher")
        if pub_el:
            txt = self._clean(pub_el)
            txt = re.sub(r"^Uitgever\s*", "", txt)
            publisher = txt

        # Download link (detail page has the actual filename in link text)
        dl_el = soup.select_one(".field--name-field-downloads a[href]")
        dl_href = dl_el.get("href", "") if dl_el else item.get("dl_href", "")
        dl_text = self._clean(dl_el) if dl_el else item.get("dl_text", "")

        # Category from body CSS class (e.g. "term--consumer-protection")
        category = ""
        body_el = soup.find("body")
        if body_el:
            for cls in body_el.get("class", []):
                if cls.startswith("term--"):
                    category = cls[len("term--"):].replace("-", " ").title()
                    break

        return {
            "abstract": abstract,
            "pub_date": pub_date,
            "authors": authors,
            "publisher": publisher,
            "dl_href": dl_href,
            "dl_text": dl_text,
            "category": category,
        }

    # ------------------------------------------------------------------
    # Filename extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_filename(dl_href, dl_text):
        """Extract original PDF filename from detail-page link text or URL path."""
        # Detail page text: "Something-Report.pdf (Other, 5.3 MB)"
        m = re.match(r"(.+?\.(?:pdf|PDF))\s*(?:\(|$)", dl_text)
        if m:
            return m.group(1).strip()
        # Last URL segment if it looks like a file
        path = dl_href.split("?")[0].rstrip("/")
        seg = path.rsplit("/", 1)[-1]
        if "." in seg and not seg.startswith("download"):
            return seg
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth
        except ImportError as exc:
            print(f"[{self.site_id}] Missing dependency: {exc}")
            return 0

        saved = 0
        page_num = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"
        ua = self.USER_AGENT

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = browser.new_context(
                user_agent=ua,
                locale="nl-BE",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "nl-BE,nl;q=0.9,fr;q=0.8,en;q=0.7",
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                },
            )
            pw_page = ctx.new_page()
            Stealth(navigator_user_agent_override=ua).apply_stealth_sync(pw_page)

            try:
                while True:
                    if time.time() - start_time > self._MAX_WALL_SECS:
                        print(
                            f"[{self.site_id}] Wall-clock budget exceeded "
                            f"at page {page_num}, stopping."
                        )
                        break

                    if limit is not None and saved >= limit:
                        break

                    if page_num >= self._MAX_PAGES:
                        print(
                            f"[{self.site_id}] Safety cap of {self._MAX_PAGES} "
                            f"pages reached, stopping."
                        )
                        break

                    list_url = f"{_LIST_URL}?page={page_num}"
                    html = self._fetch_page(pw_page, list_url)
                    if not html:
                        print(
                            f"[{self.site_id}] Failed to fetch listing page "
                            f"{page_num}, stopping."
                        )
                        break

                    items = self._parse_listing(html)
                    if not items:
                        print(
                            f"[{self.site_id}] No items on page {page_num}, done."
                        )
                        break

                    new_on_page = 0
                    for item in items:
                        if limit is not None and saved >= limit:
                            break

                        detail_url = item["detail_url"]
                        if detail_url in seen_urls:
                            continue
                        seen_urls.add(detail_url)
                        new_on_page += 1

                        try:
                            abstract = item["teaser"]
                            pub_date = item["listed_date"]
                            listed_date = item["listed_date"]
                            authors = ""
                            publisher = "FOD Economie"
                            dl_href = item["dl_href"]
                            dl_text = item["dl_text"]
                            category = ""
                            original_filename = None

                            # Fetch detail when teaser is short
                            if len(abstract) < 100:
                                time.sleep(self._delay)
                                detail_html = self._fetch_page(pw_page, detail_url)
                                if detail_html:
                                    d = self._parse_detail(detail_html, item)
                                    if d.get("abstract"):
                                        abstract = d["abstract"]
                                    if d.get("pub_date"):
                                        pub_date = d["pub_date"]
                                    authors = d.get("authors", "")
                                    publisher = d.get("publisher") or "FOD Economie"
                                    dl_href = d.get("dl_href") or dl_href
                                    dl_text = d.get("dl_text") or dl_text
                                    category = d.get("category", "")
                                    original_filename = self._extract_filename(
                                        dl_href, dl_text
                                    )

                            if len(abstract) < 50:
                                print(
                                    f"[{self.site_id}] Short abstract "
                                    f"({len(abstract)} chars) for "
                                    f"{item['slug']}, skipping."
                                )
                                continue

                            # Build absolute PDF URL
                            pdf_url = None
                            if dl_href:
                                pdf_url = (
                                    _BASE_URL + dl_href
                                    if dl_href.startswith("/")
                                    else dl_href
                                )

                            paper = {
                                "site_id": self.site_id,
                                "external_id": item["slug"],
                                "post_number": item["slug"],
                                "title": item["title"],
                                "abstract": abstract,
                                "published_date": pub_date,
                                "posted_date": listed_date,
                                "url": detail_url,
                                "pdf_url": pdf_url,
                                "original_filename": original_filename,
                                "authors": authors,
                                "publisher": publisher,
                                "department": "",
                                "journal": "",
                                "keywords": "",
                                "category": category,
                                "doi": None,
                                "metadata": json.dumps(
                                    {
                                        "posted_date": listed_date,
                                        "slug": item["slug"],
                                        "dl_href": dl_href,
                                        "originalFilename": original_filename,
                                    },
                                    ensure_ascii=False,
                                ),
                            }

                            self._save_paper(paper)
                            saved += 1
                            print(
                                f"[{self.site_id}] saved {saved}/{limit_str}: "
                                f"{item['title'][:60]}"
                            )

                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            print(
                                f"[{self.site_id}] item "
                                f"{item.get('slug', '?')} failed: {exc}"
                            )
                            continue

                    if page_num % 10 == 0:
                        print(
                            f"[{self.site_id}] page {page_num}: "
                            f"saved {saved}/{limit_str}"
                        )

                    if new_on_page == 0:
                        print(
                            f"[{self.site_id}] All items on page {page_num} "
                            f"already seen, done."
                        )
                        break

                    page_num += 1

            except KeyboardInterrupt:
                print(
                    f"[{self.site_id}] KeyboardInterrupt at page {page_num} — "
                    f"stopping."
                )
                raise
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
