# -*- coding: utf-8 -*-
"""UFM.dk Publikationer crawler — Uddannelses- og Forskningsministeriet."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Absolute import — works both when loaded via spec_from_file_location and normally.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _bs4_parse(html: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("All BeautifulSoup parsers failed")


def _strip_tags(html_str: str) -> str:
    """Remove HTML tags, decode basic entities, normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Network helper (curl-based with retry / exponential back-off)
# ---------------------------------------------------------------------------

def _curl_get(url: str, site_id: str = "ufm-dk-publikationer", retries: int = 3) -> str | None:
    """GET via curl; returns decoded text or None after exhausting retries."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30", "-L",
        "-H", "Accept-Language: da,en-US;q=0.9,en;q=0.8",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            print(f"[{site_id}] curl empty response (attempt {attempt + 1}/{retries}) for {url}")
        except Exception as exc:
            print(f"[{site_id}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class UfmDkPublikationerCrawler(BaseCrawler):
    """Crawler for UFM.dk Publikationer — Danish Ministry of Higher Education."""

    site_id = "ufm-dk-publikationer"
    site_name = "Custom: ufm-dk-publikationer"
    base_url = "https://ufm.dk"

    _LIST_BASE = "https://ufm.dk/publikationer"

    # ------------------------------------------------------------------
    # Internal fetch (requests session with curl fallback)
    # ------------------------------------------------------------------

    def _fetch(self, url: str) -> str | None:
        """Fetch URL; tries requests session first, then curl, with retries."""
        for attempt in range(3):
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:
                print(f"[{self.site_id}] requests error (attempt {attempt + 1}/3) for {url}: {exc}")
                if attempt < 2:
                    time.sleep([1, 3, 9][attempt])
        # curl fallback
        return _curl_get(url, self.site_id)

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[str]:
        """Return absolute publication URLs from a list/archive page."""
        try:
            soup = _bs4_parse(html)
        except Exception as exc:
            print(f"[{self.site_id}] list-page BeautifulSoup parse failed: {exc}")
            return []

        urls: list[str] = []
        for a in soup.find_all("a", class_=lambda c: c and "results-item" in c):
            href = a.get("href", "")
            if not href or "/publikationer/" not in href:
                continue
            abs_url = href if href.startswith("http") else urljoin(self.base_url, href)
            # Only keep publication detail pages (not the archive root)
            path = urlparse(abs_url).path.strip("/")
            if path == "publikationer":
                continue
            urls.append(abs_url)
        return urls

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, url: str, html: str) -> dict | None:
        """Parse a detail page and return a paper dict, or None on fatal error."""
        try:
            soup = _bs4_parse(html)
        except Exception as exc:
            print(f"[{self.site_id}] detail BeautifulSoup parse failed for {url}: {exc}")
            return None

        # post_number — data-nodeid on <body> is a numeric Umbraco node ID
        body_tag = soup.find("body")
        node_id = body_tag.get("data-nodeid") if body_tag else None
        post_number = str(node_id).strip() if node_id else None

        # title
        h1 = soup.find("h1", class_="area-content__headline")
        title = h1.get_text(separator=" ", strip=True) if h1 else ""
        if not title:
            og = soup.find("meta", property="og:title")
            title = og.get("content", "").strip() if og else ""

        # published_date — ISO from <time datetime="...">
        time_tag = soup.find("time", class_="area-content__date-time")
        published_date = time_tag.get("datetime", "").strip() if time_tag else ""

        # intro / short summary (often the teaser sentence)
        intro_tag = soup.find("p", class_="area-content__intro")
        intro_text = intro_tag.get_text(separator=" ", strip=True) if intro_tag else ""

        # main body text (contains publisher, ISBN, page count, description)
        body_div = soup.find("div", class_="area-content__text")
        body_text = ""
        if body_div:
            body_text = _strip_tags(str(body_div))

        # abstract = intro + body combined (both may carry unique content)
        abstract_parts = [p for p in (intro_text, body_text) if p]
        abstract = "\n\n".join(abstract_parts)

        # publisher — extract "Udgiver: ..." from body text
        publisher = ""
        pub_m = re.search(r"Udgiver:\s*([^\n\r<|]+)", body_text)
        if pub_m:
            publisher = pub_m.group(1).strip().rstrip(".")

        # PDF URL — prefer <a class="area-content__download-link">
        pdf_url = None
        original_filename = None
        dl_tag = soup.find("a", class_="area-content__download-link")
        if dl_tag and dl_tag.get("href", ""):
            href = dl_tag["href"]
            if ".pdf" in href.lower() or "/media/" in href:
                pdf_url = href if href.startswith("http") else urljoin(self.base_url, href)
        # fallback: any /media/...pdf link
        if not pdf_url:
            for a in soup.find_all("a", href=True):
                if ".pdf" in a["href"].lower():
                    pdf_url = (a["href"] if a["href"].startswith("http")
                               else urljoin(self.base_url, a["href"]))
                    break

        if pdf_url:
            fname = urlparse(pdf_url).path.rsplit("/", 1)[-1].split("?")[0]
            if fname:
                original_filename = fname

        # external_id — URL slug (last non-empty path segment)
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        external_id = path_parts[-1] if path_parts else url

        # metadata blob
        isbn_m = re.search(r"ISBN[:\s]+([0-9\-X ]+)", body_text, re.I)
        year_m = re.search(r"Publikations[aå]r:\s*(\d{4})", body_text, re.I)
        pages_m = re.search(r"Sideantal:\s*(\d+)", body_text, re.I)
        pubdate_raw_m = re.search(r"Publiceringsdato:\s*([^\n\r]+)", body_text)

        metadata: dict = {
            "posted_date": published_date,
            "node_id": node_id,
        }
        if isbn_m:
            metadata["isbn"] = isbn_m.group(1).strip()
        if year_m:
            metadata["year"] = year_m.group(1)
        if pages_m:
            metadata["pages"] = pages_m.group(1)
        if pubdate_raw_m:
            metadata["pub_date_raw"] = pubdate_raw_m.group(1).strip()

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "posted_date": published_date,
            "publisher": publisher,
            "url": url,
            "meta_url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl publikationer list pages and their detail pages.

        Parameters
        ----------
        limit:
            Maximum number of papers to save. ``None`` means unlimited.
        """
        start_time = time.time()
        wall_budget_secs = 25 * 60  # 25 minutes hard cap

        saved = 0
        page = 1
        max_pages = 200
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            # Wall-clock safety check
            if time.time() - start_time > wall_budget_secs:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached, stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > max_pages:
                print(f"[{self.site_id}] Safety cap of {max_pages} pages reached, stopping.")
                break

            # Fetch list page
            list_url = f"{self._LIST_BASE}?pageNumber={page}"
            html = self._fetch(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping.")
                break

            item_urls = self._parse_list_page(html)
            if not item_urls:
                print(f"[{self.site_id}] Page {page}: no items found — end of pagination. Done.")
                break

            # URL deduplication (detects infinite pagination loops)
            new_urls = [u for u in item_urls if u not in seen_urls]
            if not new_urls:
                print(f"[{self.site_id}] Page {page}: all items already seen, stopping.")
                break
            seen_urls.update(item_urls)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # Fetch & parse each detail page
            for item_url in new_urls:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > wall_budget_secs:
                    print(f"[{self.site_id}] Wall-clock budget reached mid-page, stopping.")
                    break

                try:
                    time.sleep(self._delay)
                    detail_html = self._fetch(item_url)
                    if not detail_html:
                        print(f"[{self.site_id}] item {item_url}: fetch failed, skipping.")
                        continue

                    paper = self._parse_detail(item_url, detail_html)
                    if not paper:
                        print(f"[{self.site_id}] item {item_url}: parse returned None, skipping.")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_url}: "
                            f"abstract too short ({len(abstract)} chars), skipping."
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {paper.get('title', '')[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
