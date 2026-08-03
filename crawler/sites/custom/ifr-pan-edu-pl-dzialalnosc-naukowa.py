# -*- coding: utf-8 -*-
"""Crawler for IFR PAN publications repository (ifr-pan.edu.pl).

Publications are served via a single AJAX endpoint that returns all records as
HTML table rows.  There are no individual detail pages; abstracts are fetched
from the CrossRef API using the DOI extracted from each publication's external
link or journal info.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "ifr-pan-edu-pl-dzialalnosc-naukowa"
_BASE_URL = "https://ifr-pan.edu.pl"
# Returns all publications as HTML <tr> rows when called without filter params
_LIST_URL = "https://ifr-pan.edu.pl/publication/filtered"
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
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_ts = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget

        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_ids: set[str] = set()

        # ------------------------------------------------------------------
        # 1. Fetch full publication list (no filter = all years, ~2337 rows)
        # ------------------------------------------------------------------
        print(f"[{_SITE_ID}] Fetching full publication list from {_LIST_URL} ...")
        raw = self._curl_get(
            _LIST_URL,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{_BASE_URL}/dzialalnosc-naukowa/publikacje",
            },
        )
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch list. Aborting.")
            return 0

        # ------------------------------------------------------------------
        # 2. Parse HTML rows
        # ------------------------------------------------------------------
        # The endpoint returns bare <tr> fragments (no <table>/<html> wrapper).
        # Wrap before parsing so html.parser reconstructs the DOM correctly.
        wrapped = "<table><tbody>" + raw + "</tbody></table>"
        try:
            from bs4 import BeautifulSoup
            try:
                soup = BeautifulSoup(wrapped, "html.parser")
            except Exception:
                try:
                    soup = BeautifulSoup(wrapped, "lxml")
                except Exception:
                    soup = BeautifulSoup(wrapped, "html5lib")
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse failed: {exc}")
            return 0

        all_rows = soup.find_all("tr", attrs={"data-id": True})
        print(f"[{_SITE_ID}] Found {len(all_rows)} publication rows")

        # Sort newest-first (largest data-id first)
        def _row_key(r):
            try:
                return int(r.get("data-id", 0))
            except (TypeError, ValueError):
                return 0

        all_rows.sort(key=_row_key, reverse=True)

        # Safety cap: 200 "pages" equivalent — here we use a row-count cap
        PAGE_LOG = 50  # log every N rows
        MAX_ROWS = 10000  # hard safety cap

        # ------------------------------------------------------------------
        # 3. Process each row
        # ------------------------------------------------------------------
        for row_idx, row in enumerate(all_rows[:MAX_ROWS]):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_ts > max_wall:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached after {saved} saved. Stopping.")
                break

            if row_idx > 0 and row_idx % PAGE_LOG == 0:
                print(f"[{_SITE_ID}] progress row {row_idx}: saved {saved}/{limit_str}")

            try:
                post_id = str(row.get("data-id", "")).strip()
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)

                tds = row.find_all("td")
                if len(tds) < 4:
                    print(f"[{_SITE_ID}] row {post_id}: only {len(tds)} cells, skipping")
                    continue

                # --- Authors (1st td) ---
                author_names = []
                for li in tds[0].find_all("li"):
                    name = li.get_text(separator=" ", strip=True)
                    name = re.sub(r"\s+", " ", name).strip()
                    if name:
                        author_names.append(name)
                authors_str = "; ".join(author_names)

                # --- Title (2nd td) ---
                title = tds[1].get_text(strip=True)
                if not title:
                    print(f"[{_SITE_ID}] row {post_id}: empty title, skipping")
                    continue

                # --- Year (3rd td) ---
                year = tds[2].get_text(strip=True)
                published_date = year if re.match(r"^\d{4}$", year) else None

                # --- Journal/publication info (4th td) ---
                journal_info = tds[3].get_text(strip=True)

                # Parse journal name and volume from "Journal Name, volume: page"
                journal = ""
                m = re.match(r"^([^,]+),\s*(.*)$", journal_info)
                if m:
                    journal = m.group(1).strip()

                # --- External URL (5th td, "PDF" column) ---
                ext_href = None
                if len(tds) >= 5:
                    a_tag = tds[4].find("a")
                    if a_tag:
                        href = (a_tag.get("href") or "").strip()
                        if href and not href.startswith("file://"):
                            ext_href = href

                # Canonical URL for this record
                if ext_href:
                    canonical_url = ext_href
                else:
                    canonical_url = (
                        f"{_BASE_URL}/dzialalnosc-naukowa/publikacje"
                        + (f"?rocznik={year}" if year else "")
                    )

                # --- DOI ---
                doi = self._extract_doi(ext_href or "", journal_info)

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
                print(f"[{_SITE_ID}] item {row_idx} (id={post_id!r}) failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
