# -*- coding: utf-8 -*-
"""Crawler for ASNR (Autorité de sûreté nucléaire et de radioprotection)
"rapports d'expertise" listing.

Starting URL:
https://reglementation-controle.asnr.fr/information/publications/rapports-d-expertise

HTML site (eZ Platform CMS). Paginated list: page 1 has no ?page= param,
then ?page=2, ?page=3 … Each item links to a detail page with full body
(.RichText) and PDF download links under /content/download/{id}/...
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_REAL_BASE = "https://reglementation-controle.asnr.fr"
_LIST_PATH = "/information/publications/rapports-d-expertise"

_CURL_TIMEOUT = 60
_BACKOFF = (1, 3, 9)
_MIN_ABSTRACT = 50
_MAX_ABSTRACT = 6000
_PAGE_CAP = 200
_BUDGET_SECS = 25 * 60
_DELAY = 1.0

_MONTHS_FR = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
}


class ReglementationControleAsnrFrInformationCrawler(BaseCrawler):
    site_id = "reglementation-controle-asnr-fr-information"
    site_name = "Custom: reglementation-controle-asnr-fr-information"
    base_url = "https://reglementation-controle.asnr.fr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        page = 1
        start_time = time.time()

        while True:
            if time.time() - start_time > _BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached, stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _PAGE_CAP:
                print(f"[{self.site_id}] Safety cap of {_PAGE_CAP} pages reached, stopping.")
                break

            # Page 1 has no ?page= param; subsequent pages use ?page=N
            list_url = (
                f"{_REAL_BASE}{_LIST_PATH}"
                if page == 1
                else f"{_REAL_BASE}{_LIST_PATH}?page={page}"
            )

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            try:
                raw = self._curl_get(list_url, context=f"list page {page}")
            except KeyboardInterrupt:
                raise
            if not raw:
                print(f"[{self.site_id}] List page {page} fetch failed, stopping.")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] List page {page} parse failed, stopping.")
                break

            articles = soup.select("article.Teaser")
            if not articles:
                print(f"[{self.site_id}] No articles on page {page}, stopping.")
                break

            new_on_page = 0

            for art in articles:
                if limit is not None and saved >= limit:
                    break

                try:
                    link_el = art.select_one(".Teaser-titleLink")
                    if link_el is None:
                        continue
                    title = self._clean_text(link_el.get_text(" ", strip=True))
                    href = link_el.get("href", "")
                    if not href:
                        continue
                    detail_url = urljoin(_REAL_BASE, href)

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    # Listed date: "Publié le DD/MM/YYYY"
                    date_el = art.select_one(".Teaser-date")
                    listed_date = _parse_date_fr(date_el.get_text(" ", strip=True) if date_el else "")

                    # Teaser abstract from list page (fallback)
                    teaser_el = art.select_one(".Teaser-text .eztext-field")
                    teaser_text = self._clean_text(teaser_el.get_text(" ", strip=True)) if teaser_el else ""

                    slug = urlparse(detail_url).path.rstrip("/").rsplit("/", 1)[-1]

                    # Fetch detail page
                    time.sleep(_DELAY)
                    detail_raw = self._curl_get(detail_url, context=f"detail {slug}")

                    abstract = ""
                    pdf_url = None
                    published_date = listed_date
                    original_filename = None
                    post_number = None

                    if detail_raw:
                        dsoup = self._make_soup(detail_raw, context=f"detail {slug}")
                        if dsoup:
                            # Full body: main-content RichText only (skip footer error/success divs)
                            rich = dsoup.select_one(".Wrapper-gridMain .RichText")
                            if rich is None:
                                rich = dsoup.select_one(".RichText")
                            if rich:
                                parts = []
                                for field in rich.select(".ezrichtext-field"):
                                    t = self._clean_text(field.get_text(" ", strip=True))
                                    if t and len(t) > 2:
                                        parts.append(t)
                                if parts:
                                    abstract = " ".join(parts)[:_MAX_ABSTRACT]
                                else:
                                    abstract = self._clean_text(
                                        rich.get_text(" ", strip=True)
                                    )[:_MAX_ABSTRACT]

                            # Published date
                            date2 = dsoup.select_one(".ArticleHeader-date")
                            if date2:
                                parsed = _parse_date_fr(date2.get_text(" ", strip=True))
                                if parsed:
                                    published_date = parsed

                            # First PDF link → main report
                            for a in dsoup.find_all("a", href=True):
                                h = a.get("href", "")
                                if not h:
                                    continue
                                h_low = h.lower()
                                if h_low.endswith(".pdf") or (
                                    "/content/download/" in h_low and "/file/" in h_low
                                ):
                                    pdf_url = urljoin(_REAL_BASE, h)
                                    fname = urlparse(pdf_url).path.rsplit("/", 1)[-1]
                                    original_filename = unquote(fname) if fname else None
                                    m = re.search(r"/content/download/(\d+)/", h)
                                    if m:
                                        post_number = m.group(1)
                                    break

                    # Fallback abstract → teaser text from list page
                    if not abstract:
                        abstract = teaser_text

                    # Fallback abstract → meta description
                    if len(abstract) < _MIN_ABSTRACT and detail_raw:
                        dsoup2 = self._make_soup(detail_raw, context=f"detail-meta {slug}")
                        if dsoup2:
                            for meta in dsoup2.find_all("meta"):
                                name = meta.get("name", "") or meta.get("property", "")
                                if name in ("description", "og:description"):
                                    cand = self._clean_text(meta.get("content", ""))
                                    if len(cand) > len(abstract):
                                        abstract = cand

                    if not abstract or len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] skip {slug}: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    external_id = post_number or slug

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number or slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "authors": None,
                        "publisher": "ASNR",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "Rapport d'expertise",
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "originalFilename": original_filename,
                                "slug": slug,
                                "content_id": post_number,
                                "category": "Rapport d'expertise",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] saved {saved}/{lim_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] No new records on page {page}, stopping.")
                break

            if not _has_next_page(soup, page):
                print(f"[{self.site_id}] No next page after page {page}, done.")
                break
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request"):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(_CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=_CURL_TIMEOUT + 10)
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode != 0:
                    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(err or f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} attempt {attempt}/3: {last_error}")
                if attempt < 3:
                    wait = _BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s…")
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw, context="HTML"):
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _clean_text(self, text):
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------
# Module-level helpers (no self dependency)
# ------------------------------------------------------------------

def _parse_date_fr(raw):
    raw = re.sub(r"\s+", " ", (raw or "")).strip()
    # DD/MM/YYYY
    m = re.search(r"(\d{1,2})/(\d{2})/(\d{4})", raw)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    # "21 mai 2024"
    m = re.search(r"(\d{1,2})\s+([a-zéûôèà]+)\s+(\d{4})", raw, re.I)
    if m:
        d, month_fr, y = m.groups()
        mo = _MONTHS_FR.get(month_fr.lower())
        if mo:
            return f"{y}-{mo}-{int(d):02d}"
    # ISO YYYY-MM-DD
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return "-".join(m.groups())
    return ""


def _has_next_page(soup, current_page):
    """Return True if any pagination link points to a page number > current_page."""
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r"\?page=(\d+)", href)
        if m and int(m.group(1)) > current_page:
            return True
    return False
