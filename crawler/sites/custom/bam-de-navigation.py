# -*- coding: utf-8 -*-
"""Crawler for BAM Paper of the Month.

Site: https://www.bam.de/Navigation/EN/News/Paper-of-the-Month/paper-of-the-month.html

Listing pages use BAM CMS pagination: ?gtp=53294_list%253D{n}
Each detail page has:
  - <h1 id="Titel"> with <span class="subtitle"> for category
  - <span class="date">DD/MM/YYYY</span>
  - <div class="singleview"> with abstract <p> and bibliographic reference <p>
  - <div class="further-information"> with journal and opus4 repository links
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler


class BamDeNavigationCrawler(BaseCrawler):
    site_id = "bam-de-navigation"
    site_name = "Custom: bam-de-navigation"
    base_url = "https://www.bam.de"

    _LIST_URL = (
        "https://www.bam.de/Navigation/EN/News/Paper-of-the-Month/paper-of-the-month.html"
    )
    _PAGE_PARAM = "gtp=53294_list%253D"  # appended as ?{_PAGE_PARAM}{n}
    _PAGE_CAP = 200
    _MAX_SECONDS = 25 * 60
    _MIN_ABSTRACT = 100  # chars; items below this threshold are skipped

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch URL via curl with retry/backoff. Returns decoded text or None."""
        for attempt in range(retries):
            if attempt > 0:
                wait = 3 ** attempt  # 3s then 9s
                print(f"[bam-de-navigation] retry {attempt} in {wait}s: {url}")
                time.sleep(wait)
            try:
                r = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "-L",
                        "--max-time", "30",
                        "-A", self.USER_AGENT,
                        url,
                    ],
                    capture_output=True,
                    timeout=45,
                )
                if r.returncode == 0 and r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[bam-de-navigation] curl error (attempt {attempt + 1}): {exc}")
        return None

    # ------------------------------------------------------------------
    # BeautifulSoup helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean(tag) -> str:
        if tag is None:
            return ""
        return re.sub(r"\s+", " ", tag.get_text(separator=" ", strip=True)).strip()

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[str]:
        """Return absolute detail-page URLs found on a listing page."""
        soup = self._make_soup(html)
        if not soup:
            return []
        urls = []
        for h3 in soup.find_all("h3", class_="list-title"):
            a = h3.find("a", href=True)
            if not a:
                continue
            href = a["href"].strip()
            if not href.startswith("http"):
                href = self.base_url + "/" + href.lstrip("/")
            urls.append(href)
        return urls

    def _has_next_page(self, html: str, next_num: int) -> bool:
        return any(
            p in html
            for p in (
                f"{self._PAGE_PARAM}{next_num}",
                f"gtp=53294_list%3D{next_num}",
                f"gtp=53294_list={next_num}",
            )
        )

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, url: str, html: str) -> dict | None:
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[bam-de-navigation] soup error for {url}: {exc}")
            return None
        if not soup:
            return None

        # --- Title and category ---
        h1 = soup.find("h1", id="Titel") or soup.find("h1")
        if not h1:
            return None

        subtitle_tag = h1.find("span", class_="subtitle")
        category = ""
        if subtitle_tag:
            raw_sub = self._clean(subtitle_tag)
            m = re.search(r":\s*(.+)$", raw_sub)
            if m:
                category = m.group(1).strip()
            subtitle_tag.decompose()

        title = self._clean(h1)
        if not title:
            return None

        # --- Published date ---
        published_date = ""
        date_span = soup.find("span", class_="date")
        if date_span:
            raw_date = date_span.get_text(strip=True)  # e.g. "01/05/2026"
            try:
                published_date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                published_date = raw_date

        # --- Abstract and bibliographic reference ---
        abstract = ""
        authors = ""
        journal = ""
        ref_title = ""

        singleview = soup.find("div", class_="singleview")
        if singleview:
            content_paras = []
            for p in singleview.find_all("p"):
                if p.find_parent("figcaption") or p.find_parent("figure"):
                    continue
                cls = p.get("class") or []
                if any(c in cls for c in ("navToTop", "description", "source")):
                    continue
                text = self._clean(p)
                if len(text) > 40:
                    content_paras.append((p, text))

            if content_paras:
                _, abstract = content_paras[0]

            # Second paragraph: <em>title</em><br/>Authors<br/>Journal, Year
            if len(content_paras) >= 2:
                ref_p, _ = content_paras[1]
                em = ref_p.find("em")
                if em:
                    ref_title = self._clean(em)
                raw_lines = [
                    l.strip()
                    for l in ref_p.get_text(separator="\n", strip=True).split("\n")
                    if l.strip()
                ]
                # raw_lines[0] = paper title, [1] = authors, [2] = "Journal, Year"
                if len(raw_lines) >= 3:
                    authors = raw_lines[1]
                    jy = raw_lines[2]
                    jm = re.match(r"^(.+?),\s*\d{4}$", jy)
                    journal = jm.group(1).strip() if jm else jy
                elif len(raw_lines) == 2:
                    authors = raw_lines[1]

        # --- Further links ---
        article_url: str | None = None
        opus_url: str | None = None

        further = soup.find("div", class_="further-information")
        if further:
            for a_tag in further.find_all("a", href=True):
                href = a_tag["href"]
                if "opus4.kobv.de" in href:
                    opus_url = href
                elif href.startswith("http") and "bam.de" not in href and article_url is None:
                    article_url = href

        # --- DOI from article URL ---
        doi: str | None = None
        if article_url:
            m = re.search(r"doi\.org/(.+)", article_url)
            if m:
                doi = m.group(1).rstrip(".,")

        # --- opus4 docId → post_number ---
        opus_doc_id: str | None = None
        if opus_url:
            m = re.search(r"docId[=/](\d+)", opus_url)
            if m:
                opus_doc_id = m.group(1)

        slug = url.rstrip("/").split("/")[-1].replace(".html", "")
        post_number = opus_doc_id if opus_doc_id else slug

        # --- PDF URL (opus4 canonical download pattern) ---
        pdf_url: str | None = None
        if opus_doc_id:
            pdf_url = (
                f"https://opus4.kobv.de/opus4-bam/files/{opus_doc_id}/{opus_doc_id}.pdf"
            )

        original_filename: str | None = None
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1]

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "authors": authors,
            "publisher": "BAM - Federal Institute for Materials Research and Testing",
            "journal": journal,
            "keywords": "",
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "article_url": article_url,
                    "opus_url": opus_url,
                    "ref_title": ref_title,
                    "posted_date": published_date,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        for page_num in range(1, self._PAGE_CAP + 1):
            if time.time() - start_time > self._MAX_SECONDS:
                print(f"[bam-de-navigation] 25-min budget reached at page {page_num}, stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page_num == self._PAGE_CAP:
                print(f"[bam-de-navigation] safety cap {self._PAGE_CAP} pages reached, stopping")
                break

            if page_num == 1 or page_num % 10 == 0:
                print(f"[bam-de-navigation] page {page_num}: saved {saved}/{limit_str}")

            list_url = (
                self._LIST_URL
                if page_num == 1
                else f"{self._LIST_URL}?{self._PAGE_PARAM}{page_num}"
            )

            html = self._curl_get(list_url)
            if not html:
                print(f"[bam-de-navigation] failed to fetch list page {page_num}, stopping")
                break

            detail_links = self._parse_list_page(html)
            if not detail_links:
                print(f"[bam-de-navigation] no items on page {page_num}, done")
                break

            new_links = [lnk for lnk in detail_links if lnk not in seen_urls]
            if not new_links:
                print(f"[bam-de-navigation] all items on page {page_num} already seen, stopping")
                break

            for url in new_links:
                if limit is not None and saved >= limit:
                    break
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(url)
                    if not detail_html:
                        print(f"[bam-de-navigation] fetch failed: {url}")
                        continue

                    paper = self._parse_detail(url, detail_html)
                    if paper is None:
                        print(f"[bam-de-navigation] parse failed: {url}")
                        continue

                    abstract_len = len(paper.get("abstract") or "")
                    if abstract_len < self._MIN_ABSTRACT:
                        print(
                            f"[bam-de-navigation] abstract too short ({abstract_len} chars), "
                            f"skipping: {url}"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[bam-de-navigation] item failed ({url}): {exc}")
                    continue

            if not self._has_next_page(html, page_num + 1):
                print(
                    f"[bam-de-navigation] no page {page_num + 1} link found, "
                    f"done after {page_num} page(s)"
                )
                break

        print(f"[bam-de-navigation] crawl complete: {saved} saved")
        return saved
