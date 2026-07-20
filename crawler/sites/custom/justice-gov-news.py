# -*- coding: utf-8 -*-
"""Crawler for US Department of Justice press releases.

Target: https://www.justice.gov/news/press-releases
Bot protection: Akamai interstitial (solved once via /_sec/verify, cookie reused).
Pagination: ?page=0, ?page=1, ...  (12 items/page)
Detail page: Drupal node, body in div.node-press-release paragraphs.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler

_SITE_ID = "justice-gov-news"
_BASE_URL = "https://www.justice.gov"
_LIST_URL = "https://www.justice.gov/news/press-releases"
_PAGE_SIZE = 12
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 50
_DETAIL_DELAY = 1.0
_COOKIE_FILE = "/tmp/justice_gov_cookies.txt"

_BS_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str):
    """Try parsers in order; return first that succeeds."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("beautifulsoup4 is required")
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("All BS4 parsers failed")


class JusticeGovNewsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: justice-gov-news"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self._akamai_solved = False

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, referer: str | None = None, timeout: int = 40) -> str | None:
        """GET with cookie jar + retry/backoff. Returns decoded text or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", str(timeout),
            "--compressed",
            "-c", _COOKIE_FILE, "-b", _COOKIE_FILE,
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "Accept-Encoding: gzip, deflate, br",
            "-H", "Connection: keep-alive",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        waits = [1, 3, 9]
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                err = result.stderr.decode("utf-8", errors="replace").strip()
                last = f"exit={result.returncode} {err[:80]}"
            except Exception as exc:
                last = str(exc)
            if attempt < len(waits):
                print(f"[{_SITE_ID}] curl attempt {attempt}/3 failed for {url}: {last}; retrying in {wait}s")
                time.sleep(wait)
        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}")
        return None

    def _solve_akamai(self, url: str) -> str | None:
        """Fetch url, solving Akamai interstitial if present. Returns HTML or None."""
        html = self._curl(url)
        if html is None:
            return None

        # If no challenge token present, we already have the real page
        if "bm-verify" not in html:
            return html

        m_i = re.search(r'var i = (\d+);', html)
        m_j = re.search(r'Number\("(\d+)"\s*\+\s*"(\d+)"\)', html)
        m_bm = re.search(r'"bm-verify":\s*"([^"]+)"', html)
        if not (m_i and m_j and m_bm):
            print(f"[{_SITE_ID}] Could not parse Akamai challenge tokens from {url}")
            return html

        i_val = int(m_i.group(1))
        j_val = i_val + int(m_j.group(1) + m_j.group(2))
        bm = m_bm.group(1)
        payload = json.dumps({"bm-verify": bm, "pow": j_val})

        verify_result = subprocess.run([
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30", "--compressed",
            "-c", _COOKIE_FILE, "-b", _COOKIE_FILE,
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Content-Type: application/json",
            "-H", "Origin: https://www.justice.gov",
            "-H", f"Referer: {url}",
            "-d", payload,
            "https://www.justice.gov/_sec/verify?provider=interstitial",
        ], capture_output=True, timeout=40)

        verify_resp = verify_result.stdout.decode("utf-8", errors="replace")
        try:
            data = json.loads(verify_resp)
        except Exception:
            data = {}

        if data.get("reload"):
            time.sleep(1)
            return self._curl(url)
        if data.get("location"):
            redir = data["location"]
            if not redir.startswith("http"):
                redir = _BASE_URL + redir
            return self._curl(redir)

        # Fallback: retry the original URL
        time.sleep(1)
        return self._curl(url)

    def _fetch_page(self, page: int) -> str | None:
        """Fetch list page N (0-indexed). Returns HTML or None."""
        url = f"{_LIST_URL}?page={page}"
        html = self._curl(url, referer=_LIST_URL)
        if html is None:
            return None
        if "bm-verify" in html:
            return self._solve_akamai(url)
        return html

    def _fetch_detail(self, url: str) -> str | None:
        """Fetch a detail page, solving bot challenge if needed."""
        html = self._curl(url, referer=_LIST_URL)
        if html is None:
            return None
        if "bm-verify" in html:
            return self._solve_akamai(url)
        return html

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_tags(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return list of {title, url, listed_date, teaser} dicts from list page HTML."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse failed on list page: {exc}")
            return []

        items = []
        for article in soup.find_all("article", class_="news-content-listing"):
            try:
                link_tag = article.find("a", href=True)
                if not link_tag:
                    continue
                href = link_tag.get("href", "").strip()
                if not href:
                    continue
                if not href.startswith("http"):
                    href = urljoin(_BASE_URL, href)

                title_span = article.find("span", class_="field-formatter--string")
                title = title_span.get_text(strip=True) if title_span else link_tag.get_text(strip=True)

                time_tag = article.find("time")
                listed_date = ""
                if time_tag:
                    dt = time_tag.get("datetime", "")
                    if dt:
                        listed_date = dt[:10]  # YYYY-MM-DD

                teaser_el = article.find(class_="field_teaser")
                teaser = teaser_el.get_text(separator=" ", strip=True) if teaser_el else ""

                items.append({
                    "title": title,
                    "url": href,
                    "listed_date": listed_date,
                    "teaser": teaser,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] Error parsing list item: {exc}")
                continue
        return items

    def _parse_detail(self, html: str, url: str) -> dict:
        """Parse detail page. Returns metadata dict."""
        result = {
            "title": "",
            "abstract": "",
            "published_date": "",
            "node_id": "",
            "department": "",
            "publisher": "",
            "office": "",
        }

        # Extract node ID from HTML (Drupal numeric ID)
        m_node = re.search(r'/node/(\d+)', html)
        if m_node:
            result["node_id"] = m_node.group(1)

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse failed on detail {url}: {exc}")
            return result

        # Title
        h1 = soup.find("h1")
        if h1:
            result["title"] = h1.get_text(strip=True)

        # Published date
        time_tag = soup.find("time")
        if time_tag:
            dt = time_tag.get("datetime", "")
            if dt:
                result["published_date"] = dt[:10]

        # Body paragraphs from node press release div
        node_div = soup.find(class_="node-press-release")
        if not node_div:
            node_div = soup.find(class_="node-content")

        paras = []
        if node_div:
            for p in node_div.find_all("p"):
                txt = p.get_text(separator=" ", strip=True)
                # Skip very short lines and navigation/banner text
                if len(txt) > 30 and "official website" not in txt.lower():
                    paras.append(txt)

        result["abstract"] = "\n\n".join(paras)

        # Department/components
        comp_div = soup.find(class_="node-component")
        if comp_div:
            comps = [a.get_text(strip=True) for a in comp_div.find_all("a")]
            result["publisher"] = "; ".join(c for c in comps if c)

        # Office (e.g., "Office of Public Affairs")
        office_div = soup.find(class_="node-office")
        if office_div:
            result["office"] = office_div.get_text(strip=True)

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl DOJ press releases. Returns number of saved documents."""
        import time as _time
        start_wall = _time.monotonic()
        wall_budget_s = 25 * 60  # 25 minutes

        # Ensure Akamai cookies are valid on first run
        if not self._akamai_solved:
            print(f"[{_SITE_ID}] Initializing session (solving bot check if needed)...")
            html0 = self._solve_akamai(_LIST_URL)
            if html0 and "bm-verify" not in html0:
                self._akamai_solved = True
                print(f"[{_SITE_ID}] Session ready.")
            else:
                print(f"[{_SITE_ID}] Warning: could not confirm session; continuing anyway.")

        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(_MAX_PAGES):
            # Wall-clock budget
            if _time.monotonic() - start_wall > wall_budget_s:
                print(f"[{_SITE_ID}] Wall-clock budget reached at page {page}; exiting cleanly.")
                break

            # Limit check
            if limit is not None and saved >= limit:
                break

            # Progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # Fetch list page
            html = self._fetch_page(page)
            if html is None:
                print(f"[{_SITE_ID}] page {page}: fetch failed, stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no items found, pagination end.")
                break

            # Safety: page-loop detection via URL deduplication
            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                # Derive slug and external_id from URL path
                path = urlparse(url).path  # e.g. /opa/pr/some-title
                slug = path.rstrip("/").split("/")[-1]  # last segment
                external_id = path.lstrip("/").replace("/", "--")  # unique across all components

                # Per-item isolation
                try:
                    _time.sleep(self._delay)

                    detail_html = self._fetch_detail(url)
                    if detail_html is None:
                        print(f"[{_SITE_ID}] Detail fetch failed: {url}")
                        continue

                    detail = self._parse_detail(detail_html, url)

                    title = detail["title"] or item["title"]
                    abstract = detail["abstract"]
                    published_date = detail["published_date"] or item["listed_date"]
                    listed_date = item["listed_date"]
                    publisher = detail["publisher"]
                    department = detail["office"]
                    node_id = detail["node_id"]

                    # post_number: Drupal node ID (numeric string) when available
                    post_number = node_id if node_id else slug

                    if not abstract or len(abstract) < _ABSTRACT_MIN_CHARS:
                        # Fall back to teaser
                        if item["teaser"] and len(item["teaser"]) >= _ABSTRACT_MIN_CHARS:
                            abstract = item["teaser"]
                        else:
                            print(f"[{_SITE_ID}] Skipping (abstract too short): {title[:60]}")
                            continue

                    # Build metadata dict
                    meta = {
                        "posted_date": listed_date,
                        "node_id": node_id,
                        "slug": slug,
                        "url_path": urlparse(url).path,
                        "office": detail["office"],
                    }

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": url,
                        "pdf_url": None,
                        "authors": "",
                        "publisher": publisher,
                        "department": department,
                        "keywords": "",
                        "category": "Press Release",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({url}): {exc}")
                    continue

            # If page had items but none were new, pagination is looping — stop
            if new_on_page == 0 and items:
                print(f"[{_SITE_ID}] All items on page {page} already seen; stopping.")
                break

            if page == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached; logging and stopping.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
