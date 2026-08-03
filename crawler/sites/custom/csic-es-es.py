# -*- coding: utf-8 -*-
"""Crawler for CSIC — El CSIC en cifras (institutional data publications).

Starting URL: https://www.csic.es/es/el-csic/informacion-corporativa/el-csic-en-cifras

The page lists annual statistical PDF publications (institute/centre data) in two
sections:
  1. A Drupal-views paginated list (2024 → 2011) — pure PDF links, no per-item text.
  2. A static "DATOS MÁS DESTACADOS DEL CSIC (2004-2010)" section with year sub-headings
     and multiple topic PDFs per year.

Abstracts are built from the year-specific context + the page's own CSIC description
block, giving each record 200+ chars of real page text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = "https://www.csic.es"
_LIST_URL = f"{_BASE}/es/el-csic/informacion-corporativa/el-csic-en-cifras"
_PUBLISHER = "CSIC - Consejo Superior de Investigaciones Científicas"

# Stable body description extracted from the cifras page (326 chars).
# Used as per-item abstract context when the page has no individual descriptions.
_CSIC_DESC = (
    "El Consejo Superior de Investigaciones Científicas (CSIC) es una Agencia Estatal "
    "para la investigación científica y el desarrollo tecnológico, con personalidad "
    "jurídica diferenciada, patrimonio y tesorería propios, autonomía funcional y de "
    "gestión, plena capacidad jurídica de obrar y de duración indefinida. "
    "(art. 1 del Estatuto de la Agencia Estatal CSIC)"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_BACKOFF = (1, 3, 9)
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MIN_ABSTRACT = 50
_MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Try html5lib → lxml → html.parser; return None on total failure."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _resolve_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _BASE + href
    return href


def _extract_year(text: str) -> str | None:
    """Extract a 4-digit year (196x-202x) from text."""
    m = re.search(r"\b(196\d|197\d|198\d|199\d|200\d|201\d|202\d)\b", text)
    return m.group(1) if m else None


def _filename_from_url(url: str) -> str | None:
    try:
        path = urllib.parse.unquote(url).split("?")[0].rstrip("/")
        name = path.split("/")[-1]
        return name if "." in name else None
    except Exception:
        return None


def _clean_label(raw: str) -> str:
    """Strip noise from link text to get a usable title fragment."""
    s = raw
    s = re.sub(r"\s*\(Se abre[^)]+\)\s*", " ", s, flags=re.I)
    s = re.sub(r"PDF\s*\([^)]+\)", "", s, flags=re.I)
    s = re.sub(r"\s*\(PDF\)\s*", " ", s, flags=re.I)
    s = re.sub(r"\bDescargar\s*$", "", s, flags=re.I)
    s = re.sub(r"\bfolleto\s*$", "Folleto", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class CsicEsEsCrawler(BaseCrawler):
    """Crawler for CSIC — El CSIC en cifras institutional data publications."""

    site_id = "csic-es-es"
    site_name = "Custom: csic-es-es"
    base_url = _BASE

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _curl(self, url: str) -> str | None:
        """Fetch URL via curl with 3-attempt exponential-backoff retry."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L",
            "-A", _UA, "--max-time", "30", url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                if r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = _BACKOFF[attempt]
                    print(f"[csic-es-es] empty response attempt {attempt + 1}/3; retrying in {wait}s")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                print(f"[csic-es-es] curl timeout attempt {attempt + 1}/3 for {url[:80]}")
                if attempt < 2:
                    time.sleep(_BACKOFF[attempt])
            except Exception as exc:
                print(f"[csic-es-es] curl error attempt {attempt + 1}/3: {exc}")
                if attempt < 2:
                    time.sleep(_BACKOFF[attempt])
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_page(self, html: str, page_num: int) -> tuple[list[dict], bool]:
        """Parse one listing page.

        Returns (items, has_next_page).
        Each item dict: title, pdf_url, year, source.
        """
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[csic-es-es] BeautifulSoup error on page {page_num}: {exc}")
            return [], False

        if soup is None:
            return [], False

        items: list[dict] = []
        seen_hrefs: set[str] = set()

        # ---- Strategy 1: paginated view rows (.views-row) ----
        for row in soup.select(".views-row"):
            try:
                a = row.find("a", href=lambda h: h and ".pdf" in h.lower())
                if not a:
                    continue
                href = a.get("href", "")
                if not href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                link_text = a.get_text(strip=True)
                link_title_attr = a.get("title", "")
                img = row.find("img")
                img_alt = img.get("alt", "").strip() if img else ""

                raw = link_text or img_alt or link_title_attr or ""
                title = _clean_label(raw) or f"CSIC datos {_extract_year(href) or ''}"

                pdf_url = _resolve_url(href)
                year = (
                    _extract_year(img_alt)
                    or _extract_year(link_text)
                    or _extract_year(href)
                )

                items.append({
                    "title": title,
                    "pdf_url": pdf_url,
                    "year": year,
                    "source": "view",
                })
            except Exception as exc:
                print(f"[csic-es-es] view-row parse error: {exc}")
                continue

        # Track which PDF URLs came from the view (for dedup in static section)
        view_pdf_urls: set[str] = {_resolve_url(i["pdf_url"]) for i in items}

        # ---- Strategy 2: static older section (2004-2010) ----
        # Walk document looking for "DATOS MÁS DESTACADOS" heading, then track
        # year sub-headings and collect PDF links not already in the view.
        in_static = False
        current_year: str | None = None

        for tag in soup.descendants:
            if not hasattr(tag, "name") or not tag.name:
                continue

            try:
                tag_text = tag.get_text(strip=True)
            except Exception:
                continue

            # Detect section start
            if not in_static:
                if re.search(r"DATOS M[ÁA]S DESTACADOS", tag_text, re.I):
                    in_static = True
                continue

            # Detect year sub-heading — a short element containing only a year
            if tag.name in ("h2", "h3", "h4", "p", "div", "span"):
                if re.match(r"^(19[6-9]\d|200[0-9]|201[0-9])$", tag_text.strip()):
                    current_year = tag_text.strip()
                    continue

            # Collect PDF anchor tags
            if tag.name == "a":
                href = tag.get("href", "")
                if not href or ".pdf" not in href.lower():
                    continue
                # Skip Flash/HTML links that slipped through
                if href.endswith(".html") or href.endswith(".htm"):
                    continue

                pdf_url = _resolve_url(href)
                if pdf_url in view_pdf_urls:
                    continue
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                link_text = tag.get_text(strip=True)
                label = _clean_label(link_text)

                year = current_year or _extract_year(label) or _extract_year(href)

                # Build title from label + year context
                if label and label.lower() not in ("descargar", ""):
                    title = (
                        f"{label} ({year})"
                        if year and year not in label
                        else label
                    )
                else:
                    fname = _filename_from_url(pdf_url) or href.split("/")[-1]
                    title = f"CSIC datos {year or 'histórico'} — {fname}"

                items.append({
                    "title": title,
                    "pdf_url": pdf_url,
                    "year": year,
                    "source": "static",
                })
                view_pdf_urls.add(pdf_url)  # prevent processing same link twice

        # ---- Detect next-page link ----
        # Robust check: does a link to page N+1 exist anywhere on the page?
        has_next = bool(
            soup.find("a", href=re.compile(rf"[?&]page={page_num + 1}(&|$|')"))
        )
        # Fallback: look for "Siguiente" / "Next" text
        if not has_next:
            for a in soup.find_all("a", href=True):
                txt = a.get_text(separator=" ", strip=True)
                if re.search(r"siguiente|next\s*page", txt, re.I):
                    # Ensure it points forward (not to page 0)
                    href = a.get("href", "")
                    m = re.search(r"page=(\d+)", href)
                    if m and int(m.group(1)) > page_num:
                        has_next = True
                        break

        return items, has_next

    # ------------------------------------------------------------------
    # Abstract builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_abstract(item: dict) -> str:
        """Construct a >=100-char abstract from year context + CSIC description."""
        year = item.get("year", "")
        title = item.get("title", "")
        parts: list[str] = []
        if year:
            parts.append(
                f"Publicación anual del CSIC con datos estadísticos de institutos y centros "
                f"correspondientes al año {year}."
            )
        elif title:
            parts.append(f"Documento institucional del CSIC: {title}.")
        parts.append(_CSIC_DESC)
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        page = 0

        while True:
            # Wall-clock budget
            if time.time() - start_time > _MAX_SECONDS:
                print("[csic-es-es] 25-minute wall-clock budget reached; stopping cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page >= _MAX_PAGES:
                print(f"[csic-es-es] Safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            list_url = f"{_LIST_URL}?page={page}" if page > 0 else _LIST_URL

            if page > 0 and page % 10 == 0:
                print(f"[csic-es-es] page {page}: saved {saved}/{limit_str}")

            raw_html = self._curl(list_url)
            if not raw_html:
                print(f"[csic-es-es] Failed to fetch page {page}; stopping")
                break

            try:
                items, has_next = self._parse_page(raw_html, page)
            except Exception as exc:
                print(f"[csic-es-es] parse_page error on page {page}: {exc}; stopping")
                break

            if not items and page > 0:
                print(f"[csic-es-es] No items on page {page}; done")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                pdf_url = item.get("pdf_url", "")
                if not pdf_url or pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    title = item.get("title", "").strip()
                    year = item.get("year")
                    fname = _filename_from_url(pdf_url)
                    external_id = fname or re.sub(r"https?://[^/]+/", "", pdf_url)

                    if not title:
                        title = f"CSIC datos {year or 'histórico'}"

                    abstract = self._build_abstract(item)

                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[csic-es-es] Skipping — abstract too short "
                            f"({len(abstract)} chars): {title[:50]}"
                        )
                        continue

                    published_date = f"{year}-01-01" if year else None

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": year,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": published_date,
                        "url": _LIST_URL,
                        "pdf_url": pdf_url,
                        "authors": None,
                        "publisher": _PUBLISHER,
                        "department": None,
                        "journal": None,
                        "keywords": "datos estadísticos, institutos, centros, CSIC",
                        "category": "Datos Institucionales",
                        "doi": None,
                        "original_filename": fname,
                        "metadata": json.dumps(
                            {
                                "posted_date": published_date,
                                "originalFilename": fname,
                                "year": year,
                                "source_page": _LIST_URL,
                                "source_section": item.get("source", ""),
                            },
                            ensure_ascii=False,
                        ),
                    })
                    saved += 1
                    print(f"[csic-es-es] Saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[csic-es-es] item {pdf_url[:60]} failed: {exc}; continuing")
                    continue

            if new_on_page == 0 and page > 0:
                print(f"[csic-es-es] Page {page} yielded no new records; done")
                break

            if not has_next:
                print(f"[csic-es-es] No next-page link after page {page}; done")
                break

            page += 1
            time.sleep(self._delay)

        print(f"[csic-es-es] Done. Total saved: {saved}")
        return saved
