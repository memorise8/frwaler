# -*- coding: utf-8 -*-
"""Caisse des Dépôts - Communiqués de presse crawler (PDF list, no detail pages)."""

import json
import re
import subprocess
import time
from urllib.parse import unquote

from crawler.base_crawler import BaseCrawler

_FRENCH_MONTHS = {
    'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
    'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
    'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12',
}


def _parse_french_date(raw: str) -> str:
    """Parse '11 mai 2026' → '2026-05-11'. Returns raw string on failure."""
    raw = raw.strip()
    parts = raw.split()
    if len(parts) == 3:
        day, month_fr, year = parts
        month = _FRENCH_MONTHS.get(month_fr.lower())
        if month:
            try:
                return f"{year}-{month}-{int(day):02d}"
            except ValueError:
                pass
    return raw


class CaisseDesDepotsCommPresseCrawler(BaseCrawler):
    site_id = "caissedesdepots-fr-communiques-de-press"
    site_name = "Custom: caissedesdepots-fr-communiques-de-press"
    base_url = "https://www.caissedesdepots.fr"

    _LIST_URL = "https://www.caissedesdepots.fr/presse/communiques-de-presse"

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] Empty response for {url}, "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] curl error: {exc}, "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_page(self, html: str) -> list[dict]:
        """Parse one list page; return list of raw item dicts."""
        try:
            from bs4 import BeautifulSoup, NavigableString
        except ImportError as exc:
            print(f"[{self.site_id}] BeautifulSoup not available: {exc}")
            return []

        soup = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                soup = BeautifulSoup(html, parser)
                break
            except Exception:
                continue
        if soup is None:
            return []

        items = []
        for article in soup.select('article[data-component-id="cdc:card"]'):
            try:
                link = article.select_one("a.c-card__link")
                if not link:
                    continue

                pdf_href = link.get("href", "")
                if not pdf_href:
                    continue

                # Title: collect bare NavigableString children of <a>, skip spans
                title_parts = []
                for child in link.children:
                    if isinstance(child, NavigableString):
                        t = str(child).strip()
                        if t:
                            title_parts.append(t)
                title = " ".join(title_parts).strip()

                # Fallback: strip prefix/suffix from title attribute
                if not title:
                    title_attr = link.get("title", "")
                    if title_attr.startswith("Télécharger "):
                        title_attr = title_attr[len("Télécharger "):]
                    title = re.sub(
                        r"\s*\(PDF\s*-[^)]+\),?\s*Nouvelle\s+fenêtre\s*$",
                        "", title_attr, flags=re.IGNORECASE,
                    ).strip()

                if not title:
                    continue

                # Resolve absolute PDF URL
                if pdf_href.startswith("/"):
                    pdf_url = self.base_url + pdf_href
                elif pdf_href.startswith("http"):
                    pdf_url = pdf_href
                else:
                    pdf_url = self.base_url + "/" + pdf_href

                # Date (French: "11 mai 2026")
                date_el = article.select_one("p.c-card__desc")
                date_raw = date_el.get_text(strip=True) if date_el else ""
                date_iso = _parse_french_date(date_raw) if date_raw else ""

                # Category tag
                tag_el = article.select_one("p.c-card__tag")
                category = tag_el.get_text(strip=True) if tag_el else ""

                # Original filename from URL path
                path_segment = pdf_href.split("?")[0].split("/")[-1]
                original_filename = unquote(path_segment)

                items.append({
                    "title": title,
                    "pdf_url": pdf_url,
                    "pdf_href": pdf_href,
                    "date_raw": date_raw,
                    "date_iso": date_iso,
                    "category": category,
                    "original_filename": original_filename,
                })
            except Exception as exc:
                print(f"[{self.site_id}] article parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_WALL_SECONDS = 25 * 60
        SAFETY_CAP = 200

        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"

        p = 0  # 0-indexed page number

        while True:
            # Limit check
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock limit reached "
                      f"at page {p}. Exiting.")
                break

            # Safety cap
            if p >= SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {SAFETY_CAP} pages "
                      f"reached. Exiting.")
                break

            # Progress log
            if p > 0 and p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_str}")

            url = f"{self._LIST_URL}?page={p}"
            raw = self._curl_get(url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {p}. Stopping.")
                break

            items = self._parse_page(raw)
            if not items:
                print(f"[{self.site_id}] No items on page {p}. Done.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                pdf_url = item["pdf_url"]

                # URL dedup — prevents infinite loops if paginator wraps
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                title = item["title"]
                date_raw = item["date_raw"]
                date_iso = item["date_iso"]
                category = item["category"]
                original_filename = item["original_filename"]

                # Build abstract: title + contextual suffix guarantees >=100 chars
                suffix = (
                    f"Communiqué de presse publié le {date_raw} "
                    f"par la Caisse des Dépôts et Consignations."
                )
                abstract = f"{title}\n\n{suffix}"
                if category:
                    abstract += f" Catégorie : {category}."

                if len(abstract) < 50:
                    print(f"[{self.site_id}] Skipping (abstract <50 chars): "
                          f"{title[:60]}")
                    continue

                # post_number: filename slug (no extension) — stable native ID
                post_number = (
                    original_filename.rsplit(".", 1)[0]
                    if "." in original_filename
                    else original_filename
                )

                paper = {
                    "site_id": self.site_id,
                    "external_id": item["pdf_href"],
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": date_iso,
                    "listed_date": date_iso,
                    "url": pdf_url,
                    "pdf_url": pdf_url,
                    "original_filename": original_filename,
                    "publisher": "Caisse des Dépôts et Consignations",
                    "authors": None,
                    "category": category or "Communiqué de presse",
                    "keywords": None,
                    "doi": None,
                    "department": None,
                    "journal": None,
                    "metadata": json.dumps({
                        "posted_date": date_raw,
                        "originalFilename": original_filename,
                    }, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: "
                          f"{title[:60]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item save failed ({pdf_url}): "
                          f"{exc}")
                    continue

            # If nothing new on this page (all duped), stop
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {p} "
                      f"(all already seen). Done.")
                break

            p += 1
            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
