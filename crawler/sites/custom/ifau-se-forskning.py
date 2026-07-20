# -*- coding: utf-8 -*-
"""Crawler for IFAU (Institute for Evaluation of Labour Market and Education Policy) Reports.

Starting URL: https://www.ifau.se/Forskning/Publikationer/Rapporter/
List API:     POST /Forskning/Publikationer/Rapporter/SearchQep/
              body: {"Years":[], "SubjectCategories":[], "Query":"", "Page": N}
              Returns HTML fragment with 10 items/page; 600 total / 60 pages.
Detail pages: citation_* meta tags supply title, abstract, authors, date, PDF URL.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from html import unescape

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.ifau.se"
_LIST_URL = _BASE_URL + "/Forskning/Publikationer/Rapporter/"
_SEARCHQEP = _LIST_URL + "SearchQep/"
_LINK_RE = re.compile(
    r'href="(/Forskning/Publikationer/Rapporter/\d{4}/[^"]+)"'
)
_MAX_PAGES = 200
_WALL_MINUTES = 25
_MIN_ABSTRACT = 50   # chars; items below this are skipped


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl(url: str, retries: int = 3) -> str:
    """Fetch *url* with curl (TLS-1.3, insecure). Returns text or '' on failure."""
    backoff = [1, 3, 9]
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(backoff[attempt - 1])
        try:
            cmd = [
                "curl", "--tls-max", "1.3", "-skL",
                "--max-time", "30",
                "--user-agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-H", "Accept: text/html,*/*",
                "-H", "Accept-Language: sv-SE,sv;q=0.9,en;q=0.8",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[ifau-se-forskning] curl exit {result.returncode} for {url}")
        except subprocess.TimeoutExpired:
            print(f"[ifau-se-forskning] curl timeout (attempt {attempt + 1}/{retries}) for {url}")
        except Exception as exc:
            print(f"[ifau-se-forskning] curl error (attempt {attempt + 1}/{retries}): {exc}")
    return ""


# ---------------------------------------------------------------------------
# HTML / parse helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
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


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_citation_meta(raw: str, soup) -> dict:
    """Return dict of citation_* meta tag values (multi-valued → list for authors)."""
    meta: dict = {}
    authors: list = []

    if soup is not None:
        try:
            for tag in soup.find_all("meta"):
                name = tag.get("name", "")
                content = tag.get("content", "")
                if not name.startswith("citation_"):
                    continue
                val = unescape(content)
                if name == "citation_author":
                    authors.append(val)
                else:
                    meta[name] = val
        except Exception:
            pass

    # Regex fallback for malformed pages
    if not meta:
        for m in re.finditer(
            r'<meta\s+name="(citation_\w+)"\s+content="([^"]*)"', raw
        ):
            name, val = m.group(1), unescape(m.group(2))
            if name == "citation_author":
                authors.append(val)
            else:
                meta[name] = val

    meta["_authors_list"] = authors
    return meta


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class IFAURapporterCrawler(BaseCrawler):
    site_id = "ifau-se-forskning"
    site_name = "Custom: ifau-se-forskning"
    base_url = "https://www.ifau.se"

    # ------------------------------------------------------------------
    # List API
    # ------------------------------------------------------------------

    def _post_list_page(self, page: int):
        """POST to SearchQep; returns Response or None."""
        return self._request(
            _SEARCHQEP,
            method="POST",
            json={"Years": [], "SubjectCategories": [], "Query": "", "Page": page},
        )

    def _extract_links(self, html: str) -> list[str]:
        """Return list of unique /Forskning/Publikationer/Rapporter/YEAR/slug/ paths."""
        seen: set = set()
        result = []
        for m in _LINK_RE.finditer(html):
            path = m.group(1)
            if path not in seen:
                seen.add(path)
                result.append(path)
        return result

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, path: str) -> dict | None:
        """Fetch detail page and parse citation_* meta tags. Returns paper dict or None."""
        url = _BASE_URL + path

        # Use curl for resilience
        raw = _curl(url)
        if not raw:
            return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error {url}: {exc}")
            soup = None

        meta = _extract_citation_meta(raw, soup)

        # Title
        title = meta.get("citation_title", "")
        if not title and soup is not None:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
        if not title:
            m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.DOTALL)
            if m:
                title = _strip_html(m.group(1))

        # Abstract — HTML-encoded in meta tag; strip tags
        abstract = _strip_html(meta.get("citation_abstract", ""))
        if not abstract and soup is not None:
            art = soup.select_one(".article-content")
            if art:
                abstract = art.get_text(strip=True)

        # Authors list
        authors = meta.get("_authors_list", [])

        # Publication date (prefer full date over year-only)
        pub_date = (
            meta.get("citation_publication_date", "")
            or meta.get("citation_year", "")
        )
        if not pub_date:
            m = re.search(r"/(\d{4})/", path)
            if m:
                pub_date = m.group(1)

        # Report number e.g. "2026:9"
        report_num = meta.get("citation_technical_report_number", "")
        institution = meta.get("citation_technical_report_institution", "IFAU")

        # PDF URL
        pdf_rel = meta.get("citation_pdf_url", "")
        if pdf_rel:
            pdf_url = (_BASE_URL + pdf_rel) if pdf_rel.startswith("/") else pdf_rel
        else:
            hits = re.findall(r'href="(/globalassets/[^"]*\.pdf)"', raw)
            pdf_url = (_BASE_URL + hits[0]) if hits else ""

        external_id = report_num if report_num else path.rstrip("/").split("/")[-1]

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": "Rapport",
            "keywords": json.dumps([]),
            "published_date": pub_date or None,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": institution,
            "metadata": json.dumps(
                {"report_number": report_num, "institution": institution, "path": path},
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_paths: set = set()
        limit_display = limit if limit is not None else "inf"
        deadline = time.time() + _WALL_MINUTES * 60

        for page in range(1, _MAX_PAGES + 1):
            if time.time() > deadline:
                print(
                    f"[{self.site_id}] {_WALL_MINUTES}-minute budget reached "
                    f"at page {page}, stopping cleanly."
                )
                break

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")

            if limit is not None and saved >= limit:
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            resp = self._post_list_page(page)
            if resp is None:
                print(f"[{self.site_id}] page {page}: list request failed, stopping.")
                break

            links = self._extract_links(resp.text)
            if not links:
                print(f"[{self.site_id}] page {page}: no links, end of pagination.")
                break

            new_links = [p for p in links if p not in seen_paths]
            seen_paths.update(links)

            if not new_links:
                print(f"[{self.site_id}] page {page}: all links already seen, stopping.")
                break

            for path in new_links:
                if limit is not None and saved >= limit:
                    break
                if time.time() > deadline:
                    break

                try:
                    detail = self._fetch_detail(path)
                    if not detail:
                        print(f"[{self.site_id}] item {path}: fetch/parse failed, skipping.")
                        continue

                    abstract = detail.get("abstract", "")
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] item {path}: "
                            f"abstract too short ({len(abstract)} chars), skipping."
                        )
                        continue

                    self._save_paper(detail)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved [{saved}] "
                        f"{detail.get('title', '')[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {path} failed: {exc}")
                    continue

        print(f"[{self.site_id}] crawl complete: {saved} records saved.")
        return saved
