# -*- coding: utf-8 -*-
"""Crawler for SLAC National Accelerator Laboratory – News Releases archive."""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "www6-slac-stanford-edu-news-and-events"

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _parse_date(raw: str) -> str:
    """'March 31, 2026' -> '2026-03-31'. Returns raw string on parse failure."""
    m = re.search(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", (raw or "").strip(), re.IGNORECASE)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return f"{m.group(3)}-{month}-{m.group(2).zfill(2)}"
    return (raw or "").strip()


def _strip_html(html_text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(html: str):
    """Return BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class SLACNewsCrawler(BaseCrawler):
    """Crawler for SLAC National Accelerator Laboratory news releases."""

    site_id = _SITE_ID
    site_name = "Custom: www6-slac-stanford-edu-news-and-events"
    base_url = "https://www6.slac.stanford.edu"

    _ARCHIVE_URL = "https://www6.slac.stanford.edu/news-and-events/news-center/archive"
    _ARCHIVE_PARAMS = "created=&news_research_area=All&news_type=1"
    _MAX_PAGES = 200
    _WALL_BUDGET = 25 * 60  # 25 minutes in seconds

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential backoff (1s, 3s, 9s)."""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                        "-L",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                        "-H", "Accept-Language: en-US,en;q=0.9",
                        url,
                    ],
                    capture_output=True,
                    timeout=40,
                )
                if result.stdout:
                    try:
                        return result.stdout.decode("utf-8", errors="replace")
                    except Exception:
                        return result.stdout.decode("latin-1", errors="replace")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt + 1}: {exc}")

            if attempt < 2:
                wait = 3 ** attempt  # 1s, 3s
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return list of card dicts from a list page."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error on list page: {exc}")
            return []
        if soup is None:
            return []

        cards = []
        for card_div in soup.find_all("div", class_="c-card--teaser"):
            try:
                node_id = card_div.get("data-history-node-id", "").strip()
                about = card_div.get("about", "").strip()

                # Only actual news articles (path starts with /news/)
                if not about or not about.startswith("/news/"):
                    continue

                # Title from <span> inside the anchor
                title_el = card_div.find("h3") or card_div.find("h2") or card_div
                span = title_el.find("span") if title_el else None
                title = span.get_text(strip=True) if span else ""
                if not title:
                    a_el = card_div.find("a", class_="c-card__link")
                    title = a_el.get_text(strip=True) if a_el else ""

                # Article URL
                a_el = card_div.find("a", class_="c-card__link")
                href = (a_el.get("href") or "").strip() if a_el else about
                url = self.base_url + href if href.startswith("/") else href

                # Teaser (fallback abstract)
                teaser_div = card_div.find(
                    "div", class_=re.compile(r"field--name-field-teaser")
                )
                teaser = _strip_html(str(teaser_div)) if teaser_div else ""

                # Date from footer
                footer = card_div.find("div", class_="c-card__footer")
                date_raw = footer.get_text(strip=True) if footer else ""
                date_clean = re.split(r"[·&·]", date_raw)[0].strip()
                published_date = _parse_date(date_clean)

                cards.append({
                    "node_id": node_id,
                    "about": about,
                    "url": url,
                    "title": title,
                    "teaser": teaser,
                    "published_date": published_date,
                    "date_raw": date_raw,
                })
            except Exception as exc:
                print(f"[{self.site_id}] card parse error: {exc}")
                continue

        return cards

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict:
        """Fetch and parse a news article detail page."""
        time.sleep(self._delay)
        html = self._curl_get(url)
        if not html:
            return {}

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] detail soup error for {url}: {exc}")
            return {}
        if soup is None:
            return {}

        # Published date from kicker
        date_el = soup.find("div", class_=re.compile(r"c-kicker--big"))
        pub_date = _parse_date(date_el.get_text(strip=True)) if date_el else ""

        # Lede
        lede_div = soup.find("div", class_="c-lede")
        lede = _strip_html(str(lede_div)) if lede_div else ""

        # Author: look for <p>By <a>Name</a></p> pattern in header area
        author = ""
        header_el = soup.find("header", class_=re.compile("c-page-title"))
        search_root = header_el if header_el else soup
        for p in search_root.find_all("p"):
            txt = p.get_text(strip=True)
            if txt.startswith("By "):
                author = txt[3:].strip()
                break

        # Body: collect all c-field--name-field-body divs
        body_parts = []
        for body_div in soup.find_all("div", class_=re.compile(r"field--name-field-body")):
            inner = body_div.find("div", class_=re.compile(r"c-field__content"))
            text = _strip_html(str(inner or body_div))
            if text and len(text) > 30:
                body_parts.append(text)

        body = "\n\n".join(dict.fromkeys(body_parts))  # dedup while preserving order

        # Keywords from tag links (best-effort)
        keywords = []
        for a in soup.find_all("a", class_=re.compile(r"tag|topic|keyword", re.I)):
            kw = a.get_text(strip=True)
            if kw and kw not in keywords:
                keywords.append(kw)

        return {
            "published_date": pub_date,
            "lede": lede,
            "author": author,
            "body": body,
            "keywords": keywords,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Paginate through the SLAC news archive and save articles.

        Pagination: ``?page=0``, ``?page=1``, … (10 items per page).
        Stops when: saved >= limit, empty page, all seen (dedup), 200 pages,
        or 25-minute wall-clock budget exhausted.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        for page_num in range(self._MAX_PAGES):
            # Limit check
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._WALL_BUDGET:
                print(f"[{self.site_id}] Wall-clock budget ({self._WALL_BUDGET}s) exceeded, stopping.")
                break

            # Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            # Safety cap log
            if page_num == self._MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")

            list_url = f"{self._ARCHIVE_URL}?{self._ARCHIVE_PARAMS}&page={page_num}"
            html = self._curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page_num}, stopping.")
                break

            cards = self._parse_list_page(html)
            if not cards:
                print(f"[{self.site_id}] No articles on page {page_num}, done.")
                break

            # Dedup: skip cards whose URL we already saw
            new_cards = [c for c in cards if c["url"] not in seen_urls]
            if not new_cards:
                print(f"[{self.site_id}] All cards on page {page_num} already seen, done.")
                break
            for c in new_cards:
                seen_urls.add(c["url"])

            for card in new_cards:
                if limit is not None and saved >= limit:
                    break

                try:
                    detail = self._fetch_detail(card["url"])

                    # Build abstract from lede + body
                    parts = []
                    lede = (detail.get("lede") or "").strip()
                    body = (detail.get("body") or "").strip()
                    if lede:
                        parts.append(lede)
                    if body and body != lede:
                        parts.append(body)
                    abstract = "\n\n".join(parts).strip()

                    # Fallback: use teaser from list page
                    if len(abstract) < 50:
                        abstract = (card.get("teaser") or "").strip()

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping (abstract {len(abstract)} chars): "
                            f"{card['title'][:60]}"
                        )
                        continue

                    pub_date = (detail.get("published_date") or "").strip() or card["published_date"]
                    listed_date = card["published_date"]
                    author = (detail.get("author") or "").strip()
                    keywords_list = detail.get("keywords") or []
                    node_id = card["node_id"]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": node_id,
                        "post_number": node_id,
                        "title": card["title"],
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "url": card["url"],
                        "pdf_url": None,
                        "authors": author,
                        "publisher": "SLAC National Accelerator Laboratory",
                        "department": "",
                        "journal": "",
                        "doi": "",
                        "keywords": ",".join(keywords_list) if keywords_list else None,
                        "category": "News Release",
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "node_id": node_id,
                                "about": card["about"],
                                "date_raw": card.get("date_raw", ""),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {card['title'][:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {card.get('url', '?')} failed: {exc}")
                    continue

            # Polite pause between list pages
            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
