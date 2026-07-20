# -*- coding: utf-8 -*-
"""KNMI kennis-en-datacentrum publications crawler.

Starting URL:
  https://www.knmi.nl/kennis-en-datacentrum/zoekresultaten?q=&page=1&sort=score
  &type=data_center_publication&publication_types%5B%5D=knmi_publication
  &year_from=2016&year_to=2026

Strategy:
  - Fetch paginated search results via AJAX (X-Requested-With: XMLHttpRequest).
    The server returns a JS snippet that sets innerHTML of #search-results.
  - Parse the JS string to extract publication links and truncated intro text.
  - For items with a non-empty intro (abstract likely present), fetch the detail
    page and extract the full abstract from div.serif.
  - Skip items whose abstract < 50 chars. Save the rest.
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup as _BS

    _PARSERS: list[str] = []
    for _candidate in ("html5lib", "lxml", "html.parser"):
        _mod = _candidate.split(".")[0]
        try:
            __import__(_mod)
            _PARSERS.append(_candidate)
        except ImportError:
            pass
    if not _PARSERS:
        _PARSERS = ["html.parser"]
except ImportError:
    _BS = None  # type: ignore[assignment]
    _PARSERS = []

_SITE_ID = "knmi-nl-kennis-en-datacentru"


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html_text: str):
    if _BS is None or not html_text:
        return None
    for parser in _PARSERS:
        try:
            return _BS(html_text, parser)
        except Exception:
            continue
    return None


def _parse_js_string_at(text: str, start: int) -> tuple[str, int]:
    """Scan a JS double-quoted string beginning at `start` (past the opening `"`).

    Handles common escape sequences: \\n \\r \\t \\/ \\" \\\\
    Returns (unescaped_content, end_pos) where end_pos points at the closing `"`.
    """
    _ESC = {
        "n": "\n", "r": "\r", "t": "\t",
        "/": "/", '"': '"', "'": "'", "\\": "\\",
        "0": "\0",
    }
    pos = start
    chars: list[str] = []
    n = len(text)
    while pos < n:
        c = text[pos]
        if c == "\\" and pos + 1 < n:
            nc = text[pos + 1]
            chars.append(_ESC.get(nc, nc))
            pos += 2
        elif c == '"':
            break
        else:
            chars.append(c)
            pos += 1
    return "".join(chars), pos


def _extract_js_html(text: str, selector: str) -> str | None:
    """Extract the HTML content from a jQuery .html("...") call for `selector`."""
    for q_outer in ("'", '"'):
        pattern = f"$({q_outer}{selector}{q_outer}).html(\""
        idx = text.find(pattern)
        if idx != -1:
            start = idx + len(pattern)
            content, _ = _parse_js_string_at(text, start)
            return content
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KnmiNlKennisEnDatacentruCrawler(BaseCrawler):
    """Crawls KNMI kennis-en-datacentrum KNMI publications (2016-2026)."""

    site_id = "knmi-nl-kennis-en-datacentru"
    site_name = "Custom: knmi-nl-kennis-en-datacentru"
    base_url = "https://www.knmi.nl"

    _LIST_URL = "https://www.knmi.nl/kennis-en-datacentrum/zoekresultaten"
    _SEARCH_PARAMS = {
        "q": "",
        "sort": "score",
        "type": "data_center_publication",
        "publication_types[]": "knmi_publication",
        "year_from": "2016",
        "year_to": "2026",
    }

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, extra_headers: dict | None = None,
                  retries: int = 3) -> str | None:
        """GET via curl with exponential backoff (1s, 3s, 9s)."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)

        for attempt in range(retries):
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=35)
                text = proc.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < retries - 1:
                    wait = 3 ** attempt  # 1, 3, 9
                    print(f"[{_SITE_ID}] empty response (attempt {attempt + 1}), "
                          f"retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}): {exc}, "
                          f"retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict]:
        """Fetch one AJAX search-results page. Returns list of item dicts."""
        params = dict(self._SEARCH_PARAMS)
        if page > 1:
            params["page"] = str(page)
        url = f"{self._LIST_URL}?{urlencode(params)}"

        text = self._curl_get(
            url,
            extra_headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if not text:
            return []

        # The server returns a JS snippet: $('#search-results').html("...escaped HTML...");
        html_content = _extract_js_html(text, "#search-results")

        # Fallback for page 1 or server returning full HTML
        if not html_content:
            soup_full = _make_soup(text)
            if soup_full:
                container = soup_full.select_one("#search-results")
                if container:
                    html_content = str(container)

        if not html_content:
            return []

        soup = _make_soup(html_content)
        if not soup:
            return []

        items: list[dict] = []
        for li in soup.select("li"):
            a_tag = li.select_one("a.search-results__title")
            intro_tag = li.select_one("p.search-results__intro")
            meta_tag = li.select_one("span.search-results__meta")

            if not a_tag:
                continue

            href = a_tag.get("href", "")
            title = a_tag.get_text(strip=True)
            intro = intro_tag.get_text(separator=" ", strip=True) if intro_tag else ""
            meta_text = meta_tag.get_text(separator=" ", strip=True) if meta_tag else ""

            if not href or not title:
                continue

            items.append({
                "url": f"https://www.knmi.nl{href}",
                "title": title,
                "intro": intro,
                "meta_text": meta_text,
            })

        return items

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict | None:
        """Fetch a publication detail page and extract all metadata."""
        text = self._curl_get(url)
        if not text:
            return None

        try:
            soup = _make_soup(text)
        except Exception as exc:
            print(f"[{_SITE_ID}] parse error for {url}: {exc}")
            return None
        if not soup:
            return None

        result: dict = {}

        # Title
        h1 = soup.select_one("h1.hero-text__heading")
        result["title"] = h1.get_text(strip=True) if h1 else ""

        # Full abstract from .serif div
        serif = soup.select_one("div.serif")
        result["abstract"] = serif.get_text(separator="\n", strip=True) if serif else ""

        # Authors — strip child UI elements (share button, inputs)
        author_div = soup.select_one("div.publication-author")
        if author_div:
            for child in author_div.find_all(["div", "input", "label"]):
                child.decompose()
            result["authors_raw"] = author_div.get_text(separator=", ", strip=True)
        else:
            result["authors_raw"] = ""

        # Bibliographic paragraph (KNMI number, journal, year, pages, DOI)
        bib_p = soup.select_one("div.editable-table p")
        bib_text = bib_p.get_text(separator=" ", strip=True) if bib_p else ""
        result["bib_text"] = bib_text

        m = re.search(r"KNMI number:\s*([A-Z0-9\-]+)", bib_text)
        result["knmi_number"] = m.group(1) if m else None

        m = re.search(r"Year:\s*(\d{4})", bib_text)
        result["year"] = m.group(1) if m else None

        # Journal — stop before Volume/Year/Issue/end
        m = re.search(
            r"Journal:\s*(.+?)(?:\s*,\s*(?:Volume:|Year:|Issue:)|$)",
            bib_text,
        )
        result["journal"] = m.group(1).strip() if m else None

        m = re.search(r"Volume:\s*(\S+?)(?:\s*,|$)", bib_text)
        result["volume"] = m.group(1) if m else None

        m = re.search(r"Issue:\s*(\S+?)(?:\s*,|$)", bib_text)
        result["issue"] = m.group(1) if m else None

        m_first = re.search(r"First page:\s*([^,]+?)(?:\s*,|$)", bib_text)
        m_last = re.search(r"Last page:\s*([^,]+?)(?:\s*,|$)", bib_text)
        if m_first and m_last:
            result["pages"] = f"{m_first.group(1).strip()}-{m_last.group(1).strip()}"
        elif m_first:
            result["pages"] = m_first.group(1).strip()
        else:
            result["pages"] = None

        # DOI — prefer <a href> extraction, fall back to text
        doi_a = soup.select_one("a[href*='doi.org']")
        if doi_a:
            href_val = doi_a.get("href", "")
            m = re.search(r"10\.\d{4,}/\S+", href_val)
            result["doi"] = m.group(0).rstrip(".") if m else None
        else:
            m = re.search(r"doi:\s*(10\.\d{4,}/\S+)", bib_text, re.IGNORECASE)
            result["doi"] = m.group(1).rstrip(".") if m else None

        # PDF URL and original filename
        pdf_a = soup.select_one("a.download[href]")
        if pdf_a:
            pdf_url = pdf_a.get("href", "")
            result["pdf_url"] = pdf_url
            raw_fn = pdf_url.split("?")[0].split("/")[-1]
            result["original_filename"] = raw_fn or None
        else:
            result["pdf_url"] = None
            result["original_filename"] = None

        return result

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl KNMI publications. Skips items with abstract < 50 chars.

        Parameters
        ----------
        limit:
            Max items to save. None = unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        limit_display = limit if limit is not None else "∞"
        start_time = time.time()
        MAX_MINUTES = 25
        MAX_PAGES = 200
        page = 1

        try:
            while True:
                # Wall-clock budget
                if (time.time() - start_time) / 60 >= MAX_MINUTES:
                    print(f"[{_SITE_ID}] Time budget ({MAX_MINUTES}min) reached "
                          f"at page {page}. Exiting.")
                    break

                # Limit satisfied
                if limit is not None and saved >= limit:
                    break

                # Safety cap
                if page > MAX_PAGES:
                    print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. "
                          f"Exiting.")
                    break

                # Progress log every 10 pages
                if page % 10 == 0:
                    print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

                items = self._fetch_list_page(page)
                if not items:
                    print(f"[{_SITE_ID}] No items on page {page}. Stopping.")
                    break

                # URL deduplication — detect looping paginator
                new_items = [i for i in items if i["url"] not in seen_urls]
                for i in items:
                    seen_urls.add(i["url"])

                if not new_items:
                    print(
                        f"[{_SITE_ID}] Page {page}: all items already seen. Stopping."
                    )
                    break

                for item_data in new_items:
                    if limit is not None and saved >= limit:
                        break

                    # Quick skip: if the list shows no intro text, the detail
                    # page serif div will also be empty — don't waste a request.
                    if not item_data.get("intro"):
                        continue

                    time.sleep(self._delay)

                    try:
                        detail = self._fetch_detail(item_data["url"])
                        if not detail:
                            print(
                                f"[{_SITE_ID}] item fetch failed: {item_data['url']}"
                            )
                            continue

                        abstract = detail.get("abstract", "")
                        if len(abstract) < 50:
                            print(
                                f"[{_SITE_ID}] skipping short abstract "
                                f"({len(abstract)}c): {item_data['title'][:50]}"
                            )
                            continue

                        title = detail.get("title") or item_data.get("title", "")
                        year = detail.get("year")
                        published_date = f"{year}-01-01" if year else None
                        slug = item_data["url"].rstrip("/").split("/")[-1]

                        meta_dict = {
                            "knmi_number": detail.get("knmi_number"),
                            "volume": detail.get("volume"),
                            "issue": detail.get("issue"),
                            "pages": detail.get("pages"),
                            "bib_text": detail.get("bib_text"),
                        }

                        self._save_paper({
                            "site_id": self.site_id,
                            "external_id": slug,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "posted_date": published_date,
                            "authors": detail.get("authors_raw") or "",
                            "publisher": "KNMI",
                            "journal": detail.get("journal"),
                            "url": item_data["url"],
                            "pdf_url": detail.get("pdf_url"),
                            "original_filename": detail.get("original_filename"),
                            "doi": detail.get("doi"),
                            "category": "KNMI Publication",
                            "metadata": json.dumps(
                                meta_dict, ensure_ascii=False
                            ),
                        })
                        saved += 1
                        print(
                            f"[{_SITE_ID}] saved [{saved}/{limit_display}]: "
                            f"{title[:60]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f"[{_SITE_ID}] item failed "
                            f"({item_data.get('url', '?')}): {exc}"
                        )
                        continue

                page += 1

        except KeyboardInterrupt:
            print(f"[{_SITE_ID}] Interrupted. Saved {saved} items.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
