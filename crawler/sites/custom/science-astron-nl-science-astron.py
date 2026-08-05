# -*- coding: utf-8 -*-
"""ASTRON Science Publications crawler.

The old source — yearly HTML listing files at
``https://www.astron.nl/~leeuwen/ag/pub-ag.{year}.html`` — is gone: the
``~leeuwen`` personal-page space 404s ("Page not found | ASTRON"), and the
``science.astron.nl`` subdomain the crawler's ``base_url`` pointed at
(formerly hosting the same content) now refuses connections outright —
ASTRON's site was migrated to a plain WordPress site
(``www.astron.nl``) with no equivalent hand-maintained publication index;
``/category/science/`` there is just a news blog, not a bibliography.

Since there is no successor listing page, this crawler now sources the
publication list directly from OpenAlex's institution-filtered works API
(cursor-paginated, newest first) for ASTRON's OpenAlex institution id
``I922237871`` ("Netherlands Institute for Radio Astronomy", ~9.9k works).
OpenAlex already returned abstracts (as an inverted index, reconstructed
below) and DOIs for this crawler's old per-title lookups, so this reuses
the same endpoint/helper — just as the primary listing source instead of a
secondary abstract lookup.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

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


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class ASTRONScienceCrawler(BaseCrawler):
    """Crawler for ASTRON Science publications, sourced from OpenAlex."""

    site_id   = "science-astron-nl-science-astron"
    site_name = "Custom: science-astron-nl-science-astron"
    base_url  = "https://science.astron.nl"

    _OPENALEX_BASE     = "https://api.openalex.org/works"
    _OPENALEX_INST_ID  = "I922237871"   # OpenAlex institution id for ASTRON (NL)
    _PER_PAGE          = 50

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
    # OpenAlex listing fetcher
    # ------------------------------------------------------------------

    def _fetch_openalex_page(self, cursor: str) -> dict | None:
        """Fetch one cursor-paginated page of ASTRON works from OpenAlex."""
        select = "id,title,abstract_inverted_index,doi,publication_date,authorships,primary_location,type"
        url = (
            f"{self._OPENALEX_BASE}?filter=institutions.id:{self._OPENALEX_INST_ID}"
            f"&sort=publication_date:desc&per_page={self._PER_PAGE}"
            f"&cursor={urllib.parse.quote(cursor)}&select={select}"
        )
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] JSON parse error: {exc}")
            return None

    @staticmethod
    def _parse_work(work: dict) -> dict:
        """Convert one OpenAlex work record into a raw entry dict."""
        openalex_id = (work.get("id") or "").rsplit("/", 1)[-1]
        title = (work.get("title") or "").strip()
        abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

        doi_raw = (work.get("doi") or "").strip()
        doi = doi_raw.replace("https://doi.org/", "") if doi_raw else ""

        authors = [
            (a.get("author") or {}).get("display_name", "").strip()
            for a in (work.get("authorships") or [])
        ]
        authors = [a for a in authors if a]

        pub_date = (work.get("publication_date") or "").strip()

        primary_location = work.get("primary_location") or {}
        source = (primary_location.get("source") or {}).get("display_name") or ""
        landing_url = primary_location.get("landing_page_url") or ""
        pdf_url = primary_location.get("pdf_url") or None

        return {
            "openalex_id":  openalex_id,
            "title":        title,
            "abstract":     abstract,
            "doi":          doi,
            "authors":      authors,
            "pub_date":     pub_date,
            "journal":      source,
            "url":          landing_url or (f"https://openalex.org/{openalex_id}" if openalex_id else ""),
            "pdf_url":      pdf_url,
            "category":     work.get("type") or "",
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):  # noqa: C901
        """Crawl ASTRON's publication list via OpenAlex's institution-filtered
        works API (cursor pagination, newest first).
        """
        saved         = 0
        seen_ids: set[str] = set()
        limit_or_inf  = limit if limit is not None else float("inf")
        start_time    = time.time()
        max_wall_secs = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25 minutes hard cap
        safety_cap    = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))           # page-count safety cap
        pages_done    = 0
        cursor        = "*"

        while cursor:
            if saved >= limit_or_inf:
                break
            if pages_done >= safety_cap:
                print(f"[{self.site_id}] safety cap of {safety_cap} pages reached, stopping")
                break
            if time.time() - start_time > max_wall_secs:
                print(f"[{self.site_id}] wall-clock budget exceeded, stopping")
                break

            data = self._fetch_openalex_page(cursor)
            pages_done += 1
            if not data:
                print(f"[{self.site_id}] page {pages_done}: fetch/parse failed, stopping")
                break

            results = data.get("results") or []
            if pages_done == 1:
                total = (data.get("meta") or {}).get("count")
                print(f"[{self.site_id}] OpenAlex reports {total} total works for ASTRON")

            if not results:
                print(f"[{self.site_id}] page {pages_done}: 0 results, done")
                break

            if pages_done % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {pages_done}: saved {saved}/{lim_str}")

            for work in results:
                if saved >= limit_or_inf:
                    break

                try:
                    entry = self._parse_work(work)
                except Exception as exc:
                    print(f"[{self.site_id}] item parse error: {exc}")
                    continue

                openalex_id = entry["openalex_id"]
                if not openalex_id or not entry["title"] or openalex_id in seen_ids:
                    continue
                seen_ids.add(openalex_id)

                abstract = entry["abstract"]
                if len(abstract) < 50:
                    print(f"[{self.site_id}] item {openalex_id}: abstract too short "
                          f"({len(abstract)} chars), skipping")
                    continue

                published_date = entry["pub_date"] or None

                paper = {
                    "site_id":           self.site_id,
                    "external_id":       openalex_id,
                    "post_number":       openalex_id,
                    "title":             entry["title"],
                    "abstract":          abstract,
                    "published_date":    published_date,
                    "posted_date":       published_date,
                    "url":               entry["url"],
                    "pdf_url":           entry["pdf_url"],
                    "authors":           "; ".join(entry["authors"]),
                    "publisher":         "ASTRON",
                    "journal":           entry["journal"],
                    "keywords":          "",
                    "doi":               entry["doi"],
                    "category":          entry["category"],
                    "original_filename": None,
                    "metadata": json.dumps({
                        "posted_date": published_date,
                        "openalex_id": openalex_id,
                        "journal_raw": entry["journal"],
                    }, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] saved {saved}/{lim_str}: {entry['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {openalex_id} failed: {exc}")
                    continue

                time.sleep(self._delay)

            cursor = (data.get("meta") or {}).get("next_cursor") or ""

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
