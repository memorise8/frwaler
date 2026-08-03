# -*- coding: utf-8 -*-
"""ASTRON Science Publications crawler.

Publications are sourced from yearly HTML listing files at:
  https://www.astron.nl/~leeuwen/ag/pub-ag.{year}.html

Each entry links to an ADS abstract page (React SPA — no server-rendered
metadata). Abstracts and DOIs are fetched from OpenAlex using the paper title
as the search key. OpenAlex returns abstracts as an inverted-index which is
reconstructed here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
from datetime import datetime

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# BeautifulSoup with fallback parsers
# ---------------------------------------------------------------------------

_BS4_PARSERS = ("html5lib", "lxml", "html.parser")


def _make_soup(raw: str):
    """Try BeautifulSoup parsers in priority order; return soup or None."""
    from bs4 import BeautifulSoup
    for parser in _BS4_PARSERS:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# OpenAlex helpers
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inv_index) -> str:
    """Rebuild abstract text from OpenAlex inverted index {word: [positions]}."""
    if not inv_index:
        return ""
    positions: dict[int, str] = {}
    for word, pos_list in inv_index.items():
        for p in pos_list:
            positions[p] = word
    if not positions:
        return ""
    abstract = " ".join(positions[k] for k in sorted(positions))
    # Strip the "ABSTRACT " header that some publishers embed as the first word
    if re.match(r"^ABSTRACT\s+", abstract):
        abstract = abstract[8:].strip()
    return abstract


def _title_matches(t1: str, t2: str, threshold: float = 0.5) -> bool:
    """Word-overlap guard: returns True when two titles share enough words."""
    stop = {"a", "an", "the", "of", "in", "for", "and", "or", "to", "on",
            "by", "at", "with", "from", "via", "using"}
    norm = lambda s: set(re.sub(r"[^\w\s]", " ", s.lower()).split()) - stop
    w1, w2 = norm(t1), norm(t2)
    if not w1 or not w2:
        return False
    return len(w1 & w2) / max(len(w1), len(w2)) >= threshold


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class ASTRONScienceCrawler(BaseCrawler):
    """Crawler for ASTRON Science publications (2008–present)."""

    site_id   = "science-astron-nl-science-astron"
    site_name = "Custom: science-astron-nl-science-astron"
    base_url  = "https://science.astron.nl"

    _PUB_URL_TMPL  = "https://www.astron.nl/~leeuwen/ag/pub-ag.{year}.html"
    _OPENALEX_BASE = "https://api.openalex.org/works"
    _END_YEAR      = 2008

    # ------------------------------------------------------------------
    # Network helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch *url* with curl; retry up to *retries* times (1 s, 3 s, 9 s)."""
        waits = [1, 3, 9]
        for attempt in range(retries):
            if attempt > 0:
                wait = waits[min(attempt - 1, len(waits) - 1)]
                print(f"[{self.site_id}] retry {attempt}/{retries - 1} for {url[:80]}, wait {wait}s")
                time.sleep(wait)
            try:
                cmd = [
                    "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                    "-A", self.USER_AGENT, "-L", url,
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{self.site_id}] curl error ({url[:60]}): {exc}")
        print(f"[{self.site_id}] fetch failed after {retries} attempts: {url[:80]}")
        return None

    # ------------------------------------------------------------------
    # HTML parser
    # ------------------------------------------------------------------

    def _parse_year_html(self, html: str, year: int) -> list[dict]:
        """Parse pub-ag.{year}.html; return list of raw entry dicts."""
        soup = _make_soup(html)
        if not soup:
            return []

        entries: list[dict] = []
        current_category = "Unknown"

        for tag in soup.find_all(["h2", "li"]):
            if tag.name == "h2":
                current_category = tag.get_text(strip=True)
                continue
            if tag.name != "li":
                continue

            a_tag = tag.find("a")
            if not a_tag:
                continue

            ads_url = (a_tag.get("href") or "").strip()
            title = a_tag.get_text(strip=True)
            if not ads_url or not title:
                continue

            # Bibcode from ADS URL: .../abs/{bibcode}
            m = re.search(r"/abs/([^/?#\s]+)", ads_url)
            if not m:
                continue
            bibcode = m.group(1)

            # Authors: full text before the first ':'
            full_text = tag.get_text()
            colon_pos = full_text.find(":")
            author_raw = full_text[:colon_pos].strip() if colon_pos > 0 else ""
            all_authors = [a.strip() for a in re.split(r",\s*", author_raw) if a.strip()]

            # ASTRON-affiliated authors are wrapped in <b>
            astron_authors = [b.get_text(strip=True) for b in tag.find_all("b")]

            # Citation text after </a>: ", Year, Journal, Volume, Page"
            tag_str = str(tag)
            a_end = tag_str.find("</a>")
            after = ""
            if a_end >= 0:
                after_raw = tag_str[a_end + 4:].replace("</li>", "").strip()
                after = re.sub(r"<[^>]+>", "", after_raw).lstrip(",").strip()

            parts = [p.strip() for p in after.split(",")]
            journal_raw = parts[1] if len(parts) >= 2 else ""
            volume      = parts[2] if len(parts) >= 3 else ""
            page        = parts[3] if len(parts) >= 4 else ""

            entries.append({
                "bibcode":       bibcode,
                "ads_url":       ads_url,
                "title":         title,
                "all_authors":   all_authors,
                "astron_authors": astron_authors,
                "category":      current_category,
                "year":          year,
                "journal_raw":   journal_raw,
                "volume":        volume,
                "page":          page,
            })

        return entries

    # ------------------------------------------------------------------
    # OpenAlex abstract fetcher
    # ------------------------------------------------------------------

    def _get_openalex(self, title: str) -> dict:
        """Search OpenAlex by title; return {'abstract', 'doi', 'publication_date'}."""
        # OpenAlex full-text search — truncate long titles to keep URL sane
        q = urllib.parse.quote(title[:150])
        url = (
            f"{self._OPENALEX_BASE}?search={q}&per_page=5"
            f"&select=title,abstract_inverted_index,doi,publication_date"
        )
        raw = self._curl_get(url)
        if not raw:
            return {}

        try:
            data = json.loads(raw)
        except Exception:
            return {}

        for result in data.get("results") or []:
            result_title = (result.get("title") or "").strip()
            if not _title_matches(title, result_title):
                continue
            abstract = _reconstruct_abstract(result.get("abstract_inverted_index"))
            doi_raw  = (result.get("doi") or "").strip()
            doi      = doi_raw.replace("https://doi.org/", "") if doi_raw else ""
            return {
                "abstract":         abstract,
                "doi":              doi,
                "publication_date": result.get("publication_date") or "",
            }

        return {}

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):  # noqa: C901
        """Crawl ASTRON publications from yearly HTML listing files.

        Iterates years from current year down to _END_YEAR. For each entry the
        ADS URL is used as the canonical record URL; the abstract is fetched
        from OpenAlex by title search (ADS is a React SPA with no server-
        rendered metadata).
        """
        saved           = 0
        seen_urls: set[str] = set()
        limit_or_inf    = limit if limit is not None else float("inf")
        start_time      = time.time()
        max_wall_secs   = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))          # 25 minutes hard cap
        safety_cap      = 200              # page-count safety cap
        pages_done      = 0

        current_year = datetime.now().year
        years        = list(range(current_year, self._END_YEAR - 1, -1))
        total_pages  = len(years)

        for page_idx, year in enumerate(years):
            if saved >= limit_or_inf:
                break
            if pages_done >= safety_cap:
                print(f"[{self.site_id}] safety cap of {safety_cap} pages reached, stopping")
                break
            if time.time() - start_time > max_wall_secs:
                print(f"[{self.site_id}] wall-clock budget exceeded at year {year}, stopping")
                break

            if page_idx > 0 and page_idx % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(
                    f"[{self.site_id}] page {page_idx}/{total_pages}"
                    f" (year {year}): saved {saved}/{lim_str}"
                )

            pub_url = self._PUB_URL_TMPL.format(year=year)
            html    = self._curl_get(pub_url)
            pages_done += 1

            if not html or len(html) < 200:
                print(f"[{self.site_id}] year {year}: no data, skipping")
                continue

            try:
                entries = self._parse_year_html(html, year)
            except Exception as exc:
                print(f"[{self.site_id}] year {year}: parse error: {exc}")
                continue

            if not entries:
                print(f"[{self.site_id}] year {year}: 0 entries parsed")
                continue

            print(f"[{self.site_id}] year {year}: {len(entries)} entries found")

            for entry in entries:
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > max_wall_secs:
                    print(f"[{self.site_id}] wall-clock budget exceeded mid-year, stopping")
                    return saved

                ads_url = entry["ads_url"]
                if ads_url in seen_urls:
                    continue
                seen_urls.add(ads_url)

                try:
                    time.sleep(self._delay)

                    openalex = self._get_openalex(entry["title"])
                    abstract = (openalex.get("abstract") or "").strip()

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {entry['bibcode']}: "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue

                    authors_str = "; ".join(entry["all_authors"]) if entry["all_authors"] else ""
                    doi         = openalex.get("doi") or ""

                    # Publication date: prefer OpenAlex ISO date, fall back to year
                    pub_date_raw  = openalex.get("publication_date") or ""
                    published_date = f"{entry['year']}-01-01"
                    if pub_date_raw:
                        dm = re.match(r"(\d{4}-\d{2}-\d{2})", pub_date_raw)
                        if dm:
                            published_date = dm.group(1)
                        elif re.match(r"\d{4}", pub_date_raw):
                            published_date = f"{pub_date_raw[:4]}-01-01"

                    listed_date = f"{entry['year']}-01-01"
                    bibcode     = entry["bibcode"]

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":       bibcode,
                        "post_number":       bibcode,
                        "title":             entry["title"],
                        "abstract":          abstract,
                        "published_date":    published_date,
                        "posted_date":       listed_date,
                        "url":               ads_url,
                        "pdf_url":           None,
                        "authors":           authors_str,
                        "publisher":         "ASTRON",
                        "journal":           entry["journal_raw"],
                        "keywords":          "",
                        "doi":               doi,
                        "category":          entry["category"],
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date":    listed_date,
                            "bibcode":        bibcode,
                            "journal_raw":    entry["journal_raw"],
                            "volume":         entry["volume"],
                            "issue":          "",
                            "firstpage":      entry["page"],
                            "astron_authors": entry["astron_authors"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] saved {saved}/{lim_str}: {entry['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {entry.get('bibcode', '?')} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
