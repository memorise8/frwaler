# -*- coding: utf-8 -*-
"""HEC Paris Press Release crawler (hec-edu-en).

Targets: https://www.hec.edu/en/list-press-release
API:     Drupal Views AJAX at /en/views/ajax (POST, page=0,1,2…)
"""

import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin

import sys
sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler


def _make_soup(html):
    """BeautifulSoup with fallback parser chain."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available (install html5lib or lxml)")


class HecEduEnCrawler(BaseCrawler):
    site_id = "hec-edu-en"
    site_name = "Custom: hec-edu-en"
    base_url = "https://www.hec.edu"

    _LIST_URL = "https://www.hec.edu/en/list-press-release"
    _AJAX_URL = "https://www.hec.edu/en/views/ajax"
    _DEFAULT_DOM_ID = "a68c261bee1d10e75826a6485887cb20c84190b3bab8991f44584b6d2ac4bc8b"

    # ------------------------------------------------------------------ #
    # Low-level curl helpers                                               #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url, retries=3):
        """GET via curl; returns decoded text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[hec-edu-en] GET empty response, retry in {wait}s: {url}")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[hec-edu-en] GET error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[hec-edu-en] GET failed after {retries} attempts: {url}")
        return None

    def _ajax_post(self, page, view_dom_id, retries=3):
        """POST to Drupal Views AJAX; returns raw response text or None."""
        post_data = (
            "view_name=press_release_list"
            "&view_display_id=listing"
            "&view_args="
            "&view_path=%2Fen%2Flist-press-release"
            "&view_base_path=list-press-release"
            f"&view_dom_id={view_dom_id}"
            "&pager_element=0"
            f"&page={page}"
        )
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", "X-Requested-With: XMLHttpRequest",
            "-H", f"Referer: {self._LIST_URL}",
            "-A", self.USER_AGENT,
            "--data", post_data,
            self._AJAX_URL,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        text = raw.decode("utf-8", errors="replace")
                    if text.strip():
                        return text
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[hec-edu-en] AJAX empty response page {page}, retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[hec-edu-en] AJAX error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[hec-edu-en] AJAX failed after {retries} attempts")
        return None

    # ------------------------------------------------------------------ #
    # DOM-ID extraction                                                    #
    # ------------------------------------------------------------------ #

    def _get_dom_id(self):
        """Fetch list page and extract current view_dom_id from Drupal settings."""
        raw = self._curl_get(self._LIST_URL)
        if raw:
            m = re.search(r'"view_dom_id"\s*:\s*"([a-f0-9]{10,})"', raw)
            if m:
                return m.group(1)
        return self._DEFAULT_DOM_ID

    # ------------------------------------------------------------------ #
    # HTML parsers                                                         #
    # ------------------------------------------------------------------ #

    def _parse_cards(self, html):
        """Return (list_of_card_dicts, has_next_page) from AJAX HTML fragment."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[hec-edu-en] soup error in _parse_cards: {exc}")
            return [], False

        cards = []
        for card in soup.find_all("div", class_="news-item"):
            try:
                link_tag = card.find("a", rel="bookmark")
                if not link_tag or not link_tag.get("href"):
                    continue
                url = urljoin(self.base_url, link_tag["href"])

                # Title
                title_span = card.find("span", class_="h4")
                if title_span:
                    inner = title_span.find("span")
                    title = (inner or title_span).get_text(strip=True)
                else:
                    title = link_tag.get_text(strip=True)

                # Short description from list card
                desc = ""
                desc_div = card.find("div", class_="news-item__description")
                if desc_div:
                    p_tag = desc_div.find("p")
                    if p_tag:
                        desc = p_tag.get_text(strip=True)

                # Date
                date_str = ""
                time_tag = card.find("time")
                if time_tag:
                    dt = time_tag.get("datetime", "")
                    date_str = dt[:10] if dt else time_tag.get_text(strip=True)

                # Category
                cartridge = card.find("div", class_="cartridge")
                category = cartridge.get_text(strip=True) if cartridge else ""

                cards.append({
                    "url": url,
                    "title": title,
                    "description": desc,
                    "date": date_str,
                    "category": category,
                })
            except Exception as exc:
                print(f"[hec-edu-en] card parse error: {exc}")
                continue

        has_next = bool(soup.find("a", href=lambda h: h and "page=" in h))
        return cards, has_next

    def _fetch_detail(self, url):
        """Fetch detail page; return dict with node_id, abstract, pdf_url, etc."""
        raw = self._curl_get(url)
        if not raw:
            return {}

        result = {}

        # Node ID — first /node/NNNN in the page
        node_ids = re.findall(r'/node/(\d+)', raw)
        if node_ids:
            result["node_id"] = node_ids[0]

        # Meta description (concise summary, always present on HEC pages)
        m = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', raw)
        if m:
            result["meta_desc"] = m.group(1)

        # PDF URL (S3 or any .pdf link)
        pdfs = re.findall(r'href="([^"]*\.pdf[^"]*)"', raw, re.IGNORECASE)
        if pdfs:
            result["pdf_url"] = pdfs[0]

        # Date from <time datetime="…">
        dt_vals = re.findall(r'datetime="([^"]+)"', raw)
        if dt_vals:
            result["detail_date"] = dt_vals[0][:10]

        # Body paragraphs via BeautifulSoup
        try:
            soup = _make_soup(raw)
            body_parts = []
            for p in soup.find_all("p"):
                text = p.get_text(separator=" ", strip=True)
                # Skip address/footer noise
                if len(text) > 50 and not re.search(
                    r'(?:Maps and directions|Jouy-en-Josas|cookie|matomo|'
                    r'privacy policy|terms of use|©)', text, re.IGNORECASE
                ):
                    body_parts.append(text)
            if body_parts:
                result["body"] = "\n\n".join(body_parts)
        except Exception as exc:
            print(f"[hec-edu-en] detail soup error for {url}: {exc}")

        return result

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        start_ts = time.time()
        MAX_WALL_SECS = 25 * 60  # 25 minutes
        MAX_PAGES = 200

        saved = 0
        page = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "unlimited"

        print(f"[hec-edu-en] Starting crawl (limit={limit_str})")
        view_dom_id = self._get_dom_id()
        print(f"[hec-edu-en] view_dom_id={view_dom_id[:16]}…")

        while True:
            # Wall-clock budget
            if time.time() - start_ts > MAX_WALL_SECS:
                print(f"[hec-edu-en] 25-minute budget reached at page {page}. Stopping.")
                break

            # Limit reached
            if limit is not None and saved >= limit:
                break

            # Safety page cap
            if page >= MAX_PAGES:
                print(f"[hec-edu-en] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            # Progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[hec-edu-en] page {page}: saved {saved}/{limit_str}")

            raw = self._ajax_post(page, view_dom_id)
            if not raw:
                print(f"[hec-edu-en] No AJAX response for page {page}. Stopping.")
                break

            try:
                commands = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[hec-edu-en] JSON decode error on page {page}: {exc}. Stopping.")
                break

            # Find the insert command containing article HTML
            html_data = None
            has_next = False
            for cmd in commands:
                if not isinstance(cmd, dict):
                    continue
                data = cmd.get("data", "")
                if isinstance(data, str) and "news-item" in data:
                    html_data = data
                    has_next = bool(re.search(r'href="[^"]*page=\d+[^"]*"', data))
                    break

            if not html_data:
                print(f"[hec-edu-en] No article HTML in AJAX response for page {page}. Done.")
                break

            cards, _ = self._parse_cards(html_data)

            if not cards:
                print(f"[hec-edu-en] No cards parsed on page {page}. Done.")
                break

            new_cards = [c for c in cards if c["url"] not in seen_urls]
            if not new_cards:
                print(f"[hec-edu-en] All {len(cards)} cards on page {page} already seen. Done.")
                break

            for card in new_cards:
                if limit is not None and saved >= limit:
                    break

                url = card["url"]
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(url)

                    # Build abstract: prefer full body, then meta desc, then list desc
                    abstract = detail.get("body") or detail.get("meta_desc") or card["description"]

                    if not abstract or len(abstract) < 50:
                        print(f"[hec-edu-en] Short abstract (<50 chars) for {url}, skipping.")
                        continue

                    # Use slug as external_id (nav-menu /node/N links pollute
                    # regex-based node ID extraction on HEC pages).
                    slug = url.rstrip("/").split("/")[-1]
                    node_id = detail.get("node_id", "")
                    external_id = slug
                    post_number = slug

                    published_date = detail.get("detail_date") or card["date"]
                    listed_date = card["date"]

                    pdf_url = detail.get("pdf_url") or ""
                    original_filename = None
                    if pdf_url:
                        raw_fn = pdf_url.split("/")[-1].split("?")[0]
                        original_filename = unquote(raw_fn) if raw_fn else None

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": card["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": url,
                        "pdf_url": pdf_url or None,
                        "original_filename": original_filename,
                        "authors": "",
                        "publisher": "HEC Paris",
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": card["category"],
                        "doi": "",
                        "metadata": json.dumps({
                            "posted_date": card["date"],
                            "node_id": node_id,
                            "slug": slug,
                            "originalFilename": original_filename,
                            "meta_description": detail.get("meta_desc", ""),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[hec-edu-en] Saved {saved}/{limit_str}: {card['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[hec-edu-en] item {url} failed: {exc}")
                    continue

            if not has_next:
                print(f"[hec-edu-en] No next-page link after page {page}. Done.")
                break

            page += 1

        print(f"[hec-edu-en] Done. Total saved: {saved}")
        return saved
