# -*- coding: utf-8 -*-
"""Crawler for Manatū Wāhine Ministry for Women – Library.

Site ID : women-govt-nz-library
Starting URL: https://www.women.govt.nz/library
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urljoin, urlparse

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

_BS4_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str):
    """Try html5lib → lxml → html.parser.  Returns BeautifulSoup or None."""
    from bs4 import BeautifulSoup
    for parser in _BS4_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# Network helpers (curl-based for TLS compatibility)
# ---------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        try:
            r = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "-H", f"User-Agent: {_UA}",
                    "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                    "--max-time", "30",
                    url,
                ],
                capture_output=True,
                timeout=40,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
            print(
                f"[women-govt-nz-library] GET failed "
                f"(attempt {attempt + 1}/{retries}, rc={r.returncode}) {url}"
            )
        except Exception as exc:
            print(
                f"[women-govt-nz-library] GET exception "
                f"(attempt {attempt + 1}/{retries}) {url}: {exc}"
            )
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            time.sleep(wait)
    return None


def _curl_post(url: str, fields: dict, retries: int = 3) -> bytes | None:
    form_data = "&".join(f"{k}={v}" for k, v in fields.items())
    for attempt in range(retries):
        try:
            r = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "-X", "POST",
                    "-H", "Content-Type: application/x-www-form-urlencoded",
                    "-H", "Accept: application/json, text/html, */*",
                    "-H", f"User-Agent: {_UA}",
                    "-H", "X-Requested-With: XMLHttpRequest",
                    "-H", "Referer: https://www.women.govt.nz/library",
                    "--data", form_data,
                    "--max-time", "30",
                    url,
                ],
                capture_output=True,
                timeout=40,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
            print(
                f"[women-govt-nz-library] POST failed "
                f"(attempt {attempt + 1}/{retries}, rc={r.returncode}) {url}"
            )
        except Exception as exc:
            print(
                f"[women-govt-nz-library] POST exception "
                f"(attempt {attempt + 1}/{retries}) {url}: {exc}"
            )
        if attempt < retries - 1:
            wait = 3 ** attempt
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Date extraction helpers
# ---------------------------------------------------------------------------

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _date_from_url(url: str) -> str | None:
    """Extract approx. date from PDF path e.g. /2025-08/file.pdf → '2025-08-01'."""
    m = re.search(r"/(\d{4}-\d{2})/", url)
    return m.group(1) + "-01" if m else None


def _date_from_text(text: str) -> str | None:
    """Best-effort date extraction from body text."""
    # ISO format
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    # "DD Month YYYY"
    pat = r"\b(\d{1,2})\s+(" + "|".join(_MONTH_MAP) + r")\s+(\d{4})\b"
    m = re.search(pat, text, re.I)
    if m:
        d = m.group(1).zfill(2)
        mon = _MONTH_MAP[m.group(2).lower()]
        y = m.group(3)
        return f"{y}-{mon}-{d}"
    # "Month YYYY"
    pat2 = r"\b(" + "|".join(_MONTH_MAP) + r")\s+(\d{4})\b"
    m = re.search(pat2, text, re.I)
    if m:
        return f"{m.group(2)}-{_MONTH_MAP[m.group(1).lower()]}-01"
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class WomenGovtNzLibraryCrawler(BaseCrawler):
    site_id   = "women-govt-nz-library"
    site_name = "Custom: women-govt-nz-library"
    base_url  = "https://www.women.govt.nz"

    # Drupal Views AJAX endpoint — discovered from library page JS settings
    _VIEWS_AJAX = "https://www.women.govt.nz/views/ajax"
    # DOM ID from drupal-settings-json embedded in https://www.women.govt.nz/library
    _DOM_ID = (
        "11211517047e0757838c6222fdd9f396129813aeb01e731f5dae631cb6d3f4a0"
    )
    _MIN_ABSTRACT = 100   # chars — items below this threshold are skipped

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        t_start = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute wall-clock budget
        MAX_PAGES = 200

        page = 0

        while True:
            # ── wall-clock budget ──────────────────────────────────────────
            if time.time() - t_start > MAX_WALL:
                print("[women-govt-nz-library] wall-clock budget reached, exiting cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page >= MAX_PAGES:
                print(f"[women-govt-nz-library] reached safety cap of {MAX_PAGES} pages")
                break

            # progress logging every 10 pages
            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[women-govt-nz-library] page {page}: saved {saved}/{limit_str}")

            # ── fetch list page via Drupal Views AJAX ──────────────────────
            raw = _curl_post(
                self._VIEWS_AJAX,
                {
                    "view_name":       "sector_sitewide_search",
                    "view_display_id": "resource_search",
                    "view_path":       "%2Fnode%2F178",
                    "view_dom_id":     self._DOM_ID,
                    "pager_element":   "0",
                    "page":            str(page),
                },
            )

            if not raw:
                print(f"[women-govt-nz-library] page {page}: list fetch failed, stopping")
                break

            # ── parse Views-AJAX JSON response ─────────────────────────────
            # Drupal sometimes wraps the JSON in <textarea>…</textarea> to
            # prevent IE XSS issues — strip that wrapper before parsing.
            try:
                raw_text = raw.decode("utf-8", errors="replace")
                m = re.search(r"<textarea[^>]*>(.*)</textarea>", raw_text, re.DOTALL)
                json_text = m.group(1) if m else raw_text
                commands = json.loads(json_text)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                print(f"[women-govt-nz-library] page {page}: JSON parse error: {exc}")
                break

            html_frag = None
            for cmd in commands:
                if cmd.get("command") == "insert" and cmd.get("method") == "replaceWith":
                    html_frag = cmd.get("data", "")
                    break

            if not html_frag:
                print(f"[women-govt-nz-library] page {page}: no HTML fragment in response, stopping")
                break

            soup = _make_soup(html_frag)
            if soup is None:
                print(f"[women-govt-nz-library] page {page}: could not parse HTML, stopping")
                break

            items = soup.find_all("li", class_="slat")
            if not items:
                print(f"[women-govt-nz-library] page {page}: no items found, end of pagination")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - t_start > MAX_WALL:
                    print("[women-govt-nz-library] wall-clock budget reached mid-page, stopping")
                    break

                try:
                    # ── list-level extraction ──────────────────────────────
                    a_tag = item.find("a")
                    if not a_tag:
                        continue

                    title = _clean(a_tag.get_text())
                    rel_url = (a_tag.get("href") or "").strip()
                    if not rel_url:
                        continue

                    full_url = urljoin(self.base_url, rel_url)

                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    new_on_page += 1

                    body_div = item.find("div", class_="slat__body")
                    list_abstract = _clean(body_div.get_text()) if body_div else ""

                    badges = item.find_all("span", class_="badge")
                    categories = [
                        _clean(b.get_text()) for b in badges if b.get_text().strip()
                    ]

                    # ── detail page fetch ──────────────────────────────────
                    time.sleep(self._delay)
                    raw_detail = _curl_get(full_url)
                    if not raw_detail:
                        print(
                            f"[women-govt-nz-library] detail fetch failed after retries,"
                            f" skipping: {full_url}"
                        )
                        continue

                    dhtml = raw_detail.decode("utf-8", errors="replace")

                    # Drupal node ID from <link rel="shortlink" href="/node/NNN">
                    m = re.search(r'<link rel="shortlink"[^>]+/node/(\d+)"', dhtml)
                    node_id = m.group(1) if m else None
                    slug = rel_url.rstrip("/").rsplit("/", 1)[-1]
                    external_id = node_id if node_id else slug
                    post_number = node_id if node_id else slug

                    # ── parse detail HTML ──────────────────────────────────
                    ds = _make_soup(dhtml)

                    full_abstract = list_abstract
                    pdf_url = None
                    original_filename = None
                    published_date = None

                    if ds:
                        # primary: .prose div (Drupal body field)
                        prose = ds.find("div", class_=re.compile(r"\bprose\b"))
                        if prose:
                            prose_text = _clean(prose.get_text())
                            if len(prose_text) > len(full_abstract):
                                full_abstract = prose_text

                        # fallback: <main> tag
                        if len(full_abstract) < self._MIN_ABSTRACT:
                            main = ds.find("main")
                            if main:
                                candidate = _clean(main.get_text())
                                if len(candidate) > len(full_abstract):
                                    full_abstract = candidate

                        # first PDF link
                        for a in ds.find_all("a", href=True):
                            href = a["href"]
                            if re.search(r"\.pdf", href, re.I):
                                if not href.startswith("http"):
                                    href = urljoin(self.base_url, href)
                                pdf_url = href
                                parsed_p = urlparse(href).path
                                original_filename = unquote(
                                    parsed_p.rsplit("/", 1)[-1]
                                )
                                published_date = _date_from_url(href)
                                break

                        # date from body text if not found in PDF path
                        if not published_date:
                            published_date = _date_from_text(full_abstract)

                    # ── skip if abstract too short ─────────────────────────
                    if len(full_abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[women-govt-nz-library] abstract too short "
                            f"({len(full_abstract)} chars), skipping: {title[:60]}"
                        )
                        continue

                    paper = {
                        "external_id":       external_id,
                        "site_id":           self.site_id,
                        "post_number":       post_number,
                        "title":             title,
                        "abstract":          full_abstract,
                        "published_date":    published_date,
                        "listed_date":       None,
                        "url":               full_url,
                        "pdf_url":           pdf_url,
                        "original_filename": original_filename,
                        "category":          "; ".join(categories) if categories else None,
                        "publisher":         "Manatū Wāhine Ministry for Women",
                        "authors":           None,
                        "keywords":          None,
                        "doi":               None,
                        "metadata":          json.dumps(
                            {
                                "node_id":    node_id,
                                "slug":       slug,
                                "categories": categories,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[women-govt-nz-library] item failed: {exc}")
                    continue

            # if no new (unseen) items on this page (and not the very first page),
            # pagination has looped back — stop
            if new_on_page == 0 and page > 0:
                print(f"[women-govt-nz-library] page {page}: all items already seen, stopping")
                break

            page += 1

        print(f"[women-govt-nz-library] crawl complete: {saved} saved")
        return saved
