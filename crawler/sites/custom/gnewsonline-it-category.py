# -*- coding: utf-8 -*-
"""Crawler for gnewsonline.it – Comunicati Stampa category.

Italian Ministry of Justice press-release archive.
Starting URL: https://www.gnewsonline.it/category/comunicati-stampa/

HTML-only crawl (WP REST API blocked via maintenance-mode redirect).
Pagination: /category/comunicati-stampa/page/N/  (~130 pages, ~10 articles/page).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_SITE_ID = "gnewsonline-it-category"
_BASE_URL = "https://www.gnewsonline.it"
_LIST_URL = "https://www.gnewsonline.it/category/comunicati-stampa/"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 100

_ITALIAN_MONTHS = {
    "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
    "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
    "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12",
}


def _parse_italian_date(raw: str) -> str:
    """'12 Maggio 2026' → '2026-05-12'. Returns '' on failure."""
    if not raw:
        return ""
    parts = raw.strip().split()
    if len(parts) == 3:
        day, month_it, year = parts
        month_num = _ITALIAN_MONTHS.get(month_it.lower())
        if month_num:
            try:
                return f"{int(year):04d}-{month_num}-{int(day):02d}"
            except ValueError:
                pass
    return ""


def _make_soup(html: str):
    """html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


class GnewsonlineItCategoryCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: gnewsonline-it-category"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, timeout: int = 45,
                  referer: str | None = None) -> str | None:
        """GET via curl with 3-attempt exponential backoff. Returns text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*;q=0.8",
            "-H", "Accept-Language: it-IT,it;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10
                )
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                wait = 3 ** attempt  # 1 s, 3 s
                if attempt < 2:
                    print(f"[{_SITE_ID}] empty response for {url}, "
                          f"retry in {wait}s...")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    print(f"[{_SITE_ID}] timeout for {url}, retry...")
                    time.sleep(3)
            except Exception as exc:
                wait = 3 ** attempt
                if attempt < 2:
                    print(f"[{_SITE_ID}] curl error ({exc}), retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{_SITE_ID}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> tuple[list[str], bool]:
        """Return (article_urls, has_next) for a category listing page."""
        url = _LIST_URL if page == 1 else f"{_LIST_URL}page/{page}/"
        raw = self._curl_get(url, referer=_BASE_URL)
        if not raw:
            return [], False

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse error on listing page {page}: {exc}")
            return [], False

        # id="cont" is an <a> anchor (skip target), not a container — search full page
        cont = soup

        urls: list[str] = []
        seen_in_page: set[str] = set()

        # Top-2 featured articles are in h3, rest are in h5
        for tag in ("h3", "h5"):
            for heading in cont.find_all(tag):
                a = heading.find("a", href=True)
                if not a:
                    continue
                href = a["href"].strip()
                if (
                    href.startswith(_BASE_URL)
                    and "/category/" not in href
                    and "/page/" not in href
                    and "/wp-" not in href
                    and "/author/" not in href
                    and "/tag/" not in href
                    and href not in seen_in_page
                ):
                    seen_in_page.add(href)
                    urls.append(href)

        # Next-page detection: <link rel="next"> in <head>
        link_next = soup.find("link", rel=lambda r: r and "next" in r)
        has_next = bool(link_next)

        return urls, has_next

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict | None:
        """Fetch and parse a single article page. Returns a dict or None."""
        raw = self._curl_get(url, referer=_LIST_URL)
        if not raw:
            return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse error on detail {url}: {exc}")
            return None

        # --- Post ID (WordPress numeric ID) ---
        post_id = ""
        body = soup.find("body")
        if body:
            body_class = " ".join(body.get("class", []))
            m = re.search(r"\bpostid-(\d+)\b", body_class)
            if m:
                post_id = m.group(1)
        if not post_id:
            sp_div_tmp = soup.find("div", class_=re.compile(r"\bsingle-post\b"))
            if sp_div_tmp:
                sp_class = " ".join(sp_div_tmp.get("class", []))
                m = re.search(r"\bpost-(\d+)\b", sp_class)
                if m:
                    post_id = m.group(1)

        # --- Single-post container ---
        sp_div = soup.find("div", class_=re.compile(r"\bsingle-post\b"))

        # --- Title ---
        title = ""
        if sp_div:
            h = sp_div.find(re.compile(r"^h[123]$"))
            if h:
                title = h.get_text(separator=" ", strip=True)
        if not title:
            og = soup.find("meta", property="og:title")
            if og:
                title = og.get("content", "").strip()
        if not title:
            t = soup.find("title")
            if t:
                title = t.get_text(strip=True).split(" - ")[0].strip()

        # --- Date ---
        date_raw = ""
        date_span = soup.find("span", class_=re.compile(r"featured-post-date"))
        if date_span:
            date_raw = date_span.get_text(strip=True)
        published_date = _parse_italian_date(date_raw)

        # --- Author ---
        author = ""
        author_ul = soup.find("ul", class_="author")
        if author_ul:
            links = author_ul.find_all("a")
            if links:
                author = "; ".join(a.get_text(strip=True) for a in links)

        # --- Abstract (full body text) ---
        abstract = ""
        if sp_div:
            # Clone to avoid mutating the tree between calls
            import copy
            sp_copy = copy.copy(sp_div)

            # Strip noise nodes before text extraction
            for noise_cls in ("addtoany", "a2a_kit", "author", "download-attachment"):
                for node in sp_copy.find_all(
                    True,
                    class_=re.compile(rf"\b{noise_cls}\b", re.I)
                ):
                    node.decompose()
            for h in sp_copy.find_all(re.compile(r"^h[1-6]$")):
                h.decompose()
            for ds in sp_copy.find_all("span", class_=re.compile(r"featured-post-date")):
                ds.decompose()

            # Prefer <p> paragraphs; fallback to full div text
            paras = sp_copy.find_all("p")
            if paras:
                parts = [
                    unescape(p.get_text(separator=" ", strip=True))
                    for p in paras
                    if p.get_text(strip=True)
                ]
                abstract = "\n\n".join(parts)
            if not abstract:
                abstract = unescape(
                    sp_copy.get_text(separator="\n", strip=True)
                )

        # Normalize whitespace
        abstract = re.sub(r"\n{3,}", "\n\n", abstract).strip()

        # --- PDF attachment (if any) ---
        pdf_url = None
        pdf_a = soup.find("a", href=re.compile(r"\.pdf(\?|$)", re.I))
        if pdf_a:
            href = pdf_a["href"]
            pdf_url = href if href.startswith("http") else urljoin(_BASE_URL, href)

        original_filename = ""
        if pdf_url:
            seg = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if seg.lower().endswith(".pdf"):
                original_filename = seg

        return {
            "post_id": post_id,
            "title": title,
            "date_raw": date_raw,
            "published_date": published_date,
            "author": author,
            "abstract": abstract,
            "pdf_url": pdf_url or "",
            "original_filename": original_filename,
            "url": url,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl comunicati-stampa category, saving up to ``limit`` items.

        Walks pages until (a) saved >= limit, (b) page returns 0 new records,
        or (c) the 200-page safety cap is reached.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

        page = 1
        while page <= _MAX_PAGES:
            # Wall-clock budget
            if time.time() - start_time > max_wall:
                print(
                    f"[{_SITE_ID}] 25-minute wall budget reached at page {page}. "
                    "Exiting cleanly."
                )
                break

            if limit is not None and saved >= limit:
                break

            article_urls, has_next = self._fetch_list_page(page)

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            if not article_urls:
                print(f"[{_SITE_ID}] No articles on page {page}. Done.")
                break

            new_on_page = 0
            for art_url in article_urls:
                if limit is not None and saved >= limit:
                    break

                if art_url in seen_urls:
                    continue
                seen_urls.add(art_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(art_url)
                    if not detail:
                        print(f"[{_SITE_ID}] fetch failed, skipping: {art_url}")
                        continue

                    abstract = detail.get("abstract", "")
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] abstract too short "
                            f"({len(abstract)} chars), skipping: {art_url}"
                        )
                        continue

                    post_id = detail.get("post_id") or ""
                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": post_id if post_id else art_url,
                        "post_number": post_id or None,
                        "title": detail.get("title") or "(untitled)",
                        "abstract": abstract,
                        "published_date": detail.get("published_date") or "",
                        "listed_date": detail.get("published_date") or "",
                        "url": art_url,
                        "authors": detail.get("author") or "",
                        "publisher": "gNews / Ministero della Giustizia",
                        "department": "",
                        "journal": "",
                        "pdf_url": detail.get("pdf_url") or "",
                        "keywords": "",
                        "category": "Comunicati Stampa",
                        "doi": "",
                        "original_filename": detail.get("original_filename") or "",
                        "metadata": json.dumps({
                            "posted_date": detail.get("date_raw") or "",
                            "originalFilename": detail.get("original_filename") or "",
                            "category_slug": "comunicati-stampa",
                            "post_id": post_id,
                        }, ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{_SITE_ID}] saved {saved}"
                        + (f"/{limit}" if limit is not None else "")
                        + f": {detail['title'][:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({art_url}): {exc}")
                    continue

            # End-of-pagination checks
            if new_on_page == 0:
                print(f"[{_SITE_ID}] All URLs on page {page} already seen. Done.")
                break

            if not has_next:
                print(f"[{_SITE_ID}] No next page after page {page}. Done.")
                break

            page += 1

        if page > _MAX_PAGES:
            print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
