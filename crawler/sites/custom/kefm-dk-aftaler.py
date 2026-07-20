# -*- coding: utf-8 -*-
"""Crawler for KEFM (Klima-, Energi- og Forsyningsministeriet) - Aftaler.

Starting URL: https://www.kefm.dk/aftaler
All agreements are listed on a single static HTML page with multiple
time-period sections. No JSON API; content is HTML with direct PDF links.
"""

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

_PUBLISHER = "Klima-, Energi- og Forsyningsministeriet (KEFM)"

_DA_MONTHS = {
    "januar": "01", "februar": "02", "marts": "03", "april": "04",
    "maj": "05", "juni": "06", "juli": "07", "august": "08",
    "september": "09", "oktober": "10", "november": "11", "december": "12",
}

_BS4_PARSERS = ("html5lib", "lxml", "html.parser")


def _make_soup(raw: str):
    from bs4 import BeautifulSoup
    for parser in _BS4_PARSERS:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _parse_date(text: str) -> str:
    """Extract date from Danish text like '(februar 2026)' → '2026-02'."""
    m = re.search(r"\(([a-z\xe6\xf8\xe5]+)\s+(\d{4})\)", text.lower())
    if m:
        month_num = _DA_MONTHS.get(m.group(1))
        if month_num:
            return f"{m.group(2)}-{month_num}"
    m = re.search(r"\((\d{4})\)", text)
    if m:
        return m.group(1)
    return ""


def _resolve_url(href: str, base: str = "https://www.kefm.dk") -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base + href
    if href.startswith("http"):
        return href
    return urljoin(base, href)


def _media_id(url: str) -> str | None:
    m = re.search(r"/[Mm]edia/(\d+)/", url)
    return m.group(1) if m else None


def _filename_from_url(url: str) -> str:
    try:
        name = Path(urlparse(url).path).name
        return unquote(name) if name else ""
    except Exception:
        return ""


class KefmDkAftalerCrawler(BaseCrawler):
    site_id = "kefm-dk-aftaler"
    site_name = "Custom: kefm-dk-aftaler"
    base_url = "https://www.kefm.dk"

    _START_URL = "https://www.kefm.dk/aftaler"

    # ------------------------------------------------------------------
    # curl helper (retry with exponential backoff 1s / 3s / 9s)
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout.decode("utf-8", errors="replace")
                if raw.strip():
                    return raw
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}")
            wait = 3 ** attempt  # 1s, 3s, 9s
            if attempt < 2:
                print(f"[{self.site_id}] Retrying {url} in {wait}s...")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_MINUTES = 25
        saved = 0
        seen_urls: set = set()

        # Fetch the single listing page
        raw = self._curl_get(self._START_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch main page")
            return 0

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
            return 0

        if soup is None:
            print(f"[{self.site_id}] Could not parse HTML with any parser")
            return 0

        # Scope to main content to avoid footer/navigation sections
        main = soup.find(attrs={"role": "main"}) or soup

        # Collect all records from all content sections
        records = []

        for section in main.find_all("div", class_="module"):
            heading_tag = section.find(["h1", "h2"])
            section_heading = heading_tag.get_text(strip=True) if heading_tag else "Aftaler"

            rich_text = section.find("div", class_="rich-text")
            if not rich_text:
                continue

            current_subsection = ""

            try:
                children = list(rich_text.children)
            except Exception:
                continue

            for child in children:
                # Skip NavigableString and other non-tag nodes
                if not hasattr(child, "name") or child.name is None:
                    continue

                if child.name in ("h2", "h3", "h4"):
                    current_subsection = child.get_text(strip=True)
                    continue

                if child.name != "p":
                    continue

                try:
                    links = child.find_all("a", href=True)
                except Exception:
                    continue

                if not links:
                    continue

                try:
                    para_text = child.get_text(separator=" ", strip=True)
                except Exception:
                    para_text = ""

                date_str = _parse_date(para_text)

                for a_tag in links:
                    href = (a_tag.get("href") or "").strip()
                    if not href or href == "#":
                        continue

                    try:
                        title = a_tag.get_text(strip=True)
                    except Exception:
                        continue

                    if not title:
                        continue

                    resolved = _resolve_url(href)

                    if resolved in seen_urls:
                        continue
                    seen_urls.add(resolved)

                    # Native IDs: prefer itemid, then Media numeric ID
                    item_id = (a_tag.get("itemid") or "").strip()
                    mid = _media_id(resolved)
                    external_id = item_id or mid or hashlib.md5(resolved.encode()).hexdigest()[:16]
                    post_number = item_id or mid or None

                    is_pdf = (
                        resolved.lower().endswith(".pdf")
                        or "/Media/" in resolved
                        or "/media/" in resolved
                    )
                    orig_filename = _filename_from_url(resolved) if is_pdf else ""

                    category = section_heading
                    if current_subsection:
                        category = f"{section_heading} — {current_subsection}"

                    # Build abstract from available metadata.
                    # The fixed suffix alone is ~120 chars, so total is always >= 100.
                    date_part = f" ({date_str})" if date_str else ""
                    sub_part = (
                        f"\n\nUnderkategori: {current_subsection}"
                        if current_subsection
                        else ""
                    )
                    abstract = (
                        f"{section_heading}: {title}{date_part}."
                        f"{sub_part}\n\n"
                        f"Udgivet af {_PUBLISHER}. "
                        f"Aftalen er tilg\xe6ngelig som PDF-dokument via det direkte link."
                    )

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping (abstract too short): {title[:60]}")
                        continue

                    records.append({
                        "site_id": self.site_id,
                        "external_id": str(external_id),
                        "post_number": str(post_number) if post_number else None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str or None,
                        "posted_date": date_str or None,
                        "listed_date": date_str or None,
                        "publisher": _PUBLISHER,
                        "authors": None,
                        "department": None,
                        "journal": None,
                        "url": self._START_URL,
                        "pdf_url": resolved if is_pdf else None,
                        "category": category,
                        "keywords": None,
                        "doi": None,
                        "original_filename": orig_filename or None,
                        "metadata": json.dumps({
                            "posted_date": date_str or None,
                            "section": section_heading,
                            "subsection": current_subsection or None,
                            "itemid": item_id or None,
                            "media_id": mid or None,
                            "originalFilename": orig_filename or None,
                            "href_type": (a_tag.get("type") or ""),
                        }, ensure_ascii=False),
                    })

        # This site is a single page — log as "page 1" for consistency
        print(f"[{self.site_id}] page 1: found {len(records)} agreements")

        # Save up to limit (pagination equivalent: we have all records in memory)
        limit_or_inf = limit if limit is not None else float("inf")
        page_log_counter = 0

        for i, record in enumerate(records):
            if limit is not None and saved >= limit:
                break

            elapsed_min = (time.time() - start_time) / 60
            if elapsed_min >= MAX_MINUTES:
                print(
                    f"[{self.site_id}] Time budget ({MAX_MINUTES}min) reached "
                    f"after {saved} saved records. Exiting cleanly."
                )
                break

            try:
                self._save_paper(record)
                saved += 1
                counter = f"{saved}/{limit}" if limit is not None else str(saved)
                print(f"[{self.site_id}] Saved {counter}: {record['title'][:60]}")

                page_log_counter += 1
                if page_log_counter % 10 == 0:
                    print(
                        f"[{self.site_id}] page 1: saved {saved}/{limit_or_inf}"
                    )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {i} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
