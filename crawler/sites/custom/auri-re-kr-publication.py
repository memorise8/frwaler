# -*- coding: utf-8 -*-
"""AURI 건축공간연구원 연구보고서 crawler.

Starting URL: https://www.auri.re.kr/publication/list.es?mid=a10312000000&publication_type=research
Pagination:   ?nPage=N  (10 items/page, ~560 total)
Detail page:  /publication/view.es?mid=a10312000000&publication_id=XXXX&...
PDF download: /userPublicationDownload.es?publication_type=research&publication_id=XXXX&seq=N
"""

import json
import os
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html: str):
    """Parse HTML with fallback parser chain. Returns None on total failure."""
    if _BS is None:
        return None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&#\d+;", "", s)
    s = re.sub(r"&[a-zA-Z]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(raw: str) -> str:
    """Convert '2025.12.31' / '2025-12-31' / '20251231' → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4})[./\-](\d{1,2})(?:[./\-](\d{1,2}))?", raw)
    if m:
        y, mo = m.group(1), m.group(2).zfill(2)
        d = (m.group(3) or "01").zfill(2)
        return f"{y}-{mo}-{d}"
    m2 = re.match(r"(\d{4})(\d{2})(\d{2})", raw)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return raw


class AuriReKrPublicationCrawler(BaseCrawler):
    """Crawler for AURI 건축공간연구원 연구보고서."""

    site_id   = "auri-re-kr-publication"
    site_name = "Custom: auri-re-kr-publication"
    base_url  = "https://www.auri.re.kr"

    _LIST_URL  = "https://www.auri.re.kr/publication/list.es"
    _LIST_QS   = {"mid": "a10312000000", "publication_type": "research"}
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _RATE_SLEEP = 1.0          # seconds between detail fetches
    _MAX_WALL   = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))      # 25-minute overall budget

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _curl(self, url: str, params: dict | None = None,
              head: bool = False) -> str | None:
        """GET (or HEAD) via curl with TLS/SSL workaround.

        Returns decoded response text (headers for HEAD) or None on failure.
        Retries up to 3 times with 1 s / 3 s / 9 s backoff.
        """
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            "-H", f"Referer: {self.base_url}/",
        ]
        if head:
            cmd.append("-I")
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                if attempt < 2:
                    wait = 3 ** attempt   # 1 s, 3 s
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict]:
        """Fetch one list page; return list of item-dicts (empty = no more pages)."""
        raw = self._curl(self._LIST_URL, {**self._LIST_QS, "nPage": page})
        if not raw:
            return []
        soup = _make_soup(raw)
        if soup is None:
            return []

        items: list[dict] = []
        for li in soup.select("div.galleryList li"):
            a_view = li.select_one("a[href*='publication_id=']")
            if not a_view:
                continue
            m = re.search(r"publication_id=(\d+)", a_view.get("href", ""))
            if not m:
                continue
            pub_id = m.group(1)

            title_el  = li.select_one("strong.title")
            title     = title_el.get_text(strip=True) if title_el else ""

            cat_el    = li.select_one("span.category b")
            category  = cat_el.get_text(strip=True) if cat_el else ""

            date_el   = li.select_one("span.date b")
            date_raw  = date_el.get_text(strip=True) if date_el else ""

            writer_el   = li.select_one("span.writer")
            first_author = writer_el.get_text(strip=True) if writer_el else ""

            tags     = li.select("span.tag a")
            kw_list  = [t.get_text(strip=True).lstrip("#") for t in tags]

            dl_a     = li.select_one("span.btn a[href*='Download']")
            dl_href  = dl_a.get("href", "") if dl_a else ""

            items.append({
                "pub_id":       pub_id,
                "title":        title,
                "category":     category,
                "date_raw":     date_raw,
                "first_author": first_author,
                "kw_list":      kw_list,
                "dl_href":      dl_href,
                "url": (
                    f"{self.base_url}/publication/view.es"
                    f"?mid=a10312000000&publication_id={pub_id}"
                    f"&nPage={page}&publication_type=research"
                ),
            })
        return items

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    def _fetch_detail(self, pub_id: str, npage: int = 1) -> dict:
        """Fetch detail page; return dict with title, authors, abstract, etc."""
        raw = self._curl(
            f"{self.base_url}/publication/view.es",
            {
                "mid":              "a10312000000",
                "publication_id":   pub_id,
                "nPage":            npage,
                "publication_type": "research",
            },
        )
        if not raw:
            return {}
        soup = _make_soup(raw)
        if soup is None:
            return {}

        result: dict = {}

        # Full title from OG meta (strip site-name suffix)
        og = soup.find("meta", property="og:title")
        if og:
            t = re.sub(r"\s*\|.*$", "", og.get("content", "")).strip()
            if t:
                result["title"] = t

        # Report series  e.g. "일반연구보고서 2025-13"
        series_el = soup.select_one("li.category strong")
        if series_el:
            result["report_series"] = series_el.get_text(strip=True)

        # Published date from detail header
        date_el = soup.select_one("li.date strong")
        if date_el:
            result["date_raw"] = date_el.get_text(strip=True)

        # All authors (detail page shows full list with job titles)
        authors: list[str] = []
        for w in soup.select("span.writer"):
            name = w.get_text(strip=True)
            if name:
                authors.append(name)
        result["authors"] = authors

        # Abstract: find div.tb_contents whose strong.tb_tit == "요약"
        for div in soup.select("div.tb_contents"):
            tit = div.select_one("strong.tb_tit")
            if tit and tit.get_text(strip=True) == "요약":
                tit.decompose()
                result["abstract"] = _strip_tags(str(div)).strip()
                break

        # Download URL: prefer /publicationDownload, fallback /userPublicationDownload
        for sel in [
            "a[href*='publicationDownload']",
            "a[href*='userPublicationDownload']",
        ]:
            el = soup.select_one(sel)
            if el:
                result["dl_href"] = el.get("href", "")
                break

        return result

    # ------------------------------------------------------------------
    # Filename from Content-Disposition
    # ------------------------------------------------------------------

    def _original_filename(self, pdf_url: str) -> str:
        """HEAD-request the PDF URL; extract filename from Content-Disposition."""
        if not pdf_url:
            return ""
        full = pdf_url if pdf_url.startswith("http") else f"{self.base_url}{pdf_url}"
        headers_text = self._curl(full, head=True)
        if not headers_text:
            return ""
        m = re.search(
            r"Content-Disposition:.*?filename=\"?([^\"\r\n;]+)",
            headers_text,
            re.IGNORECASE,
        )
        if not m:
            return ""
        fname = m.group(1).strip().strip('"').strip("'")
        try:
            fname = urllib.parse.unquote(fname)
        except Exception:
            pass
        return fname

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl AURI 연구보고서 list and save to DB. Returns saved count."""
        saved      = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        cap        = limit if limit is not None else float("inf")

        for page in range(1, self._MAX_PAGES + 1):
            # ── budget / limit checks ──────────────────────────────────
            if saved >= cap:
                break
            if time.monotonic() - start_time > self._MAX_WALL:
                print(f"[{self.site_id}] 25-min wall-clock budget reached at page {page}. Stopping.")
                break
            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")

            if page % 10 == 0:
                lbl = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lbl}")

            # ── fetch list page ────────────────────────────────────────
            try:
                items = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} list-fetch failed: {exc}")
                continue

            if not items:
                print(f"[{self.site_id}] page {page}: no items returned. Done.")
                break

            # Dedup check — if every URL on this page was already seen,
            # the paginator is looping; stop.
            new_on_page = [it for it in items if it["url"] not in seen_urls]
            if not new_on_page:
                print(f"[{self.site_id}] page {page}: all items already seen. Done.")
                break

            # ── per-item processing ────────────────────────────────────
            for item in items:
                if saved >= cap:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                pub_id = item["pub_id"]
                try:
                    time.sleep(self._RATE_SLEEP)
                    detail = self._fetch_detail(pub_id, npage=page)

                    # Title
                    title = detail.get("title") or item["title"]
                    if not title:
                        print(f"[{self.site_id}] item {pub_id}: no title, skipping.")
                        continue

                    # Abstract (must be ≥ 50 chars to be worth saving)
                    abstract = detail.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {pub_id}: abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    # Authors
                    author_list = (
                        detail.get("authors")
                        or ([item["first_author"]] if item["first_author"] else [])
                    )
                    authors_str = "; ".join(a for a in author_list if a)

                    # Date
                    date_raw       = detail.get("date_raw") or item["date_raw"]
                    published_date = _parse_date(date_raw)

                    # Keywords (comma-separated, stripped of # prefix)
                    keywords_str = ", ".join(item["kw_list"])

                    # PDF URL
                    dl_href = detail.get("dl_href") or item["dl_href"]
                    pdf_url = ""
                    if dl_href:
                        pdf_url = (
                            dl_href if dl_href.startswith("http")
                            else f"{self.base_url}{dl_href}"
                        )

                    # Original filename via Content-Disposition HEAD
                    orig_filename = ""
                    if pdf_url:
                        try:
                            orig_filename = self._original_filename(pdf_url)
                        except Exception:
                            pass

                    report_series = detail.get("report_series", "")

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":       pub_id,
                        "post_number":       pub_id,
                        "title":             title,
                        "abstract":          abstract,
                        "published_date":    published_date,
                        "listed_date":       published_date,
                        "authors":           authors_str,
                        "publisher":         "건축공간연구원",
                        "journal":           "",
                        "url":               url,
                        "pdf_url":           pdf_url,
                        "keywords":          keywords_str,
                        "category":          item["category"],
                        "doi":               "",
                        "original_filename": orig_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date":      date_raw,
                                "originalFilename": orig_filename,
                                "publication_id":   pub_id,
                                "report_series":    report_series,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lbl = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] saved {saved}/{lbl}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pub_id} failed: {exc}")
                    continue

            time.sleep(0.5)   # light inter-page pause

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
