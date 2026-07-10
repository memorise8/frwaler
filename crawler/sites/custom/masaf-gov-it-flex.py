# -*- coding: utf-8 -*-
"""Crawler for MASAF (Italian Ministry of Agriculture) press releases.

Target: https://www.masaf.gov.it/flex/cm/pages/ServeBLOB.php/L/IT/IDPagina/331

Structure:
  - One listing page per year (?YY=YYYY), years 2014-2026
  - All articles for a year on a single page (no sub-pagination)
  - Each article card: date + title + slug URL
  - Detail pages: h4 title + viewPar body divs + optional PDF links
"""

import json
import re
import subprocess
import sys
import os
import time
from datetime import datetime

# Allow absolute import from project root (spec_from_file_location has no package context)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup as _BS
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date_it(raw: str) -> str:
    """Convert DD/MM/YYYY or (DD.MM.YYYY) to YYYY-MM-DD. Returns raw on failure."""
    s = raw.strip().strip("()")
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


class MasafGovItFlexCrawler(BaseCrawler):
    """Crawler for MASAF Comunicati stampa (press releases)."""

    site_id = "masaf-gov-it-flex"
    site_name = "Custom: masaf-gov-it-flex"
    base_url = "https://www.masaf.gov.it"

    _LIST_BASE = "https://www.masaf.gov.it/flex/cm/pages/ServeBLOB.php/L/IT/IDPagina/331"
    # Newest first so incremental crawls stop early
    _YEARS = list(range(2026, 2013, -1))
    _PAGE_CAP = 200       # safety: never fetch more than 200 listing pages
    _MAX_WALL = 25 * 60   # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # Low-level fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_html(raw: bytes) -> str:
        """Decode response bytes, sniffing encoding to avoid mojibake.

        masaf.gov.it declares `charset=ISO-8859-1` in its Content-Type header,
        but also emits windows-1252-specific bytes (e.g. curly quotes, euro
        sign) that are undefined/wrong under strict ISO-8859-1. Try utf-8
        first (in case a page is actually UTF-8), then windows-1252 (superset
        of ISO-8859-1 for printable chars), then ISO-8859-1, picking the
        first result that decodes cleanly without the U+FFFD replacement
        character. Falls back to utf-8 with errors="replace" as a last resort.
        """
        for enc in ("utf-8", "windows-1252", "iso-8859-1"):
            try:
                html_text = raw.decode(enc)
                if "�" not in html_text:
                    return html_text
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _curl(self, url: str, retries: int = 3) -> str | None:
        """GET via curl with TLS workaround and exponential backoff."""
        for attempt in range(retries):
            if attempt > 0:
                wait = 3 ** (attempt - 1)   # 1s, 3s
                print(f"[{self.site_id}] Retry {attempt}/{retries - 1} in {wait}s: {url}")
                time.sleep(wait)
            try:
                result = subprocess.run(
                    [
                        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30", "-L",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if raw and raw.strip():
                    return self._decode_html(raw)
                print(f"[{self.site_id}] Empty response (attempt {attempt + 1}/{retries}): {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_listing(self, html: str) -> list[dict]:
        """Return list of {url, title, listed_date} from a year listing page."""
        items = []
        try:
            soup = _make_soup(html)
            if not soup:
                # Fallback: regex
                return self._parse_listing_regex(html)
            # Each article card is a div with class "u-border-bottom-xxs"
            cards = soup.find_all(
                "div",
                class_=lambda c: c and "u-border-bottom-xxs" in c.split(),
            )
            for card in cards:
                link_tag = card.find(
                    "a",
                    class_=lambda c: c and "u-textClean" in c.split() and "u-color-50" in c.split(),
                )
                if not link_tag:
                    continue
                url = (link_tag.get("href") or "").strip()
                title = link_tag.get_text(separator=" ", strip=True)
                if not url or "/flex/" in url or not url.startswith("http"):
                    continue
                # Date paragraph inside the same card
                date_p = card.find(
                    "p",
                    class_=lambda c: c and "u-textSecondary" in c.split() and "u-textWeight-400" in c.split(),
                )
                listed_date = date_p.get_text(strip=True) if date_p else ""
                items.append({"url": url, "title": title, "listed_date": listed_date})
        except Exception as exc:
            print(f"[{self.site_id}] BS4 listing parse error: {exc}; falling back to regex")
            return self._parse_listing_regex(html)
        return items

    def _parse_listing_regex(self, html: str) -> list[dict]:
        """Regex fallback for listing page parsing."""
        items = []
        # Match article cards between u-border-bottom-xxs divs
        card_pat = re.compile(
            r'class=["\'][^"\']*u-border-bottom-xxs[^"\']*["\'].*?</div>\s*</div>',
            re.DOTALL,
        )
        link_pat = re.compile(
            r'class=["\'][^"\']*u-textClean[^"\']*u-color-50[^"\']*["\'][^>]*href=["\']([^"\']+)["\']'
            r'|href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*u-textClean[^"\']*u-color-50[^"\']*["\']'
        )
        date_pat = re.compile(r'u-textWeight-400[^>]*>(\d{2}/\d{2}/\d{4})<')
        for m in card_pat.finditer(html):
            chunk = m.group(0)
            lm = link_pat.search(chunk)
            if not lm:
                continue
            url = (lm.group(1) or lm.group(2) or "").strip()
            if not url or "/flex/" in url:
                continue
            dm = date_pat.search(chunk)
            listed_date = dm.group(1) if dm else ""
            title = _strip_tags(chunk)[:200]
            items.append({"url": url, "title": title, "listed_date": listed_date})
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str) -> dict:
        """Extract title, date, body, and PDF link from a detail page."""
        out = {
            "title": "",
            "abstract": "",
            "published_date": "",
            "pdf_url": None,
            "original_filename": None,
        }
        try:
            soup = _make_soup(html)
            if not soup:
                return self._parse_detail_regex(html, url)

            # Title: <h4 class="u-text-h4 u-color-70 ...">
            h4 = soup.find("h4", class_=lambda c: c and "u-text-h4" in c.split())
            if h4:
                out["title"] = h4.get_text(separator=" ", strip=True)

            # Published date: <strong class="userFormat1">(DD.MM.YYYY)</strong>
            for strong in soup.find_all("strong", class_="userFormat1"):
                txt = strong.get_text(strip=True)
                if re.match(r'^\(\d{2}\.\d{2}\.\d{4}\)$', txt):
                    out["published_date"] = _parse_date_it(txt)
                    break

            # Body: all <div class="viewPar ..."> blocks
            body_parts = []
            for div in soup.find_all("div", class_=lambda c: c and "viewPar" in c.split()):
                text = div.get_text(separator=" ", strip=True)
                # Skip the date-only line
                if re.match(r'^\(\d{2}\.\d{2}\.\d{4}\)\.?\s*$', text):
                    continue
                if text:
                    body_parts.append(text)
            out["abstract"] = "\n\n".join(body_parts)

            # PDF links anywhere in the BLOB content
            blob_start = html.find("Begin BLOB Content")
            blob_end = html.find("End BLOB Content")
            if blob_start < 0:
                blob_start = 0
            blob_section = html[blob_start:blob_end] if blob_end > blob_start else html
            blob_soup = _make_soup(blob_section)
            if blob_soup:
                for a in blob_soup.find_all("a", href=True):
                    href = a["href"]
                    lower = href.lower()
                    if ".pdf" in lower or "serveblob" in lower and "pdf" in lower:
                        pdf_url = href if href.startswith("http") else f"{self.base_url}{href}"
                        out["pdf_url"] = pdf_url
                        fname = href.rstrip("/").split("/")[-1].split("?")[0]
                        if fname and "." in fname:
                            out["original_filename"] = fname
                        break

        except Exception as exc:
            print(f"[{self.site_id}] BS4 detail parse error for {url}: {exc}")
            return self._parse_detail_regex(html, url)
        return out

    def _parse_detail_regex(self, html: str, url: str) -> dict:
        """Regex fallback for detail page parsing."""
        out = {
            "title": "",
            "abstract": "",
            "published_date": "",
            "pdf_url": None,
            "original_filename": None,
        }
        m = re.search(r'class=["\'][^"\']*u-text-h4[^"\']*["\'][^>]*>(.*?)</h4>', html, re.DOTALL)
        if m:
            out["title"] = _strip_tags(m.group(1))
        dm = re.search(r'\((\d{2}\.\d{2}\.\d{4})\)', html)
        if dm:
            out["published_date"] = _parse_date_it(dm.group(1))
        parts = re.findall(r'class=["\'][^"\']*viewPar[^"\']*["\'][^>]*>(.*?)</div>', html, re.DOTALL)
        body = " ".join(_strip_tags(p) for p in parts if p.strip())
        out["abstract"] = re.sub(r"\s+", " ", body).strip()
        pm = re.search(r'href=["\']([^"\']+\.pdf)["\']', html, re.IGNORECASE)
        if pm:
            href = pm.group(1)
            out["pdf_url"] = href if href.startswith("http") else f"{self.base_url}{href}"
            fname = href.rstrip("/").split("/")[-1].split("?")[0]
            if fname:
                out["original_filename"] = fname
        return out

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MASAF press releases, year by year (newest first).

        Pagination model: one listing page per year (2026 → 2014).
        All articles for a year appear on a single page; no sub-pagination.
        """
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"
        start_time = time.time()
        page_count = 0

        for year in self._YEARS:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > self._MAX_WALL:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping.")
                break
            if page_count >= self._PAGE_CAP:
                print(f"[{self.site_id}] Safety cap of {self._PAGE_CAP} pages reached. Stopping.")
                break

            list_url = f"{self._LIST_BASE}?YY={year}"
            page_count += 1

            if page_count % 10 == 0:
                print(f"[{self.site_id}] page {page_count}: saved {saved}/{limit_str}")

            html = self._curl(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch year {year} listing. Skipping.")
                continue

            items = self._parse_listing(html)
            if not items:
                print(f"[{self.site_id}] Year {year}: no articles found.")
                continue

            print(f"[{self.site_id}] Year {year}: {len(items)} articles.")

            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget reached mid-year.")
                    break

                art_url = item["url"]
                if art_url in seen_urls:
                    continue
                seen_urls.add(art_url)

                # Slug = last path segment, used as external_id and post_number
                slug = art_url.rstrip("/").split("/")[-1]

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl(art_url)
                    if not detail_html:
                        print(f"[{self.site_id}] Failed to fetch: {art_url}")
                        continue

                    detail = self._parse_detail(detail_html, art_url)

                    title = detail["title"] or item["title"]
                    if not title:
                        print(f"[{self.site_id}] No title at {art_url}, skipping.")
                        continue

                    abstract = detail["abstract"]
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] Short abstract ({len(abstract)} chars) "
                            f"at {art_url}, skipping."
                        )
                        continue

                    pub_date = detail["published_date"]
                    listed_date_raw = item["listed_date"]
                    listed_date = _parse_date_it(listed_date_raw) if listed_date_raw else pub_date

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "url": art_url,
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "authors": None,
                        "publisher": (
                            "Ministero dell'Agricoltura, "
                            "della Sovranità Alimentare e delle Foreste"
                        ),
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "Comunicati stampa",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date_raw,
                                "slug": slug,
                                "year": year,
                                "category": "Comunicati stampa",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item {art_url} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
