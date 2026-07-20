# -*- coding: utf-8 -*-
"""Crawler for NIEHS News Releases (www.niehs.nih.gov/newsreleases).

All NIEHS-originated releases are embedded in the single list-page HTML;
no JSON API pagination is needed.  For each release the detail page is
fetched to obtain the full article body used as the abstract.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_MONTH_SHORT = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}
_MONTH_LONG = {
    "JANUARY": "01", "FEBRUARY": "02", "MARCH": "03", "APRIL": "04",
    "MAY": "05", "JUNE": "06", "JULY": "07", "AUGUST": "08",
    "SEPTEMBER": "09", "OCTOBER": "10", "NOVEMBER": "11", "DECEMBER": "12",
}
_MONTH_MAP = {**_MONTH_SHORT, **_MONTH_LONG}


class NiehsNihGovNewsReleasesCrawler(BaseCrawler):
    """Crawler for NIEHS News Releases."""

    site_id = "niehs-nih-gov-newsreleases"
    site_name = "Custom: niehs-nih-gov-newsreleases"
    base_url = "https://www.niehs.nih.gov"

    _LIST_URL = "https://www.niehs.nih.gov/newsreleases"
    _MIN_ABSTRACT = 50   # chars; items below this are skipped
    _PAGE_CAP = 200      # safety cap on list-page fetches
    _RETRY_WAITS = (1, 3, 9)
    _MAX_SECONDS = 25 * 60  # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
        """GET via curl with TLS workaround and exponential backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        last_err = "unknown"
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False,
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_err = (
                    "empty response" if result.returncode == 0
                    else f"exit={result.returncode}"
                )
            except Exception as exc:
                last_err = str(exc)

            if attempt < 3:
                wait = self._RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last_err}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl gave up after 3 attempts for {url}: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw: str):
        """Parse HTML with html5lib → lxml → html.parser fallback."""
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value) -> str:
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @staticmethod
    def _meta(soup, *props) -> str:
        if soup is None:
            return ""
        for prop in props:
            for attr in ("property", "name"):
                node = soup.find("meta", attrs={attr: prop})
                if node and node.get("content"):
                    return node["content"].strip()
        return ""

    @staticmethod
    def _parse_date_monthyear(day: str, monthyear: str) -> str:
        """Convert '13' + 'NOV 2025' → '2025-11-13'."""
        try:
            parts = monthyear.strip().upper().split()
            if len(parts) == 2:
                mon = _MONTH_MAP.get(parts[0], "")
                if mon:
                    d = day.strip().zfill(2)
                    return f"{parts[1]}-{mon}-{d}"
        except Exception:
            pass
        return ""

    @staticmethod
    def _parse_date_text(text: str) -> str:
        """Extract 'September 18, 2023' → '2023-09-18' from free text."""
        m = re.search(
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+(\d{1,2}),\s+(\d{4})",
            text, re.I,
        )
        if m:
            mon = _MONTH_MAP.get(m.group(1).upper()[:3], "")
            if not mon:
                mon = _MONTH_MAP.get(m.group(1).upper(), "")
            return f"{m.group(3)}-{mon}-{m.group(2).zfill(2)}"
        return ""

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _fetch_entries(self) -> list[dict]:
        """Fetch the list page and return all internal NIEHS release entries."""
        raw = self._curl(self._LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch list page")
            return []

        soup = self._parse_html(raw)
        if soup is None:
            # Regex fallback
            slugs = re.findall(r'href="(/newsreleases/[^"]+)"', raw)
            return [
                {"url": f"https://www.niehs.nih.gov{s}", "title": "", "date": "", "blurb": ""}
                for s in dict.fromkeys(slugs)  # deduplicate, preserve order
            ]

        container = soup.find(id="newsreleases-container-internal") or soup
        entries = []
        seen = set()

        for div in container.find_all("div", class_="newsreleases_result"):
            try:
                # Link — must be internal (/newsreleases/...)
                title_div = div.find(class_="newsreleases_result_description_metaData-title")
                if not title_div:
                    continue
                a = title_div.find("a", href=True)
                if not a:
                    continue
                href = a["href"].strip()
                if not href.startswith("/newsreleases/"):
                    continue
                url = f"https://www.niehs.nih.gov{href}"
                if url in seen:
                    continue
                seen.add(url)

                title = self._one_line(a.get_text(" ", strip=True))

                # Date from list card
                day_el = div.find(class_="newsreleases_result_dateorigin-day")
                mon_el = div.find(class_="newsreleases_result_dateorigin-monthyear")
                day = day_el.get_text(strip=True) if day_el else ""
                monthyear = mon_el.get_text(strip=True) if mon_el else ""
                date_str = self._parse_date_monthyear(day, monthyear)

                # Blurb (the short description div that isn't the title)
                blurb = ""
                meta_div = div.find(class_="newsreleases_result_description_metaData")
                if meta_div:
                    for child in meta_div.find_all("div", recursive=False):
                        cls_list = " ".join(child.get("class", []))
                        if "title" in cls_list or "agency" in cls_list or "subtitle" in cls_list:
                            continue
                        txt = self._one_line(child.get_text(" ", strip=True))
                        if txt and txt != title:
                            blurb = txt
                            break

                entries.append({"url": url, "title": title, "date": date_str, "blurb": blurb})
            except Exception as exc:
                print(f"[{self.site_id}] list-entry parse error: {exc}")
                continue

        return entries

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict | None:
        """Fetch a detail page and return parsed fields, or None on failure."""
        raw = self._curl(url)
        if not raw:
            return None

        soup = self._parse_html(raw)

        # Title
        title = ""
        if soup:
            title = self._meta(soup, "og:title") or self._one_line(
                soup.title.get_text() if soup.title else ""
            )
            # Strip trailing site name
            title = re.sub(r"\s*\|.*$", "", title).strip()

        # Date from article text
        date_str = self._parse_date_text(raw)

        # Abstract: full article body
        abstract = ""
        if soup:
            article = soup.find("article")
            if article:
                # Remove nav, aside, script, style noise
                for tag in article.find_all(["script", "style", "nav", "aside", "footer"]):
                    tag.decompose()
                abstract = self._clean(article.get_text("\n", strip=True))
            if not abstract:
                main = soup.find("main")
                if main:
                    for tag in main.find_all(["script", "style", "nav", "aside", "footer"]):
                        tag.decompose()
                    abstract = self._clean(main.get_text("\n", strip=True))
        else:
            # Regex fallback
            art_m = re.search(r"<article[^>]*>(.*?)</article>", raw, re.DOTALL)
            if art_m:
                txt = re.sub(r"<[^>]+>", " ", art_m.group(1))
                abstract = self._clean(unescape(txt))

        # OG description as a short summary / fallback
        og_desc = ""
        if soup:
            og_desc = self._meta(soup, "og:description")

        external_id = urlparse(url).path.strip("/").rsplit("/", 1)[-1]

        return {
            "title": title,
            "date": date_str,
            "abstract": abstract,
            "og_description": og_desc,
            "external_id": external_id,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NIEHS news releases and save via _save_paper().

        Fetches the single list page (all NIEHS-originated releases are
        embedded in its HTML), then fetches each detail page for the full
        article body.  Stops when ``limit`` is reached, the list is
        exhausted, or the 25-minute wall-clock budget is consumed.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        print(f"[{self.site_id}] Starting crawl (limit={limit_or_inf})")

        # ------------------------------------------------------------------
        # Step 1 — Discover: fetch the list page (all items are in HTML)
        # ------------------------------------------------------------------
        # The site embeds every release in the page HTML; the JS only
        # shows/hides items client-side.  We therefore treat this as a
        # single-page discovery step (no server-side pagination needed).
        p = 1
        entries = self._fetch_entries()
        if not entries:
            print(f"[{self.site_id}] No entries found on list page.")
            return 0

        print(f"[{self.site_id}] Found {len(entries)} internal NIEHS entries")

        # ------------------------------------------------------------------
        # Step 2 — Crawl: fetch each detail page
        # ------------------------------------------------------------------
        for idx, entry in enumerate(entries):
            # Limit guard
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed >= self._MAX_SECONDS:
                print(
                    f"[{self.site_id}] Wall-clock budget exceeded ({elapsed:.0f}s). "
                    "Exiting cleanly."
                )
                break

            url = entry["url"]

            # Deduplication
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Progress log every 10 items
            if idx > 0 and idx % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

            # Per-item fetch + parse + save — isolated so one bad page can't abort the run
            try:
                time.sleep(self._delay)

                detail = self._fetch_detail(url)
                if not detail:
                    print(f"[{self.site_id}] item {url} failed: fetch returned nothing")
                    continue

                title = detail.get("title") or entry.get("title") or ""
                abstract = detail.get("abstract") or ""
                date_str = detail.get("date") or entry.get("date") or ""
                og_desc = detail.get("og_description") or ""
                external_id = detail.get("external_id") or url.split("/")[-1]

                # Use OG description as fallback if article body is empty
                if not abstract and og_desc:
                    abstract = og_desc

                # Skip items whose abstract is too short
                if len(abstract) < self._MIN_ABSTRACT:
                    print(
                        f"[{self.site_id}] Skipping '{title[:50]}' "
                        f"(abstract too short: {len(abstract)} chars)"
                    )
                    continue

                paper = {
                    "id": None,
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "title": title,
                    "authors": json.dumps([], ensure_ascii=False),
                    "abstract": abstract,
                    "category": "News Release",
                    "keywords": json.dumps([], ensure_ascii=False),
                    "published_date": date_str,
                    "url": url,
                    "pdf_url": "",
                    "doi": "",
                    "department": "National Institute of Environmental Health Sciences",
                    "metadata": json.dumps(
                        {
                            "blurb": entry.get("blurb", ""),
                            "og_description": og_desc,
                            "list_date": entry.get("date", ""),
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {url} failed: {exc}")
                continue

        # Safety cap check: since we have a single list page and iterate
        # through entries, p never exceeds 1 here.  Log would appear if
        # we extended to multi-page sites.
        if p >= self._PAGE_CAP:
            print(f"[{self.site_id}] Safety cap of {self._PAGE_CAP} pages reached.")

        print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")
        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
