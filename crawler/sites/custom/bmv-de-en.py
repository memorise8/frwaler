# -*- coding: utf-8 -*-
"""BMV (Federal Ministry for Digital and Transport) English publications crawler.

Target: https://www.bmv.de/EN/Services/Publications/publications.html
Strategy: HTML list pages (paginated) → per-item PDF download → pdftotext abstract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.bmv.de"
_LIST_URL = f"{_BASE}/EN/Services/Publications/publications.html"
_BACKOFF = (1, 3, 9)


def _make_soup(html: str):
    """Parse HTML with best-available parser; returns BeautifulSoup or None."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class BmvDeEnCrawler(BaseCrawler):
    """Crawler for BMV (German Federal Ministry of Transport) English publications."""

    site_id = "bmv-de-en"
    site_name = "Custom: bmv-de-en"
    base_url = "https://www.bmv.de"

    _MAX_PAGES = 200
    _MAX_RUNTIME_SECS = 25 * 60
    _MIN_ABSTRACT_CHARS = 50

    # ------------------------------------------------------------------ #
    # Network helpers
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with TLS workaround; returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                try:
                    text = result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    text = result.stdout.decode("latin-1", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = _BACKOFF[attempt]
                    print(f"[bmv-de-en] empty response (attempt {attempt+1}/3), retry in {wait}s")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    wait = _BACKOFF[attempt]
                    print(f"[bmv-de-en] timeout (attempt {attempt+1}/3), retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = _BACKOFF[attempt]
                    print(f"[bmv-de-en] curl error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[bmv-de-en] curl failed after 3 attempts: {exc}")
        return None

    def _curl_pdf(self, url: str) -> bytes | None:
        """Download PDF bytes; returns bytes or None on failure."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=65)
                if result.stdout and len(result.stdout) > 500:
                    return result.stdout
                if attempt < 2:
                    wait = _BACKOFF[attempt]
                    print(f"[bmv-de-en] short/empty PDF (attempt {attempt+1}/3), retry in {wait}s")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    wait = _BACKOFF[attempt]
                    print(f"[bmv-de-en] PDF timeout (attempt {attempt+1}/3), retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = _BACKOFF[attempt]
                    print(f"[bmv-de-en] PDF curl error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[bmv-de-en] PDF download failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # PDF text extraction
    # ------------------------------------------------------------------ #

    def _extract_pdf_text(self, pdf_url: str) -> str:
        """Download PDF and extract text via pdftotext (first 10 pages).

        Returns empty string on any failure.
        """
        pdf_bytes = self._curl_pdf(pdf_url)
        if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
            return ""

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                fh.write(pdf_bytes)
                tmp_path = fh.name

            result = subprocess.run(
                ["pdftotext", "-l", "10", tmp_path, "-"],
                capture_output=True,
                timeout=30,
            )
            try:
                text = result.stdout.decode("utf-8", errors="replace")
            except Exception:
                text = ""

            text = re.sub(r"\f", "\n", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()

        except subprocess.TimeoutExpired:
            print(f"[bmv-de-en] pdftotext timeout for {pdf_url}")
            return ""
        except Exception as exc:
            print(f"[bmv-de-en] PDF extraction error ({pdf_url}): {exc}")
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # List page helpers
    # ------------------------------------------------------------------ #

    def _list_page_url(self, page: int) -> str:
        if page == 1:
            return _LIST_URL
        return f"{_LIST_URL}?gtp=81202_liste%3D{page}"

    def _parse_list_page(self, html: str) -> list[dict]:
        """Parse publication cards from a list HTML page."""
        soup = _make_soup(html)
        if soup is not None:
            return self._parse_cards_bs4(soup)
        return self._parse_cards_regex(html)

    def _parse_cards_bs4(self, soup) -> list[dict]:
        items: list[dict] = []
        for li in soup.find_all("li", class_=re.compile(r"card-list-item")):
            try:
                cls_str = " ".join(li.get("class", []))
                pn_m = re.search(r"\b(\d{5,8})\b", cls_str)
                post_number = pn_m.group(1) if pn_m else None

                link_tag = li.find("a", class_="card-link")
                if link_tag:
                    strong = link_tag.find("strong")
                    title = strong.get_text(strip=True) if strong else link_tag.get_text(strip=True)
                else:
                    strong = li.find("strong")
                    title = strong.get_text(strip=True) if strong else ""

                if not title:
                    continue

                detail_url = ""
                if link_tag and link_tag.get("href"):
                    href = link_tag["href"]
                    detail_url = href if href.startswith("http") else urljoin(_BASE + "/", href)

                pdf_url = ""
                pdf_tag = li.find("a", class_="card-btn")
                if pdf_tag and pdf_tag.get("href") and ".pdf" in pdf_tag["href"].lower():
                    href = pdf_tag["href"]
                    pdf_url = href if href.startswith("http") else urljoin(_BASE, href)

                date_p = li.find("p", class_="card-date")
                if date_p:
                    raw = date_p.get_text(strip=True)
                    date = raw.replace(".", "-") if re.match(r"\d{4}\.\d{2}\.\d{2}$", raw) else raw
                else:
                    date = ""

                topline = li.find("p", class_="card-topline")
                if topline:
                    for sr in topline.find_all(class_="sr-only"):
                        sr.decompose()
                    pub_type = topline.get_text(strip=True)
                else:
                    pub_type = ""

                fmt_p = li.find("p", class_="card-format")
                if fmt_p:
                    for sr in fmt_p.find_all(class_="sr-only"):
                        sr.decompose()
                    topic = fmt_p.get_text(strip=True)
                else:
                    topic = ""

                items.append({
                    "post_number": post_number,
                    "title": title,
                    "detail_url": detail_url,
                    "pdf_url": pdf_url,
                    "date": date,
                    "pub_type": pub_type,
                    "topic": topic,
                })
            except Exception as exc:
                print(f"[bmv-de-en] card parse error (bs4): {exc}")
                continue
        return items

    def _parse_cards_regex(self, html: str) -> list[dict]:
        """Regex fallback card parser."""
        items: list[dict] = []
        for cls_str, card_html in re.findall(
            r'<li class="(card card-list-item[^"]*)">(.*?)</li>', html, re.DOTALL
        ):
            try:
                pn_m = re.search(r"\b(\d{5,8})\b", cls_str)
                post_number = pn_m.group(1) if pn_m else None

                title_m = re.search(
                    r'class="card-link"[^>]*>.*?<strong>(.*?)</strong>', card_html, re.DOTALL
                )
                if not title_m:
                    title_m = re.search(r"<strong>([^<]+)</strong>", card_html)
                if not title_m:
                    continue
                title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()

                detail_m = re.search(
                    r'class="card-link" href="(SharedDocs/EN/publications/[^"]+\.html)"', card_html
                )
                detail_url = urljoin(_BASE + "/", detail_m.group(1)) if detail_m else ""

                pdf_m = re.search(
                    r'href="(/SharedDocs/EN/publications/[^"]+\.pdf[^"]*)"', card_html
                )
                pdf_url = _BASE + pdf_m.group(1) if pdf_m else ""

                date_m = re.search(r'class="card-date">(\d{4}\.\d{2}\.\d{2})', card_html)
                date = date_m.group(1).replace(".", "-") if date_m else ""

                items.append({
                    "post_number": post_number,
                    "title": title,
                    "detail_url": detail_url,
                    "pdf_url": pdf_url,
                    "date": date,
                    "pub_type": "",
                    "topic": "",
                })
            except Exception as exc:
                print(f"[bmv-de-en] card parse error (regex): {exc}")
                continue
        return items

    # ------------------------------------------------------------------ #
    # Main crawl
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Crawl BMV publications, extracting abstracts from PDFs.

        Walks paginated list pages until ``limit`` items are saved, no new
        items appear, or the 200-page / 25-minute safety caps are hit.
        """
        saved = 0
        seen_urls: set[str] = set()
        page = 1
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if time.time() - start_time > self._MAX_RUNTIME_SECS:
                    print(f"[bmv-de-en] 25-minute wall-clock limit reached at page {page}. Stopping.")
                    break

                if limit is not None and saved >= limit:
                    break

                if page > self._MAX_PAGES:
                    print(f"[bmv-de-en] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break

                if page % 10 == 0:
                    print(f"[bmv-de-en] page {page}: saved {saved}/{limit_str}")

                raw = self._curl_get(self._list_page_url(page))
                if not raw:
                    print(f"[bmv-de-en] Failed to fetch list page {page}. Stopping.")
                    break

                items = self._parse_list_page(raw)
                if not items:
                    print(f"[bmv-de-en] No items on page {page}. Done.")
                    break

                new_items = [
                    it for it in items
                    if it.get("detail_url") and it["detail_url"] not in seen_urls
                ]
                if not new_items:
                    print(f"[bmv-de-en] All items on page {page} already seen. Done.")
                    break

                for it in items:
                    if it.get("detail_url"):
                        seen_urls.add(it["detail_url"])

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break

                    try:
                        title = (item.get("title") or "").strip()
                        if not title:
                            continue

                        pdf_url = item.get("pdf_url") or ""
                        detail_url = item.get("detail_url") or ""

                        original_filename: str | None = None
                        if pdf_url:
                            fn = pdf_url.split("?")[0].rstrip("/").split("/")[-1]
                            if fn.lower().endswith(".pdf"):
                                original_filename = fn

                        abstract = ""
                        if pdf_url:
                            time.sleep(self._delay)
                            abstract = self._extract_pdf_text(pdf_url)

                        if len(abstract) < self._MIN_ABSTRACT_CHARS:
                            print(
                                f"[bmv-de-en] Abstract too short ({len(abstract)} chars) "
                                f"for '{title[:50]}', skipping"
                            )
                            continue

                        pub_date = item.get("date") or ""
                        post_number = item.get("post_number")

                        if post_number:
                            external_id = post_number
                        elif detail_url:
                            slug = detail_url.rstrip("/").split("/")[-1].replace(".html", "")
                            external_id = slug
                        else:
                            external_id = re.sub(r"\W+", "-", title.lower())[:80]

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": post_number,
                            "title": title,
                            "abstract": abstract[:8000],
                            "category": item.get("topic") or "",
                            "keywords": "",
                            "published_date": pub_date,
                            "listed_date": pub_date,
                            "url": detail_url,
                            "pdf_url": pdf_url,
                            "doi": "",
                            "department": "",
                            "publisher": "Federal Ministry for Digital and Transport (BMV)",
                            "authors": "",
                            "journal": "",
                            "original_filename": original_filename,
                            "metadata": json.dumps({
                                "posted_date": pub_date,
                                "originalFilename": original_filename,
                                "pub_type": item.get("pub_type") or "",
                                "topic": item.get("topic") or "",
                                "post_number": post_number,
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[bmv-de-en] saved {saved}/{limit_str}: {title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[bmv-de-en] item '{(item.get('title') or '')[:40]}' failed: {exc}")
                        continue

                page += 1
                time.sleep(0.3)

        except KeyboardInterrupt:
            print(f"[bmv-de-en] Interrupted. Saved {saved} so far.")

        print(f"[bmv-de-en] Done. Total saved: {saved}")
        return saved
