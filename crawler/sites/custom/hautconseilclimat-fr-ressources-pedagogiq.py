# -*- coding: utf-8 -*-
"""Crawler for Haut Conseil pour le Climat — Ressources pédagogiques.

All 238 resources live on a single HTML page (no pagination). Each item
is represented by an <article> card and a corresponding <div id="modalN">
that carries the full title, credits, and download URL. Sections (strates)
give the category: Visuels clés, Graphiques, Infographies, Cartographies,
Tableaux.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.hautconseilclimat.fr/ressources-pedagogiques/"
_HCC_DESCRIPTION = (
    "Ressource pédagogique publiée par le Haut Conseil pour le Climat, "
    "organisme indépendant chargé d'évaluer la mise en œuvre des politiques "
    "et mesures publiques pour réduire les émissions de gaz à effet de serre "
    "de la France."
)


class HautConseilClimatRessourcesPedagogiqCrawler(BaseCrawler):
    """Crawler for hautconseilclimat.fr educational resources."""

    site_id = "hautconseilclimat-fr-ressources-pedagogiq"
    site_name = "Custom: hautconseilclimat-fr-ressources-pedagogiq"
    base_url = "https://www.hautconseilclimat.fr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential backoff."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            url,
        ]
        backoff = (1, 3, 9)
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.returncode == 0 and result.stdout:
                    try:
                        return result.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return result.stdout.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = backoff[attempt]
                    print(f"[{self.site_id}] Empty response (attempt {attempt+1}/3), "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    wait = backoff[attempt]
                    print(f"[{self.site_id}] Timeout (attempt {attempt+1}/3), "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = backoff[attempt]
                    print(f"[{self.site_id}] curl error {exc} (attempt {attempt+1}/3), "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str):
        """Parse HTML with html5lib → lxml → html.parser fallback."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean_text(tag) -> str:
        """Get clean text from a BS4 tag, stripping inner tags."""
        if tag is None:
            return ""
        return re.sub(r"\s+", " ", tag.get_text(separator=" ")).strip()

    @staticmethod
    def _date_from_url(url: str) -> str:
        """Extract YYYY-MM-DD from /uploads/YYYY/MM/ in the URL."""
        m = re.search(r"/uploads/(\d{4})/(\d{2})/", url)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"
        return ""

    @staticmethod
    def _filename_from_url(url: str) -> str:
        """Return the last path segment of a URL."""
        if not url:
            return ""
        path = urlparse(url).path
        return path.rsplit("/", 1)[-1] if "/" in path else path

    # ------------------------------------------------------------------
    # Page parsing
    # ------------------------------------------------------------------

    def _parse_page(self, html: str) -> list[dict]:
        """Return a list of resource dicts parsed from the single page."""
        soup = self._make_soup(html)
        if soup is None:
            print(f"[{self.site_id}] Failed to build BeautifulSoup tree.")
            return []

        # Map modal_id → category from the strate sections
        modal_to_category: dict[str, str] = {}
        for section in soup.find_all("section", id=re.compile(r"^strate-\d+$")):
            title_tag = section.find(class_=re.compile(r"page-title"))
            cat = self._clean_text(title_tag) if title_tag else section.get("id", "")
            for link in section.find_all("a", href=re.compile(r"^#modal\d+")):
                modal_ref = link.get("href", "").lstrip("#")
                if modal_ref:
                    modal_to_category[modal_ref] = cat

        items: list[dict] = []
        for modal_div in soup.find_all("div", id=re.compile(r"^modal\d+$")):
            modal_id = modal_div.get("id", "")
            modal_num = re.sub(r"\D", "", modal_id)

            h3 = modal_div.find("h3", class_="title")
            title = self._clean_text(h3)

            credits_tag = modal_div.find(class_=re.compile(r"credits"))
            credits_raw = self._clean_text(credits_tag)
            credits = re.sub(r"^Cr[eé]dits\s*:\s*", "", credits_raw,
                             flags=re.IGNORECASE).strip()

            dl_link = modal_div.find("a", class_=re.compile(r"download"))
            download_url = dl_link.get("href", "").strip() if dl_link else ""

            img_tag = modal_div.find("img")
            image_url = ""
            if img_tag:
                image_url = (img_tag.get("data-src", "")
                             or img_tag.get("src", "")).strip()

            category = modal_to_category.get(modal_id, "")

            items.append({
                "modal_id": modal_id,
                "modal_num": modal_num,
                "title": title,
                "credits": credits,
                "download_url": download_url,
                "image_url": image_url,
                "category": category,
            })

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl all resources from the single-page listing.

        All 238 items live in one HTML document, so there is no
        page-level loop. The limit parameter is honoured exactly.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        print(f"[{self.site_id}] Fetching {_LIST_URL}")
        raw = self._curl_get(_LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch main page. Exiting.")
            return 0

        items = self._parse_page(raw)
        print(f"[{self.site_id}] Found {len(items)} resources on page. "
              f"Saving up to {limit_str}.")

        for idx, item in enumerate(items):
            # Respect limit
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached. Stopping early.")
                break

            # Progress log every 10 items (analogous to every 10 pages)
            if idx > 0 and idx % 10 == 0:
                print(f"[{self.site_id}] item {idx}: saved {saved}/{limit_str}")

            try:
                title = item["title"]
                download_url = item["download_url"]
                image_url = item["image_url"]
                category = item["category"]
                credits = item["credits"]
                modal_id = item["modal_id"]
                modal_num = item["modal_num"]

                # Skip blank placeholder modals (no title, no download URL)
                if not title and not download_url:
                    print(f"[{self.site_id}] Skipping blank placeholder {modal_id}.")
                    continue

                # Deduplication by download URL or image URL or title
                dedup_key = download_url or image_url or title
                if dedup_key in seen_urls:
                    print(f"[{self.site_id}] Skipping duplicate {modal_id}: "
                          f"{title[:50]!r}")
                    continue
                seen_urls.add(dedup_key)

                # Date from file URL
                date_url = download_url or image_url
                published_date = self._date_from_url(date_url)

                # Filename
                original_filename = (self._filename_from_url(download_url)
                                     or self._filename_from_url(image_url))

                # Build abstract — always ≥ 100 chars by design:
                # shortest possible: short title + no credits + _HCC_DESCRIPTION (~180 chars)
                parts: list[str] = []
                if category and title:
                    parts.append(f"{category} — {title}.")
                elif title:
                    parts.append(f"{title}.")
                elif category:
                    parts.append(f"{category}.")
                if credits:
                    parts.append(f"Source : {credits}.")
                parts.append(_HCC_DESCRIPTION)
                abstract = " ".join(parts)

                if len(abstract) < 50:
                    print(f"[{self.site_id}] Skipping {modal_id}: "
                          f"abstract too short ({len(abstract)} chars).")
                    continue

                resource_url = _LIST_URL + f"#{modal_id}"

                paper = {
                    "site_id": self.site_id,
                    "external_id": modal_num,
                    "post_number": modal_num,
                    "title": title or f"[Ressource {modal_num}]",
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "authors": "",
                    "publisher": "Haut Conseil pour le Climat",
                    "department": "",
                    "journal": "",
                    "url": resource_url,
                    "pdf_url": None,
                    "keywords": "",
                    "category": category,
                    "doi": "",
                    "original_filename": original_filename,
                    "metadata": json.dumps({
                        "posted_date": published_date,
                        "originalFilename": original_filename,
                        "credits": credits,
                        "download_url": download_url,
                        "image_url": image_url,
                        "modal_id": modal_id,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]!r}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {item.get('modal_id', '?')} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
