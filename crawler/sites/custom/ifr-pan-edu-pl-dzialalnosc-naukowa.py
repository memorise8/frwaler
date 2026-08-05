# -*- coding: utf-8 -*-
"""Crawler for IFR PAN publications repository (ifr-pan.edu.pl).

The site was redesigned: the old ``/publication/filtered`` AJAX endpoint
that returned all records as bare ``<tr data-id>`` rows now 404s (serves a
"Strona nie została odnaleziona" page with HTTP 200). Publications are now
server-rendered directly on ``/dzialalnosc-naukowa/publikacje`` as
``<article class="box">`` cards (20/page), with normal href-based
pagination at ``/dzialalnosc-naukowa/publikacje/strona,{N}/`` (118 pages
as of this fix). There is no stable per-item id in the new markup, so a
DOI (parsed from the card's outbound "Więcej" link) is used as the
external_id when available, falling back to a hash of title+year.

There are still no individual detail pages; abstracts are fetched from the
CrossRef API using the DOI extracted from each publication's external link
or journal info, same as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "ifr-pan-edu-pl-dzialalnosc-naukowa"
_BASE_URL = "https://ifr-pan.edu.pl"
_LIST_PAGE_BASE = "https://ifr-pan.edu.pl/dzialalnosc-naukowa/publikacje"
_CROSSREF_BASE = "https://api.crossref.org/works"


class IFRPanPublicationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: ifr-pan-edu-pl-dzialalnosc-naukowa"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, headers: dict | None = None, timeout: int = 30) -> str | None:
        """GET via curl with TLS workaround and exponential-backoff retry."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
        ]
        if headers:
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = [1, 3, 9][attempt]
                print(f"[{_SITE_ID}] empty response attempt {attempt + 1}/3, retry in {wait}s")
                time.sleep(wait)
            except Exception as exc:
                wait = [1, 3, 9][attempt]
                print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/3: {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # DOI extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_doi(href: str, journal_info: str = "") -> str | None:
        """Return a DOI string from a URL or journal info, or None."""
        for text in (href, journal_info):
            if not text:
                continue
            # doi.org/10.xxx/...
            m = re.search(r"doi\.org/(10\.\d{4,9}/[^\s\"'&?#]+)", text, re.IGNORECASE)
            if m:
                return m.group(1).rstrip("/.,;)")
            # Frontiers article URL embeds DOI in path
            m = re.search(
                r"frontiersin\.org/[^/]+/articles/(10\.\d{4,9}/[^\s\"'/?#]+)",
                text, re.IGNORECASE,
            )
            if m:
                return m.group(1).rstrip("/.,;)")
            # PLoS ONE filename pattern: journal.pone.NNNNNNN
            m = re.search(r"journal\.pone\.(\d+)", text, re.IGNORECASE)
            if m:
                return f"10.1371/journal.pone.{m.group(1)}"
        return None

    # ------------------------------------------------------------------
    # Abstract sources
    # ------------------------------------------------------------------

    def _crossref_abstract(self, doi: str) -> tuple[str | None, str | None]:
        """Query CrossRef for (abstract, authors) using a DOI.

        Returns (None, None) on any failure.
        """
        url = f"{_CROSSREF_BASE}/{doi}"
        raw = self._curl_get(
            url,
            headers={"User-Agent": "mailto:crawler@ifr-research.org"},
            timeout=20,
        )
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        msg = data.get("message", {})

        abstract = msg.get("abstract") or ""
        abstract = re.sub(r"<[^>]+>", " ", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()

        author_list = [
            " ".join(filter(None, [a.get("given", ""), a.get("family", "")])).strip()
            for a in msg.get("author", [])
            if a.get("family")
        ]
        authors = "; ".join(author_list) if author_list else None

        return (abstract or None), authors

    def _scrape_abstract(self, url: str) -> str | None:
        """Attempt to scrape an abstract from the journal article page."""
        if not url or url.startswith("file://"):
            return None
        raw = self._curl_get(url, timeout=20)
        if not raw:
            return None
        try:
            try:
                from bs4 import BeautifulSoup
                try:
                    soup = BeautifulSoup(raw, "html5lib")
                except Exception:
                    try:
                        soup = BeautifulSoup(raw, "lxml")
                    except Exception:
                        soup = BeautifulSoup(raw, "html.parser")
            except Exception:
                return None
        except Exception:
            return None

        selectors = [
            'div.abstract', 'section.abstract', 'div#abstract', 'section#abstract',
            '[class*="abstract-content"]', '[class*="ArticleAbstract"]',
            'meta[name="description"]', 'meta[property="og:description"]',
        ]
        for sel in selectors:
            try:
                el = soup.select_one(sel)
                if not el:
                    continue
                if el.name == "meta":
                    text = el.get("content", "").strip()
                else:
                    text = el.get_text(separator=" ", strip=True)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) >= 50:
                    return text[:4000]
            except Exception:
                continue
        return None

    @staticmethod
    def _build_abstract(title: str, authors: str, year: str, journal_info: str, doi: str | None) -> str:
        """Construct a metadata-based abstract when no real abstract is available."""
        parts = []
        if title:
            parts.append(title + ".")
        if journal_info:
            parts.append(f"Published in: {journal_info}.")
        if authors:
            parts.append(f"Authors: {authors}.")
        if year:
            parts.append(f"Year: {year}.")
        if doi:
            parts.append(f"DOI: {doi}.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Listing page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[dict]:
        """Parse one ``/dzialalnosc-naukowa/publikacje[/strona,N/]`` page.

        Returns a list of raw row dicts (title/authors/year/journal_info/ext_href).
        """
        try:
            from bs4 import BeautifulSoup
            try:
                soup = BeautifulSoup(html, "html.parser")
            except Exception:
                try:
                    soup = BeautifulSoup(html, "lxml")
                except Exception:
                    soup = BeautifulSoup(html, "html5lib")
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse failed: {exc}")
            return []

        rows: list[dict] = []
        for article in soup.find_all("article", class_="box"):
            personlist = article.find(class_="personlist")
            if not personlist:
                continue
            h2 = personlist.find("h2", class_="title")
            if not h2:
                continue
            title = h2.get_text(strip=True)
            if not title:
                continue

            span = h2.find_next_sibling("span")
            journal_info = span.get_text(strip=True) if span else ""

            # Author/year text: every personlist child before the <h2>
            pre_parts = []
            for child in personlist.contents:
                if child is h2:
                    break
                pre_parts.append(child.get_text() if hasattr(child, "get_text") else str(child))
            pre_text = re.sub(r"\s+", " ", "".join(pre_parts)).strip()

            year = None
            authors_str = pre_text
            m = re.search(r"\((\d{4})\)", pre_text)
            if m:
                year = m.group(1)
                authors_str = pre_text[: m.start()].strip()
            authors_str = authors_str.rstrip(",").strip()

            ext_link = article.find("a", class_="goToPage")
            ext_href = (ext_link.get("href") or "").strip() if ext_link else None
            if ext_href and ext_href.startswith("file://"):
                ext_href = None

            rows.append({
                "title": title,
                "authors_str": authors_str,
                "year": year,
                "journal_info": journal_info,
                "ext_href": ext_href,
            })
        return rows

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_ts = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget
        max_pages = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))         # page-count safety cap

        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_ids: set[str] = set()
        page_num = 1

        # ------------------------------------------------------------------
        # Walk server-rendered listing pages: page 1 = base URL, page N>=2 =
        # .../strona,N/. Stops on the first page with 0 cards (naturally the
        # page past the last real one) rather than trusting a parsed total.
        # ------------------------------------------------------------------
        while True:
            if limit is not None and saved >= limit:
                break
            if page_num > max_pages:
                print(f"[{_SITE_ID}] Safety cap of {max_pages} pages reached, stopping")
                break
            if time.time() - start_ts > max_wall:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached after {saved} saved. Stopping.")
                break

            page_url = _LIST_PAGE_BASE if page_num == 1 else f"{_LIST_PAGE_BASE}/strona,{page_num}/"
            raw = self._curl_get(page_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch page {page_num}, stopping.")
                break

            rows = self._parse_listing_page(raw)
            if page_num == 1:
                print(f"[{_SITE_ID}] Fetching publication listing from {_LIST_PAGE_BASE} ...")
            if not rows:
                print(f"[{_SITE_ID}] page {page_num}: 0 rows — done")
                break

            if page_num % 10 == 0 or page_num == 1:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str} ({len(rows)} rows)")

            for row in rows:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_ts > max_wall:
                    print(f"[{_SITE_ID}] 25-minute wall-clock budget reached mid-page. Stopping.")
                    break

                title = row["title"]
                authors_str = row["authors_str"]
                year = row["year"]
                journal_info = row["journal_info"]
                ext_href = row["ext_href"]
                post_id = None

                try:
                    published_date = year if year and re.match(r"^\d{4}$", year) else None

                    # Journal name: leading segment of "Journal Name, vol, pages"
                    journal = ""
                    m = re.match(r"^([^,]+),\s*(.*)$", journal_info)
                    if m:
                        journal = m.group(1).strip()

                    canonical_url = ext_href or (
                        f"{_BASE_URL}/dzialalnosc-naukowa/publikacje"
                        + (f"?rocznik={year}" if year else "")
                    )

                    # --- DOI ---
                    doi = self._extract_doi(ext_href or "", journal_info)

                    # Stable id: DOI when available, else a hash of title+year
                    post_id = doi or hashlib.md5(f"{title}|{year}".encode("utf-8")).hexdigest()[:16]
                    if post_id in seen_ids:
                        continue
                    seen_ids.add(post_id)

                    # --- Abstract: CrossRef first ---
                    abstract: str | None = None
                    crossref_authors: str | None = None

                    if doi:
                        time.sleep(0.3)  # gentle CrossRef rate-limit
                        abstract, crossref_authors = self._crossref_abstract(doi)

                    # Fallback: scrape external page
                    if (not abstract or len(abstract) < 50) and ext_href:
                        time.sleep(self._delay)
                        abstract = self._scrape_abstract(ext_href)

                    # Final fallback: construct from metadata
                    if not abstract or len(abstract) < 50:
                        fb_authors = authors_str or crossref_authors or ""
                        abstract = self._build_abstract(title, fb_authors, year, journal_info, doi)

                    if not abstract or len(abstract) < 50:
                        print(f"[{_SITE_ID}] row {post_id}: abstract <50 chars, skipping")
                        continue

                    # Use CrossRef authors when list page had none
                    if not authors_str and crossref_authors:
                        authors_str = crossref_authors

                    # pdf_url: only genuine PDF download links
                    pdf_url: str | None = None
                    if ext_href and re.search(r"\.pdf($|\?|#)", ext_href, re.IGNORECASE):
                        pdf_url = ext_href

                    original_filename: str | None = None
                    if pdf_url:
                        original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0] or None

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": post_id,
                        "post_number": post_id,
                        "title": title,
                        "abstract": abstract,
                        "authors": authors_str,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": canonical_url,
                        "pdf_url": pdf_url,
                        "doi": doi or "",
                        "journal": journal,
                        "keywords": "",
                        "publisher": "Instytut Fizjologii Roślin im. F. Górskiego PAN",
                        "department": "",
                        "category": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": published_date,
                                "journal_raw": journal_info,
                                "node_id": post_id,
                                "crossref_doi": doi,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_str}: [{post_id}] {title[:55]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item (id={post_id!r}) failed: {exc}")
                    continue

            page_num += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
