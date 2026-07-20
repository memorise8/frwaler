# -*- coding: utf-8 -*-
"""Crawler for An Coimisiún Pleanála — Annual & Quarterly Statistics.

Starting URL: https://www.pleanala.ie/en-IE/Statistics/Annual-Statistics

Structure:
  - Annual Statistics index: direct PDF links per year + 2019 HTML sub-index
  - 2019 sub-index: 16 HTML appendix table pages (rich statistical content)
  - Quarterly Statistics: PDF links per quarter/year
"""

from __future__ import annotations

import email.utils
import html as html_lib
import io
import json
import os
import re
import subprocess
import time
import urllib.parse
import uuid
from typing import Any

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


# ──────────────────────────────────────────────────────── helpers ──────

def _clean(v: Any) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(str(v)).replace("\xa0", " ")).strip()


def _make_soup(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    if BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _abs_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    return urllib.parse.urljoin(base, html_lib.unescape(href.strip()))


def _canon(url: str | None) -> str:
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if k.lower() not in {"r", "lang"}]
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path,
         urllib.parse.urlencode(q), "")
    )


def _fname(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    name = urllib.parse.unquote(os.path.basename(path))
    return name[:240] if name else None


def _fname_from_disp(header: str | None) -> str | None:
    h = header or ""
    for pat in (
        r"filename\*\s*=\s*[^']*''([^;]+)",
        r'filename\s*=\s*"([^"]+)"',
        r"filename\s*=\s*([^;]+)",
    ):
        m = re.search(pat, h, re.I)
        if m:
            return urllib.parse.unquote(m.group(1).strip().strip('"'))[:240]
    return None


def _http_date(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return email.utils.parsedate_to_datetime(v).date().isoformat()
    except Exception:
        return None


def _year(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return m.group(1) if m else None


def _date(text: str | None) -> str | None:
    y = _year(text)
    return f"{y}-01-01" if y else None


def _media_guid(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(
        r"/get(?:media|attachment)/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)",
        urllib.parse.urlsplit(url).path,
    )
    return m.group(1).lower() if m else None


def _is_media(url: str | None) -> bool:
    if not url:
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    return "/getmedia/" in path or "/getattachment/" in path


def _file_ext(url: str | None) -> str:
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    ext = os.path.splitext(p.path)[1].lower()
    if not ext:
        for v in urllib.parse.parse_qs(p.query).get("ext", []):
            if v.startswith("."):
                return v.lower()
    return ext


def _appendix_num(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"[Aa]ppendix[-_](\d+)", url)
    return str(int(m.group(1))) if m else None


# ─────────────────────────────────────────────────────── crawler ────────

class PleanalaIeEnIeCrawler(BaseCrawler):
    site_id = "pleanala-ie-en-ie"
    site_name = "Custom: pleanala-ie-en-ie"
    base_url = "https://www.pleanala.ie"

    _START_URL = "https://www.pleanala.ie/en-IE/Statistics/Annual-Statistics"
    _QUARTERLY_URL = "https://www.pleanala.ie/en-IE/Statistics/Quarterly-Statistics"
    _PUBLISHER = "An Coimisiún Pleanála"
    _MAX_PAGES = 200
    _WALL_SECONDS = 25 * 60
    _MIN_ABSTRACT = 50

    # ───────────────── network ─────────────────

    def _get_bytes(self, url: str, *, method: str = "GET",
                   accept: str = "text/html,*/*;q=0.8",
                   timeout: int = 30) -> bytes | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept}",
        ]
        if method.upper() == "HEAD":
            cmd.append("-I")
        cmd.append(url)
        for attempt, wait in enumerate((1, 3, 9), start=1):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
                if r.returncode == 0 and r.stdout:
                    return r.stdout
                stderr = r.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] attempt {attempt}/3 failed "
                    f"({url[:80]}): {stderr[:120]}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] attempt {attempt}/3 error "
                    f"({url[:80]}): {exc}"
                )
            if attempt < 3:
                time.sleep(wait)
        return None

    def _get_text(self, url: str, *, timeout: int = 30) -> str | None:
        b = self._get_bytes(url, timeout=timeout)
        return b.decode("utf-8", errors="replace") if b else None

    def _head(self, url: str, *, timeout: int = 20) -> dict[str, str]:
        b = self._get_bytes(url, method="HEAD", accept="*/*", timeout=timeout)
        if not b:
            return {}
        text = b.decode("utf-8", errors="replace")
        blocks = [bl for bl in re.split(r"\r?\n\r?\n", text) if bl.strip()]
        hdrs: dict[str, str] = {}
        for line in (blocks[-1].splitlines() if blocks else []):
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        return hdrs

    # ───────────────── PDF text ─────────────────

    def _pdf_text(self, pdf_bytes: bytes) -> str:
        try:
            from pypdf import PdfReader
        except Exception:
            return ""
        try:
            rdr = PdfReader(io.BytesIO(pdf_bytes), strict=False)
            if getattr(rdr, "is_encrypted", False):
                try:
                    rdr.decrypt("")
                except Exception:
                    return ""
            parts: list[str] = []
            for pg in list(rdr.pages)[:3]:
                try:
                    parts.append(pg.extract_text() or "")
                except Exception:
                    continue
            return _clean(" ".join(parts))
        except Exception as exc:
            print(f"[{self.site_id}] PDF extract failed: {exc}")
            return ""

    # ───────────────── abstract helpers ─────────────────

    def _fallback_abstract(self, title: str, year: str | None,
                           category: str | None, filename: str | None,
                           last_modified: str | None,
                           content_type: str | None) -> str:
        yr = year or "the year"
        cat = category or "statistics"
        fn = filename or "the source document"
        text = (
            f"{title} is an official {cat} document published by {self._PUBLISHER}. "
            f"This record covers {yr}; the associated file is {fn}. "
            f"An Coimisiún Pleanála is Ireland's national independent planning "
            f"appeals board, responsible for determining planning appeals and "
            f"strategic infrastructure development applications."
        )
        if last_modified:
            text += f" Last-Modified: {last_modified}."
        if content_type:
            text += f" Content-Type: {content_type}."
        return text

    # ───────────────── item processors ─────────────────

    def _process_media(self, item: dict[str, Any]) -> dict[str, Any]:
        """Process a /getmedia/ or /getattachment/ URL (PDF or other file)."""
        url = item["url"]
        hdrs = self._head(url, timeout=20)
        content_type = hdrs.get("content-type", "")
        disposition = hdrs.get("content-disposition", "")
        last_modified_raw = hdrs.get("last-modified")
        last_modified_iso = _http_date(last_modified_raw)

        orig_filename = _fname_from_disp(disposition) or _fname(url)
        extension = _file_ext(orig_filename or url)
        is_pdf = extension == ".pdf" or "application/pdf" in content_type.lower()
        pdf_url = url if is_pdf else None

        pdf_text = ""
        if is_pdf:
            pdf_bytes = self._get_bytes(
                url, accept="application/pdf,*/*;q=0.8", timeout=60
            )
            if pdf_bytes:
                pdf_text = self._pdf_text(pdf_bytes)

        yr = item.get("year") or item.get("parent_year") or _year(item.get("title"))
        listed = last_modified_iso or item.get("listed_date")
        published = (last_modified_iso
                     or _date(item.get("title"))
                     or (f"{yr}-01-01" if yr else None))

        if pdf_text and len(pdf_text) >= self._MIN_ABSTRACT:
            abstract = pdf_text[:5000]
        else:
            abstract = self._fallback_abstract(
                title=item.get("title") or "Untitled",
                year=yr,
                category=item.get("category"),
                filename=orig_filename,
                last_modified=last_modified_raw,
                content_type=content_type or None,
            )

        guid = item.get("media_guid") or _media_guid(url)
        ext_id = guid or _canon(url) or str(uuid.uuid5(uuid.NAMESPACE_URL, url))
        post_num = yr or guid or ext_id

        return {
            "id": f"{self.site_id}:{ext_id}",
            "site_id": self.site_id,
            "external_id": ext_id,
            "post_number": post_num,
            "title": item.get("title") or "Untitled",
            "abstract": abstract,
            "published_date": published,
            "listed_date": listed,
            "posted_date": listed,
            "authors": None,
            "publisher": self._PUBLISHER,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": item.get("category"),
            "category": item.get("category"),
            "doi": None,
            "department": None,
            "journal": None,
            "original_filename": orig_filename if is_pdf else None,
            "metadata": json.dumps({
                "posted_date": listed,
                "originalFilename": orig_filename,
                "media_guid": guid,
                "content_type": content_type or None,
                "last_modified": last_modified_raw,
                "year": yr,
                "category": item.get("category"),
                "source": item.get("source"),
            }, ensure_ascii=False),
        }

    def _process_html(self, item: dict[str, Any]) -> dict[str, Any]:
        """Fetch an HTML statistics table page and use article text as abstract."""
        page_url = item["url"]
        raw = self._get_text(page_url, timeout=30)
        if not raw:
            raise RuntimeError(f"HTML fetch failed: {page_url}")
        soup = _make_soup(raw)
        if not soup:
            raise RuntimeError(f"HTML parse failed: {page_url}")

        article = soup.find("article")
        abstract = _clean(article.get_text(" ", strip=True)) if article else ""
        if not abstract:
            main = soup.find("main") or soup.find(id="maincontent")
            abstract = _clean(main.get_text(" ", strip=True)) if main else ""

        yr = item.get("year") or item.get("parent_year") or _year(item.get("title"))
        slug = page_url.rstrip("/").split("/")[-1].lower()
        ext_id = slug[:100] or str(uuid.uuid5(uuid.NAMESPACE_URL, page_url))
        appendix = _appendix_num(page_url)
        post_num = appendix or yr or ext_id
        published = (_date(item.get("title"))
                     or (f"{yr}-01-01" if yr else None))

        return {
            "id": f"{self.site_id}:{ext_id}",
            "site_id": self.site_id,
            "external_id": ext_id,
            "post_number": post_num,
            "title": item.get("title") or "Untitled",
            "abstract": abstract,
            "published_date": published,
            "listed_date": published,
            "posted_date": published,
            "authors": None,
            "publisher": self._PUBLISHER,
            "url": page_url,
            "pdf_url": None,
            "keywords": item.get("category"),
            "category": item.get("category"),
            "doi": None,
            "department": None,
            "journal": None,
            "original_filename": None,
            "metadata": json.dumps({
                "posted_date": published,
                "year": yr,
                "appendix_number": appendix,
                "parent_title": item.get("parent_title"),
                "category": item.get("category"),
                "source": item.get("source"),
            }, ensure_ascii=False),
        }

    # ───────────────── link extraction ─────────────────

    def _article_links(self, raw: str, source_url: str,
                       category: str | None = None,
                       parent_title: str | None = None,
                       parent_year: str | None = None) -> list[dict[str, Any]]:
        """Extract links from the <article> element of a pleanala.ie page."""
        soup = _make_soup(raw)
        if not soup:
            return []
        article = soup.find("article")
        if not article:
            return []
        items: list[dict[str, Any]] = []
        for a in article.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or "javascript" in href.lower():
                continue
            full_url = _abs_url(self.base_url, href)
            if not full_url:
                continue
            netloc = urllib.parse.urlsplit(full_url).netloc
            if netloc and "pleanala.ie" not in netloc:
                continue
            title = _clean(a.get_text())
            if not title:
                continue
            items.append({
                "title": title,
                "url": full_url,
                "canonical_url": _canon(full_url),
                "listed_date": _date(title),
                "year": _year(title),
                "parent_year": parent_year,
                "category": category,
                "media_guid": _media_guid(full_url),
                "source": source_url,
                "parent_title": parent_title,
                "is_media": _is_media(full_url),
            })
        return items

    # ───────────────── crawl ─────────────────

    def crawl(self, limit=None):
        """Crawl pleanala.ie annual and quarterly statistics pages.

        Processes HTML appendix table pages before media/PDF items so that
        fast-to-fetch items are handled first (relevant for small limits).
        """
        saved = 0
        page = 0
        seen: set[str] = set()
        t0 = time.monotonic()
        lim = limit if limit is not None else "inf"

        def _budget_ok() -> bool:
            return time.monotonic() - t0 < self._WALL_SECONDS - 30

        def _pages_ok() -> bool:
            return page < self._MAX_PAGES

        # ── Phase 1: Annual Statistics index page ──────────────────────
        page += 1
        if page % 10 == 0:
            print(f"[{self.site_id}] page {page}: saved {saved}/{lim}")
        if not _pages_ok():
            print(f"[{self.site_id}] safety cap {self._MAX_PAGES} pages reached")
            return saved

        raw_idx = self._get_text(self._START_URL, timeout=45)
        if not raw_idx:
            print(f"[{self.site_id}] Failed to fetch annual stats index; aborting")
            return 0

        index_links = self._article_links(
            raw_idx, self._START_URL, category="Annual Statistics"
        )

        html_items: list[dict[str, Any]] = []
        media_items: list[dict[str, Any]] = []

        for link in index_links:
            if not _budget_ok():
                print(f"[{self.site_id}] wall-clock budget approaching during collection")
                break
            if link["is_media"]:
                media_items.append({"type": "media", **link})
            else:
                # HTML sub-index (e.g. 2019 appendix list) — expand one level
                page += 1
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim}")
                if not _pages_ok():
                    print(f"[{self.site_id}] safety cap {self._MAX_PAGES} pages reached")
                    break
                sub_raw = self._get_text(link["url"], timeout=30)
                if not sub_raw:
                    print(f"[{self.site_id}] Sub-page fetch failed: {link['url']}")
                    continue
                sub_links = self._article_links(
                    sub_raw, link["url"],
                    category=link.get("category", "Annual Statistics"),
                    parent_title=link.get("title"),
                    parent_year=link.get("year"),
                )
                for sl in sub_links:
                    if sl["is_media"]:
                        media_items.append({"type": "media", **sl})
                    else:
                        html_items.append({"type": "html", **sl})

        # ── Phase 2: Quarterly Statistics page ─────────────────────────
        page += 1
        if page % 10 == 0:
            print(f"[{self.site_id}] page {page}: saved {saved}/{lim}")
        if _pages_ok() and _budget_ok():
            q_raw = self._get_text(self._QUARTERLY_URL, timeout=45)
            if q_raw:
                for ql in self._article_links(
                    q_raw, self._QUARTERLY_URL, category="Quarterly Statistics"
                ):
                    if ql["is_media"]:
                        media_items.append({"type": "media", **ql})

        # HTML items first (no heavy download needed) → reliable for small limits
        all_items = html_items + media_items
        print(f"[{self.site_id}] Collected {len(html_items)} HTML + "
              f"{len(media_items)} media items")

        if not all_items:
            print(f"[{self.site_id}] No items collected; done")
            return 0

        # ── Phase 3: process items ──────────────────────────────────────
        for item in all_items:
            if limit is not None and saved >= limit:
                break
            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim}")
            if not _pages_ok():
                print(f"[{self.site_id}] safety cap {self._MAX_PAGES} pages reached")
                break
            if not _budget_ok():
                print(f"[{self.site_id}] wall-clock budget approaching; exiting cleanly")
                break

            key = item.get("canonical_url") or _canon(item.get("url"))
            if not key or key in seen:
                continue
            seen.add(key)

            try:
                time.sleep(self._delay)
                if item["type"] == "html":
                    paper = self._process_html(item)
                else:
                    paper = self._process_media(item)

                abstract = _clean(paper.get("abstract") or "")
                if len(abstract) < self._MIN_ABSTRACT:
                    print(
                        f"[{self.site_id}] skip '{paper.get('title', '?')[:40]}': "
                        f"abstract {len(abstract)} chars"
                    )
                    continue
                paper["abstract"] = abstract
                self._save_paper(paper)
                saved += 1
                print(
                    f"[{self.site_id}] Saved {saved}/{lim}: "
                    f"{paper.get('title', '?')[:60]}"
                )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{self.site_id}] item {item.get('url', '?')[:80]} "
                    f"failed: {exc}"
                )
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
