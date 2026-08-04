# -*- coding: utf-8 -*-
"""Crawler for BAM Paper of the Month.

Site: https://www.bam.de/en/news/papers-of-the-month
(redesigned TYPO3/Solr site as of 2026; old Navigation/EN/... URL 301s here)

Listing pages use a TYPO3 Solr results pager that requires a per-page cHash
security token (`?tx_news_pi1[currentPage]=N&cHash=...`) which can't be
predicted from a page number alone, so pages are walked by following the
actual "next" link (`a.page-link.page-forward`) found in each page's HTML.
Each detail page has:
  - <h1 class="headerMain"> with the title (no category subtitle anymore)
  - <time datetime="YYYY-MM-DD"> for the published date
  - <div class="ce-bodytext"> (inside the news_newsdetail frame) with
    abstract <p> paragraphs and a final bibliographic-reference <p>
  - <a class="external-link"> for the journal article and opus4 repository
    links (inside a "Further Links" teaser)
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

    # Old URL 301-redirects here; kept up to date to skip the extra hop.
    _LIST_URL = "https://www.bam.de/en/news/papers-of-the-month"
    _PAGE_CAP = 200
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
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
        # Current site (TYPO3 Solr search results): li.search-result h3.results-topic a
        # Legacy markup (pre-redesign) kept as a fallback: h3.list-title a
        anchors = soup.select("li.search-result h3.results-topic a[href]")
        if not anchors:
            anchors = soup.select("h3.results-topic a[href]")
        if not anchors:
            anchors = [
                a
                for h3 in soup.find_all("h3", class_="list-title")
                for a in [h3.find("a", href=True)]
                if a
            ]
        urls = []
        for a in anchors:
            href = a["href"].strip()
            if not href.startswith("http"):
                href = self.base_url + "/" + href.lstrip("/")
            urls.append(href)
        return urls

    def _next_page_url(self, html: str) -> str | None:
        """Return the absolute URL of the next results page, if any.

        The site's TYPO3 pager requires a per-page `cHash` security token
        that can't be predicted/constructed — it must be read off the
        "next" link actually present in the current page's HTML.
        """
        soup = self._make_soup(html)
        if not soup:
            return None
        nxt = soup.select_one("a.page-link.page-forward[href]") or soup.select_one(
            "a.page-forward[href]"
        )
        if not nxt:
            return None
        href = nxt["href"].strip()
        if not href:
            return None
        if not href.startswith("http"):
            href = self.base_url + "/" + href.lstrip("/")
        return href

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
        # Current markup: <h1 class="headerMain"><span>Title</span></h1>
        # Legacy markup (pre-redesign): <h1 id="Titel">...<span class="subtitle">
        h1 = soup.select_one("h1.headerMain") or soup.find("h1", id="Titel") or soup.find("h1")
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
        # Current markup: <time class="date-text" datetime="2026-08-02">...</time>
        published_date = ""
        date_tag = soup.select_one("time[datetime]") or soup.find("span", class_="date")
        if date_tag:
            raw_datetime = (date_tag.get("datetime") or "").strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_datetime):
                published_date = raw_datetime
            else:
                raw_date = date_tag.get_text(strip=True)  # e.g. "01/05/2026"
                try:
                    published_date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    published_date = raw_date

        # --- Abstract and bibliographic reference ---
        abstract = ""
        authors = ""
        journal = ""
        ref_title = ""

        # Current markup: the article body lives in a <div class="ce-bodytext">
        # inside the "frame-type-news_newsdetail" frame (there are other,
        # unrelated ce-bodytext divs elsewhere on the page, e.g. footer/teasers,
        # so scope the search to that frame). Legacy markup used
        # <div class="singleview"> directly.
        body_div = None
        newsdetail_frame = soup.find(
            "div", class_=lambda c: c and "frame-type-news_newsdetail" in c
        )
        if newsdetail_frame:
            body_div = newsdetail_frame.find("div", class_="ce-bodytext")
        if body_div is None:
            body_div = soup.find("div", class_="singleview")
        if body_div is None:
            body_div = soup.find("div", class_="ce-bodytext")

        if body_div:
            content_paras = []
            for p in body_div.find_all("p"):
                if p.find_parent("figcaption") or p.find_parent("figure"):
                    continue
                cls = p.get("class") or []
                if any(c in cls for c in ("navToTop", "description", "source")):
                    continue
                text = self._clean(p)
                if len(text) > 40:
                    content_paras.append((p, text))

            # The bibliographic-reference paragraph is the LAST content
            # paragraph and is recognizable by >=2 <br> line breaks
            # (title<br/>authors<br/>journal, year). Every other paragraph
            # is genuine abstract text.
            ref_p = None
            if content_paras:
                last_p, _ = content_paras[-1]
                if len(last_p.find_all("br")) >= 2:
                    ref_p = last_p
                    content_paras = content_paras[:-1]

            abstract = " ".join(text for _, text in content_paras)

            if ref_p is not None:
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

        # Current markup: <a class="external-link" href="..."> inside a
        # "Further Links" teaser article. Legacy markup used
        # <div class="further-information">.
        link_candidates = soup.select("a.external-link[href]")
        further = soup.find("div", class_="further-information")
        if not link_candidates and further:
            link_candidates = further.find_all("a", href=True)

        for a_tag in link_candidates:
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

        # The site's TYPO3 pager requires a per-page cHash token that can't
        # be constructed from a page number alone, so pages are walked by
        # following the "next" link found in each page's own HTML rather
        # than by templating a page-number query param.
        list_url: str | None = self._LIST_URL

        for page_num in range(1, self._PAGE_CAP + 1):
            if time.time() - start_time > self._MAX_SECONDS:
                print(f"[bam-de-navigation] 25-min budget reached at page {page_num}, stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page_num == self._PAGE_CAP:
                print(f"[bam-de-navigation] safety cap {self._PAGE_CAP} pages reached, stopping")
                break

            if not list_url:
                print(f"[bam-de-navigation] no page {page_num} link found, stopping")
                break

            if page_num == 1 or page_num % 10 == 0:
                print(f"[bam-de-navigation] page {page_num}: saved {saved}/{limit_str}")

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

            list_url = self._next_page_url(html)
            if not list_url:
                print(
                    f"[bam-de-navigation] no next-page link found, "
                    f"done after {page_num} page(s)"
                )
                break

        print(f"[bam-de-navigation] crawl complete: {saved} saved")
        return saved
