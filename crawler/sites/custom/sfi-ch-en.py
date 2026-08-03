# -*- coding: utf-8 -*-
"""Swiss Finance Institute (sfi.ch) English publications crawler.

Starting URL: https://www.sfi.ch/en/about-us/activity-report
Actual content: https://www.sfi.ch/en/publications (Working Papers + Academic Publications)

Each publication has a detail page with:
  - Title      in <h2 class="headline">
  - Metadata   in <span class="k"> / <span class="v"> pairs
  - Abstract   in <div class="mb-5"> containing <p> text
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception:
            continue
    raise RuntimeError("No working HTML parser available (tried html5lib, lxml, html.parser)")


class SFIChEnCrawler(BaseCrawler):
    site_id = "sfi-ch-en"
    site_name = "Custom: sfi-ch-en"
    base_url = "https://www.sfi.ch"

    START_URL = "https://www.sfi.ch/en/about-us/activity-report"
    _LIST_BASE = "https://www.sfi.ch/en/publications"
    # 383 = Working Papers, 384 = Academic Publications
    _CATS = [383, 384]

    MAX_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    MIN_ABSTRACT_CHARS = 50
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 35

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl(self, url: str, retries: int = 3) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--max-time", str(self.CURL_TIMEOUT),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 5
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if raw.strip():
                    return raw
                if attempt < retries - 1:
                    wait = self.BACKOFF_SECONDS[attempt]
                    print(f"[sfi-ch-en] empty response for {url}, retrying in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = self.BACKOFF_SECONDS[attempt]
                    print(f"[sfi-ch-en] curl error ({exc}), retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[sfi-ch-en] curl failed after {retries} attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # listing page parser
    # ------------------------------------------------------------------

    def _listing_urls(self, html: str) -> list[str]:
        """Extract publication detail-page URLs from a listing page."""
        try:
            soup = _make_soup(html)
            seen: set[str] = set()
            out: list[str] = []
            for a in soup.find_all("a", href=True):
                href: str = a["href"]
                if (
                    href.startswith("/en/publications/")
                    and len(href) > len("/en/publications/")
                    and "?" not in href
                    and "#" not in href
                ):
                    full = self.base_url + href
                    if full not in seen:
                        seen.add(full)
                        out.append(full)
            return out
        except Exception as exc:
            print(f"[sfi-ch-en] listing parse error: {exc}")
            # fallback: regex
            hrefs = re.findall(r'href="(/en/publications/[^"?#]+)"', html)
            seen2: set[str] = set()
            out2: list[str] = []
            for h in hrefs:
                full = self.base_url + h
                if full not in seen2:
                    seen2.add(full)
                    out2.append(full)
            return out2

    # ------------------------------------------------------------------
    # detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str) -> dict | None:
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[sfi-ch-en] soup construction error for {url}: {exc}")
            return None

        # title
        title_el = soup.find("h2", class_="headline") or soup.find("h1")
        title = title_el.get_text(strip=True) if title_el else ""

        # key-value metadata pairs
        meta: dict[str, str] = {}
        for k_el in soup.find_all("span", class_="k"):
            key = k_el.get_text(strip=True)
            v_el = k_el.find_next_sibling("span")
            if v_el:
                meta[key] = v_el.get_text(separator=" ", strip=True)

        # abstract: first div.mb-5 whose text is long enough
        abstract = ""
        for div in soup.find_all("div", class_="mb-5"):
            text = div.get_text(separator=" ", strip=True).replace("\xa0", " ")
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                abstract = text
                break

        authors_raw = meta.get("Authors", "")
        authors_list = [a.strip() for a in re.split(r",\s*", authors_raw) if a.strip()]

        doi_raw = meta.get("Link", "").strip()
        journal = meta.get("Journal", "").strip()
        date_str = meta.get("Date", "").strip()
        category = meta.get("Category", "").strip()
        volume = meta.get("Volume", "").strip()

        slug = url.rstrip("/").rsplit("/", 1)[-1]

        return {
            "external_id": slug,
            "title": title,
            "authors": json.dumps(authors_list, ensure_ascii=False),
            "abstract": abstract,
            "journal": journal,
            "published_date": date_str,
            "url": url,
            "pdf_url": None,
            "doi": doi_raw,
            "category": category,
            "keywords": json.dumps([]),
            "department": journal,
            "metadata": json.dumps(
                {
                    "journal": journal,
                    "volume": volume,
                    "category": category,
                    "doi": doi_raw,
                    "raw_meta": meta,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        t0 = time.time()
        saved = 0
        seen_urls: set[str] = set()
        lim = limit if limit is not None else float("inf")

        for cat in self._CATS:
            if saved >= lim:
                break

            page = 1
            pages_done = 0

            while True:
                # wall-clock budget
                if time.time() - t0 > self.MAX_WALL_SECONDS:
                    print(f"[sfi-ch-en] 25-min wall-clock budget exceeded, stopping.")
                    return saved

                # safety cap
                if pages_done >= self.MAX_PAGES:
                    print(
                        f"[sfi-ch-en] Safety cap of {self.MAX_PAGES} pages reached "
                        f"(cat={cat}), moving to next category."
                    )
                    break

                list_url = f"{self._LIST_BASE}?a=&c={cat}&q=&p={page}"

                try:
                    html = self._curl(list_url)
                except KeyboardInterrupt:
                    raise

                if not html:
                    print(
                        f"[sfi-ch-en] Failed to fetch listing page {page} (cat={cat}), "
                        "stopping category."
                    )
                    break

                item_urls = self._listing_urls(html)
                new_urls = [u for u in item_urls if u not in seen_urls]

                if not new_urls:
                    print(
                        f"[sfi-ch-en] No new items on page {page} (cat={cat}), "
                        "done with category."
                    )
                    break

                pages_done += 1
                if page % 10 == 0:
                    print(f"[sfi-ch-en] page {page} (cat={cat}): saved {saved}/{lim}")

                for url in new_urls:
                    if saved >= lim:
                        break
                    seen_urls.add(url)


                    time.sleep(self._delay)

                    if time.time() - t0 > self.MAX_WALL_SECONDS:
                        print(f"[sfi-ch-en] 25-min wall-clock budget hit, stopping.")
                        return saved

                    try:
                        detail_html = self._curl(url)
                        if not detail_html:
                            print(f"[sfi-ch-en] No response for {url}, skipping.")
                            continue

                        paper = self._parse_detail(detail_html, url)
                        if not paper:
                            print(f"[sfi-ch-en] Parse returned None for {url}, skipping.")
                            continue

                        if not paper.get("title"):
                            print(f"[sfi-ch-en] No title at {url}, skipping.")
                            continue

                        abstract = paper.get("abstract", "")
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[sfi-ch-en] Abstract too short "
                                f"({len(abstract)} chars) at {url}, skipping."
                            )
                            continue

                        paper["site_id"] = self.site_id
                        self._save_paper(paper)
                        saved += 1

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[sfi-ch-en] item {url} failed: {exc}")
                        continue

                if saved >= lim:
                    break

                page += 1

        return saved
