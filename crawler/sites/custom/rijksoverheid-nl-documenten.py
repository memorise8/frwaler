# -*- coding: utf-8 -*-
"""Rijksoverheid.nl – Ambtsberichten crawler.

Targets: https://www.rijksoverheid.nl/documenten?type=Ambtsbericht
280 documents (HTML pagination, 10 items/page, 28 pages).
Detail pages carry full abstract in <div class="intro">.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, unquote

# Absolute import — spec_from_file_location has no package context
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with exponential backoff; returns decoded text or None."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=40, check=False)
            raw = res.stdout or b""
            if raw.strip():
                return raw.decode("utf-8", errors="replace")
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(waits[attempt])
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class RijksoverheidNlDocumentenCrawler(BaseCrawler):
    site_id = "rijksoverheid-nl-documenten"
    site_name = "Custom: rijksoverheid-nl-documenten"
    base_url = "https://www.rijksoverheid.nl"

    _LIST_URL = "https://www.rijksoverheid.nl/documenten"
    _LIST_PARAMS_BASE = {
        "trefwoord": "",
        "startdatum": "",
        "einddatum": "",
        "onderdeel": "Alle ministeries",
        "type": "Ambtsbericht",
    }
    _MAX_PAGES = 200

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        params = dict(self._LIST_PARAMS_BASE)
        if page > 1:
            params["pagina"] = str(page)
        return f"{self._LIST_URL}?{urlencode(params)}"

    def _parse_list_page(self, html: str) -> list[dict]:
        soup = _make_soup(html)
        if not soup:
            return []
        results_ol = soup.find("ol", class_=re.compile(r"\bresults\b"))
        if not results_ol:
            return []
        items = []
        for li in results_ol.find_all("li", class_="results__item"):
            a = li.find("a")
            if not a:
                continue
            href = a.get("href", "")
            if not href.startswith("/documenten/"):
                continue

            title_tag = a.find("h3")
            title = title_tag.get_text(strip=True) if title_tag else ""

            # Date from "Ambtsbericht | DD-MM-YYYY" meta paragraph
            listed_date = ""
            for p in a.find_all("p", class_="meta"):
                text = p.get_text(strip=True)
                m = re.search(r"(\d{2})-(\d{2})-(\d{4})", text)
                if m:
                    listed_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
                    break

            # Date + slug from URL path: /documenten/<cat>/YYYY/MM/DD/slug
            m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)", href)
            url_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
            slug = m.group(4) if m else href.rstrip("/").split("/")[-1]

            items.append({
                "url": f"https://www.rijksoverheid.nl{href}",
                "title": title,
                "listed_date": listed_date or url_date,
                "url_date": url_date,
                "slug": slug,
            })
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail_page(self, url: str, html: str) -> dict | None:
        soup = _make_soup(html)
        if not soup:
            return None

        # Title
        h1 = soup.find("h1", class_="download") or soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

        # Full abstract from .intro div
        abstract = ""
        intro = soup.find("div", class_="intro")
        if intro:
            abstract = intro.get_text(separator=" ", strip=True)

        # Fallback: DCTERMS.description / description meta
        if len(abstract) < 50:
            for attr in ({"name": "DCTERMS.description"}, {"name": "description"}):
                meta = soup.find("meta", attr)
                if meta and meta.get("content"):
                    cand = meta["content"].strip()
                    if len(cand) >= 50:
                        abstract = cand
                        break

        # Published date from DCTERMS.issued
        published_date = ""
        meta_issued = soup.find("meta", {"name": "DCTERMS.issued"})
        if meta_issued:
            raw = meta_issued.get("content", "")
            m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
            if m:
                published_date = m.group(1)

        # Publisher — DCTERMS.creator is most reliable
        publisher = ""
        meta_creator = soup.find("meta", {"name": "DCTERMS.creator"})
        if meta_creator:
            publisher = meta_creator.get("content", "").strip()
        if not publisher:
            belongs = soup.find("div", class_=re.compile(r"belongsTo"))
            if belongs:
                links = [a.get_text(strip=True) for a in belongs.find_all("a")]
                publisher = ";".join(x for x in links if x)

        # PDF URL — first .download-chunk.pdf link
        pdf_url = None
        original_filename = None
        pdf_a = soup.find("a", class_=re.compile(r"\bpdf\b"))
        if pdf_a:
            href = pdf_a.get("href", "")
            if href:
                pdf_url = href if href.startswith("http") else f"https://www.rijksoverheid.nl{href}"
                original_filename = unquote(pdf_url.rstrip("/").split("/")[-1])

        # Category from DCTERMS.type
        category = ""
        meta_type = soup.find("meta", {"name": "DCTERMS.type"})
        if meta_type:
            category = meta_type.get("content", "").strip()

        # UUID from dataLayer JS block
        uuid_val = ""
        for script in soup.find_all("script"):
            text = script.string or ""
            m = re.search(r'"uuid"\s*:\s*"([^"]+)"', text)
            if m:
                uuid_val = m.group(1)
                break

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "publisher": publisher,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": category,
            "uuid": uuid_val,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        max_seconds = 25 * 60
        limit_str = str(limit) if limit is not None else "∞"
        page = 1

        while True:
            if time.time() - start_time > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached after page {page - 1}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # Fetch and parse list page
            html = _curl_get(self._list_url(page))
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # URL deduplication — stops silent paginators that loop back to page 1
            new_items = [it for it in items if it["url"] not in seen_urls]
            for it in new_items:
                seen_urls.add(it["url"])
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen (loop detected). Stopping.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(self._delay)

                    # Fetch detail page with retry
                    detail_html = None
                    for attempt in range(3):
                        detail_html = _curl_get(item["url"])
                        if detail_html:
                            break
                        wait = [1, 3, 9][attempt]
                        print(f"[{self.site_id}] Retry {attempt + 1}/3 for {item['url']}")
                        time.sleep(wait)

                    if not detail_html:
                        print(f"[{self.site_id}] Skipping (fetch failed): {item['url']}")
                        continue

                    detail = self._parse_detail_page(item["url"], detail_html)
                    if not detail:
                        print(f"[{self.site_id}] Skipping (parse failed): {item['url']}")
                        continue

                    title = detail["title"] or item["title"]
                    abstract = detail["abstract"] or ""

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping (abstract <50 chars): {item['url']}")
                        continue

                    # external_id = URL path without base
                    external_id = item["url"].replace("https://www.rijksoverheid.nl", "").strip("/")

                    # post_number: sortable date+slug string (no numeric ID on this site)
                    url_date = item.get("url_date", "")
                    slug = item.get("slug", "")
                    post_number = f"{url_date}/{slug}" if url_date else slug

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": detail["published_date"] or url_date,
                        "posted_date": item["listed_date"],
                        "authors": "",
                        "publisher": detail["publisher"],
                        "journal": "",
                        "url": item["url"],
                        "pdf_url": detail["pdf_url"],
                        "keywords": "",
                        "category": detail["category"],
                        "doi": "",
                        "original_filename": detail["original_filename"],
                        "metadata": json.dumps({
                            "posted_date": item["listed_date"],
                            "originalFilename": detail["original_filename"],
                            "uuid": detail["uuid"],
                            "url_date": url_date,
                            "slug": slug,
                            "category_raw": detail["category"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item {item.get('url', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
