# -*- coding: utf-8 -*-
"""Crawler for Andra (France) publications.

Starting URL: https://www.andra.fr/publications?f%5B0%5D=facet_doc_cat%3A230

Access strategy
---------------
www.andra.fr sits behind an F5 BIG-IP bot-mitigation layer:

  1. GET /publications?...  →  200 + short cookie c1 + JS redirect URL.
  2. GET /redirect_XXX/...  with c1  →  307 + long session cookie c2.
  3. GET /publications?...  with c2  →  200 real HTML.

Paginated URLs (?page=N, N≥1) trigger a Drupal reCAPTCHA challenge even via
Playwright.  Instead we use Drupal's Views AJAX endpoint (/views/ajax), which
returns JSON containing the rendered article HTML without reCAPTCHA.

We extract the `view_dom_id` from the initial page load and pass it to each
AJAX request.  The pager embedded in the AJAX response tells us the last page
number so we know when to stop.

PDFs (at /sites/default/files/...) are served without bot-protection and can
be fetched with plain curl.  We run pdftotext on the first 3 pages to obtain
the abstract (the only source of body text — article cards have no description
field).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

# Absolute-import safety (spec_from_file_location has no package context)
_pkg_root = Path(__file__).resolve().parents[3]
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_BASE      = "https://www.andra.fr"
_START_URL = f"{_BASE}/publications?f%5B0%5D=facet_doc_cat%3A230"
_AJAX_URL  = f"{_BASE}/views/ajax"


# ---------------------------------------------------------------------------
# HTML parsing helper
# ---------------------------------------------------------------------------

def _bs4_parse(html: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class AndraFrPublicationsCrawler(BaseCrawler):

    site_id   = "andra-fr-publications"
    site_name = "Custom: andra-fr-publications"
    base_url  = "https://www.andra.fr"

    _PDF_PAGES         = 3     # PDF pages to extract for abstract
    _MAX_PAGES         = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))   # hard safety cap
    _CRAWL_BUDGET_SECS = 1500  # 25-minute wall-clock limit

    # ------------------------------------------------------------------ #
    # Low-level curl helpers
    # ------------------------------------------------------------------ #

    def _curl(self, url: str, cookie: str | None = None,
              extra_headers: list[str] | None = None,
              post_data: str | None = None) -> str | None:
        """Run curl, return raw stdout (headers + body) or None on failure."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "-D", "-",
            "-H", f"User-Agent: {self.USER_AGENT}",
        ]
        if cookie:
            cmd += ["-H", f"Cookie: {cookie}"]
        if extra_headers:
            for h in extra_headers:
                cmd += ["-H", h]
        if post_data is not None:
            cmd += ["-X", "POST", "--data", post_data]
        cmd.append(url)

        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=60)
                text = r.stdout.decode("utf-8", errors="replace")
                if text:
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}): {exc}")
            if attempt < 2:
                time.sleep(3 ** attempt)
        return None

    @staticmethod
    def _split_hb(raw: str) -> tuple[str, str]:
        if not raw:
            return "", ""
        for sep in ("\r\n\r\n", "\n\n"):
            idx = raw.find(sep)
            if idx >= 0:
                return raw[:idx], raw[idx + len(sep):]
        return "", raw

    @staticmethod
    def _pick_cookie(raw: str) -> str | None:
        m = re.search(r"set-cookie: (bot_mitigation_cookie=[^;\r\n]+)", raw or "", re.I)
        return m.group(1) if m else None

    @staticmethod
    def _pick_redir_js(raw: str) -> str | None:
        m = re.search(r"window\.location\.href='([^']+)'", raw or "")
        return m.group(1) if m else None

    # ------------------------------------------------------------------ #
    # Bot-protection handshake
    # ------------------------------------------------------------------ #

    def _handshake(self, start_url: str | None = None) -> str | None:
        """3-step F5 bot-mitigation handshake.  Returns c2 cookie or None."""
        if start_url is None:
            start_url = _START_URL
        raw1 = self._curl(start_url)
        if not raw1:
            return None
        c1 = self._pick_cookie(raw1)
        redir = self._pick_redir_js(raw1)
        if not redir:
            return c1  # no challenge
        url2 = redir if redir.startswith("http") else f"{_BASE}{redir}"
        raw2 = self._curl(url2, cookie=c1)
        if not raw2:
            return None
        return self._pick_cookie(raw2)

    def _load_html(self, url: str, cookie: str) -> tuple[str | None, str]:
        """Fetch a URL with bot-protection cookie. Returns (body, cookie)."""
        raw = self._curl(url, cookie=cookie)
        if not raw:
            return None, cookie
        _, body = self._split_hb(raw)
        if self._pick_redir_js(body):
            # Cookie expired — refresh
            print(f"[{self.site_id}] Cookie stale; refreshing …")
            cookie = self._handshake(url) or cookie
            raw2 = self._curl(url, cookie=cookie)
            if raw2:
                _, body = self._split_hb(raw2)
        return (body if body and len(body) > 200 else None), cookie

    # ------------------------------------------------------------------ #
    # View configuration
    # ------------------------------------------------------------------ #

    def _get_view_dom_id(self, html: str) -> str | None:
        """Extract view_dom_id from drupal-settings-json embedded in page HTML."""
        ds_m = re.search(
            r'data-drupal-selector="drupal-settings-json"[^>]*>(.*?)</script>',
            html, re.S,
        )
        if not ds_m:
            return None
        try:
            ds = json.loads(ds_m.group(1))
            ajax_views = ds.get("views", {}).get("ajaxViews", {})
            if ajax_views:
                return list(ajax_views.values())[0].get("view_dom_id")
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # Views AJAX pagination
    # ------------------------------------------------------------------ #

    def _ajax_page(self, page_num: int, cookie: str,
                   view_dom_id: str) -> str | None:
        """POST to /views/ajax and return the insert-command HTML, or None."""
        post_data = "&".join([
            "view_name=solr_publication",
            "view_display_id=page_1",
            f"view_dom_id={view_dom_id}",
            "view_path=%2Fpublications",
            "view_base_path=publications",
            "pager_element=0",
            f"page={page_num}",
        ])
        for attempt in range(3):
            raw = self._curl(
                _AJAX_URL,
                cookie=cookie,
                extra_headers=[
                    "X-Requested-With: XMLHttpRequest",
                    "Content-Type: application/x-www-form-urlencoded",
                    "Accept: application/json",
                    f"Referer: {_START_URL}",
                ],
                post_data=post_data,
            )
            if not raw:
                time.sleep(3 ** attempt)
                continue
            # raw may start with HTTP headers if curl -D - was used; strip them
            body = raw
            if raw.startswith("HTTP/"):
                _, body = self._split_hb(raw)
            try:
                cmds = json.loads(body)
                for cmd in cmds:
                    if cmd.get("command") == "insert" and cmd.get("data"):
                        return cmd["data"]
                return ""  # valid JSON but no insert → end of results
            except Exception as exc:
                print(f"[{self.site_id}] AJAX parse error page {page_num}: {exc}")
                time.sleep(3 ** attempt)
        return None

    @staticmethod
    def _extract_last_page(html: str) -> int:
        """Return max page index from pager links (0-based)."""
        nums = re.findall(r'href="[^"]*page=(\d+)[^"]*"', html)
        return max((int(n) for n in nums), default=0)

    # ------------------------------------------------------------------ #
    # Article parsing
    # ------------------------------------------------------------------ #

    def _parse_articles(self, html: str) -> list[dict]:
        try:
            soup = _bs4_parse(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
            return []
        if soup is None:
            return []

        items = []
        for art in soup.find_all("article", class_="publication"):
            try:
                h2 = art.find("h2", class_="publication-title")
                if not h2:
                    continue
                link = h2.find("a")
                if not link:
                    continue
                title = link.get_text(strip=True)
                if not title:
                    continue

                href = link.get("href", "").strip()
                if not href:
                    continue
                if href.startswith("/"):
                    href = _BASE + href

                # Prefer explicit download link
                dl_wrap = art.find("div", class_=re.compile(r"\blink\b"))
                if dl_wrap:
                    dl_a = dl_wrap.find("a")
                    if dl_a:
                        dl_href = dl_a.get("href", "").strip()
                        if dl_href.lower().endswith(".pdf"):
                            href = _BASE + dl_href if dl_href.startswith("/") else dl_href

                pdf_url = href

                cat_el   = art.find("a", class_="publication-category")
                category = cat_el.get_text(strip=True) if cat_el else ""

                # Date — outer time element has the clean ISO date
                pub_date = None
                date_el  = art.find("time", class_="publication-date")
                if date_el:
                    dt = date_el.get("datetime", "")
                    pub_date = dt[:10] if len(dt) >= 10 else None
                if not pub_date:
                    inner_t = art.find("time", attrs={"datetime": True})
                    if inner_t:
                        dt = inner_t.get("datetime", "")
                        pub_date = dt[:10] if len(dt) >= 10 else None

                filename = unquote(urlparse(pdf_url).path.split("/")[-1])

                items.append({
                    "title":             title,
                    "pdf_url":           pdf_url,
                    "category":          category,
                    "published_date":    pub_date,
                    "original_filename": filename,
                })
            except Exception as exc:
                print(f"[{self.site_id}] article parse error: {exc}")
                continue
        return items

    # ------------------------------------------------------------------ #
    # PDF abstract extraction
    # ------------------------------------------------------------------ #

    def _extract_pdf_abstract(self, pdf_url: str) -> str | None:
        """Download PDF via curl and extract first few pages with pdftotext."""
        tmp = f"/tmp/andra_crawl_{os.getpid()}.pdf"
        try:
            cmd = [
                "curl", "-sk", "--tls-max", "1.3", "--max-time", "90",
                "-H", f"User-Agent: {self.USER_AGENT}",
                "-o", tmp, pdf_url,
            ]
            for attempt in range(3):
                try:
                    r = subprocess.run(cmd, timeout=100)
                    if (r.returncode == 0
                            and os.path.exists(tmp)
                            and os.path.getsize(tmp) > 500):
                        break
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(3 ** attempt)
            else:
                return None

            r2 = subprocess.run(
                ["pdftotext", "-f", "1", "-l", str(self._PDF_PAGES), tmp, "-"],
                capture_output=True, timeout=30,
            )
            text = r2.stdout.decode("utf-8", errors="replace")
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text if len(text) >= 50 else None

        except Exception as exc:
            print(f"[{self.site_id}] PDF extract error for {pdf_url}: {exc}")
            return None
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Main crawl entry point
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None) -> int:
        start_ts  = time.monotonic()
        saved     = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"

        print(f"[{self.site_id}] Starting crawl (limit={limit_str})")

        # Step 1: bot-protection handshake → c2 cookie
        cookie = self._handshake()
        if not cookie:
            print(f"[{self.site_id}] Handshake failed; aborting.")
            return 0

        # Step 2: load filtered page → extract view_dom_id
        html, cookie = self._load_html(_START_URL, cookie)
        if not html:
            print(f"[{self.site_id}] Could not load start page; aborting.")
            return 0

        view_dom_id = self._get_view_dom_id(html)
        if not view_dom_id:
            print(f"[{self.site_id}] Could not extract view_dom_id; aborting.")
            return 0
        print(f"[{self.site_id}] view_dom_id={view_dom_id[:16]}…")

        # Step 3: fetch page 0 via AJAX → learn last page number
        first_ajax = self._ajax_page(0, cookie, view_dom_id)
        if not first_ajax:
            print(f"[{self.site_id}] AJAX page 0 failed; aborting.")
            return 0

        last_page = self._extract_last_page(first_ajax)
        total_pages = min(last_page + 1, self._MAX_PAGES)
        print(f"[{self.site_id}] Last page index: {last_page} ({total_pages} pages total)")

        # Step 4: iterate pages
        ajax_cache = {0: first_ajax}

        for page_num in range(total_pages):
            if time.monotonic() - start_ts > self._CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-min budget reached at page {page_num}.")
                break
            if limit is not None and saved >= limit:
                break

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            ajax_html = ajax_cache.pop(page_num, None)
            if ajax_html is None:
                ajax_html = self._ajax_page(page_num, cookie, view_dom_id)
            if not ajax_html:
                print(f"[{self.site_id}] page {page_num}: AJAX failed; stopping.")
                break

            articles = self._parse_articles(ajax_html)
            if not articles:
                print(f"[{self.site_id}] page {page_num}: no articles; done.")
                break

            new_on_page = 0
            for item in articles:
                if limit is not None and saved >= limit:
                    break

                pdf_url = item.get("pdf_url", "")
                if not pdf_url or pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    abstract = self._extract_pdf_abstract(pdf_url)

                    if not abstract or len(abstract) < 50:
                        print(
                            f"[{self.site_id}] skip '{item['title'][:50]}': "
                            f"abstract too short ({len(abstract) if abstract else 0} chars)"
                        )
                        continue

                    filename = item.get("original_filename", "")
                    ext_id   = filename[:-4] if filename.lower().endswith(".pdf") else filename
                    if not ext_id:
                        ext_id = re.sub(r"[^\w-]", "-", item["title"])[:80]

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":       ext_id,
                        "post_number":       ext_id,
                        "title":             item["title"],
                        "abstract":          abstract,
                        "url":               pdf_url,
                        "pdf_url":           pdf_url,
                        "published_date":    item.get("published_date"),
                        "posted_date":       item.get("published_date"),
                        "category":          item.get("category", ""),
                        "publisher":         "Agence nationale pour la gestion des déchets radioactifs (Andra)",
                        "original_filename": filename or None,
                        "authors":           "",
                        "keywords":          "",
                        "journal":           "",
                        "department":        "",
                        "doi":               None,
                        "metadata": json.dumps(
                            {
                                "posted_date":      item.get("published_date"),
                                "originalFilename": filename or None,
                                "category_raw":     item.get("category", ""),
                                "source_facet":     "facet_doc_cat:230",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved ({saved}/{limit_str}): {item['title'][:70]}")
                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{item.get('title','?')[:50]}' failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_num}: all duplicates; stopping.")
                break

            time.sleep(self._delay)

        if page_num >= self._MAX_PAGES - 1:
            print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")

        print(f"[{self.site_id}] Crawl complete: saved={saved}")
        return saved
