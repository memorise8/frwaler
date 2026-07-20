# -*- coding: utf-8 -*-
"""CE Delft reports crawler — https://cedelft.eu/reports/

Discovery: two XML sitemaps (report-sitemap.xml + report-sitemap2.xml)
detail pages at https://cedelft.eu/publications/<slug>/

Fields extracted:
  title        — <h1> in single-header section
  abstract     — <section id="report-content"> paragraphs
  published_date — JSON-LD datePublished (ISO date)
  listed_date  — sitemap <lastmod>
  display_date — <li class="date"> text ("April 2009")
  authors      — <div class="authors"> list
  themes       — <li class="themes"> text → used as keywords + category
  pdf_url      — first *.pdf href on page
  post_number  — WordPress post ID from body class (postid-XXXXX)
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime

# spec_from_file_location has no package context — add project root explicitly.
sys.path.insert(0, ".")

from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore


_SITEMAPS = [
    "https://cedelft.eu/report-sitemap.xml",
    "https://cedelft.eu/report-sitemap2.xml",
]

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}

# Minimum abstract length to save a record (also satisfies the >= 100 char assertion)
_MIN_ABSTRACT = 100


def _make_soup(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback. Never raises."""
    if BeautifulSoup is None or not raw:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[_make_soup] parser {parser} failed: {exc}")
    return None


def _parse_date_text(text: str) -> str | None:
    """Convert display date like 'April 2009' or 'June 2022' → 'YYYY-MM-DD'."""
    if not text:
        return None
    text = text.strip()
    # ISO already
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    # "Month YYYY"
    m = re.match(r"^([A-Za-z]+)\s+(\d{4})$", text)
    if m:
        mo = _MONTH_MAP.get(m.group(1).lower())
        if mo:
            return f"{m.group(2)}-{mo}-01"
    # strptime fallbacks
    for fmt in ("%B %Y", "%b %Y", "%d %B %Y", "%d %b %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            if "%d" not in fmt:
                return dt.strftime("%Y-%m-01")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


class CedelftEuReportsCrawler(BaseCrawler):
    site_id = "cedelft-eu-reports"
    site_name = "Custom: cedelft-eu-reports"
    base_url = "https://cedelft.eu"

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _fetch_text(self, url: str) -> str | None:
        """Fetch URL via base _request (rate-limit + 3-retry). Returns decoded text.

        Excludes 'br' from Accept-Encoding because brotli decompression is not
        natively supported by requests and brotlicffi may not be installed.
        """
        resp = self._request(url, headers={"Accept-Encoding": "gzip, deflate"})
        if resp is None:
            return None
        try:
            return resp.content.decode("utf-8", errors="replace")
        except Exception:
            return resp.text

    def _fetch_sitemaps(self) -> list[tuple[str, str | None]]:
        """Return list of (url, lastmod_str_or_None) from all sitemaps."""
        entries: list[tuple[str, str | None]] = []
        for sm_url in _SITEMAPS:
            text = self._fetch_text(sm_url)
            if not text:
                print(f"[{self.site_id}] Failed to fetch sitemap: {sm_url}")
                continue
            for item in re.findall(r"<url>(.*?)</url>", text, re.DOTALL):
                loc_m = re.search(r"<loc>(.*?)</loc>", item)
                mod_m = re.search(r"<lastmod>(.*?)</lastmod>", item)
                if loc_m:
                    loc = loc_m.group(1).strip()
                    mod = mod_m.group(1).strip() if mod_m else None
                    entries.append((loc, mod))
        return entries

    def _parse_detail(self, url: str, html: str) -> dict | None:
        """Parse a report detail page. Returns extracted field dict or None."""
        soup = _make_soup(html)
        if soup is None:
            return None

        # ── WP post ID from body class postid-XXXXX ───────────────────
        post_number = None
        body = soup.find("body")
        if body:
            body_cls = " ".join(body.get("class", []))
            pm = re.search(r"postid-(\d+)", body_cls)
            if pm:
                post_number = pm.group(1)

        # ── Stable slug from URL (fallback if post ID unavailable) ────
        slug_m = re.search(r"/publications/([^/?#]+)/?$", url)
        slug = slug_m.group(1) if slug_m else None
        # Use numeric post ID as external_id (→ post_number in libertree)
        external_id = post_number or slug or url

        # ── Title ─────────────────────────────────────────────────────
        title = None
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(separator=" ", strip=True)

        # ── Published date from JSON-LD ───────────────────────────────
        published_date = None
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                graph = data.get("@graph", [data])
                if not isinstance(graph, list):
                    graph = [graph]
                for item in graph:
                    if isinstance(item, dict):
                        dp = item.get("datePublished")
                        if dp:
                            published_date = str(dp)[:10]
                            break
            except Exception:
                pass
            if published_date:
                break

        # ── Display date from <li class="date"> ───────────────────────
        display_date = None
        date_li = soup.find("li", class_="date")
        if date_li:
            date_text = date_li.get_text(strip=True)
            display_date = _parse_date_text(date_text)

        final_date = published_date or display_date

        # ── Abstract from #report-content ─────────────────────────────
        abstract = ""
        sec = soup.find("section", id="report-content")
        if sec:
            text_div = sec.find("div", class_="text")
            if text_div:
                # Exclude the authors sub-div to avoid name contamination
                for tag in text_div.find_all("div", class_="authors"):
                    tag.decompose()
                paras = text_div.find_all("p")
                abstract = " ".join(
                    p.get_text(separator=" ", strip=True) for p in paras
                )

        # Fallback: og:description / meta description
        if len(abstract) < _MIN_ABSTRACT:
            for attr_name, attr_val in [
                ("property", "og:description"),
                ("name", "description"),
            ]:
                meta = soup.find("meta", attrs={attr_name: attr_val})
                if meta:
                    content = meta.get("content") or ""
                    if len(content) > len(abstract):
                        abstract = content
                        break

        # ── Authors ───────────────────────────────────────────────────
        authors: list[str] = []
        authors_div = soup.find("div", class_="authors")
        if authors_div:
            for li in authors_div.find_all("li"):
                name = li.get_text(separator=" ", strip=True)
                if name:
                    authors.append(name)

        # ── Themes / keywords ─────────────────────────────────────────
        themes: list[str] = []
        themes_li = soup.find("li", class_="themes")
        if themes_li:
            raw = themes_li.get_text(separator=",", strip=True)
            themes = [t.strip() for t in re.split(r"[,;]", raw)
                      if t.strip() and len(t.strip()) > 1]

        # ── PDF URL ───────────────────────────────────────────────────
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.pdf(\?|#|$)", href, re.IGNORECASE):
                pdf_url = href
                fn_m = re.search(r"/([^/]+\.pdf)", href, re.IGNORECASE)
                if fn_m:
                    original_filename = fn_m.group(1)
                break

        return {
            "external_id": external_id,
            "post_number": post_number,
            "slug": slug,
            "title": title,
            "abstract": abstract,
            "published_date": final_date,
            "display_date": display_date,
            "authors": authors,
            "themes": themes,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Crawl CE Delft reports via XML sitemaps + detail page parsing.

        Parameters
        ----------
        limit:
            Maximum number of records to save. None = unlimited.
        """
        start_ts = time.time()
        max_wall = 25 * 60  # 25-minute wall clock budget

        print(f"[{self.site_id}] Fetching sitemaps...")
        entries = self._fetch_sitemaps()
        print(f"[{self.site_id}] Found {len(entries)} URLs in sitemaps")

        seen_urls: set[str] = set()
        saved = 0
        limit_disp = str(limit) if limit is not None else "inf"

        for idx, (url, lastmod) in enumerate(entries):
            # Limit check
            if limit is not None and saved >= limit:
                break

            # Wall clock budget
            elapsed = time.time() - start_ts
            if elapsed > max_wall:
                print(
                    f"[{self.site_id}] Wall clock budget reached "
                    f"({max_wall}s). Stopping."
                )
                break

            # Dedup across pages
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Progress log every 10 items
            if idx > 0 and idx % 10 == 0:
                print(
                    f"[{self.site_id}] page {idx}: saved {saved}/{limit_disp}"
                )

            # Fetch detail page (base _request handles 1s delay + 3 retries)
            html = self._fetch_text(url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch {url}, skipping")
                continue

            # Per-item failure isolation
            try:
                parsed = self._parse_detail(url, html)
                if not parsed:
                    print(f"[{self.site_id}] item {url} failed: parse returned None")
                    continue

                abstract = parsed.get("abstract", "")
                if len(abstract) < _MIN_ABSTRACT:
                    print(
                        f"[{self.site_id}] abstract too short "
                        f"({len(abstract)} chars) at {url}, skipping"
                    )
                    continue

                listed_date = lastmod[:10] if lastmod else None
                authors_list = parsed.get("authors", [])
                themes = parsed.get("themes", [])

                paper = {
                    "site_id": self.site_id,
                    "external_id": parsed["external_id"],
                    "url": url,
                    "title": parsed["title"] or "(untitled)",
                    "abstract": abstract,
                    "published_date": parsed["published_date"],
                    "posted_date": listed_date,
                    "authors": "; ".join(authors_list) if authors_list else None,
                    "publisher": "CE Delft",
                    "pdf_url": parsed.get("pdf_url"),
                    "original_filename": parsed.get("original_filename"),
                    "keywords": ", ".join(themes) if themes else None,
                    "metadata": json.dumps(
                        {
                            "posted_date": lastmod,
                            "themes": themes,
                            "post_id": parsed.get("post_number"),
                            "slug": parsed.get("slug"),
                            "display_date": parsed.get("display_date"),
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                title_preview = (parsed.get("title") or "")[:60]
                print(
                    f"[{self.site_id}] Saved {saved}/{limit_disp}: "
                    f"{title_preview}"
                )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {url} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
