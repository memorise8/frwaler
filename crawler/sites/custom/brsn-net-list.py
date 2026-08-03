# -*- coding: utf-8 -*-
"""BRSN (Belt and Road Studies Network) research list crawler.

Starting URL: https://www.brsn.net/list/research/index.html
Pagination:   index.html, index_2.html, ..., index_20.html (10 items/page, ~200 total)
Detail pages: hosted on brsn.net, chinaview.xhinst.net, or xhinst.net
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BS_PARSERS = ("html5lib", "lxml", "html.parser")

_NAV_FRAGMENTS = {
    "首页", "上一页", "下一页", "Copyright", "注册", "登录",
    "Home", "Previous", "Next", "Register", "Login",
}


def _make_soup(html):
    """Try BeautifulSoup parsers in fallback order. Returns soup or None."""
    for parser in _BS_PARSERS:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class BRSNResearchListCrawler(BaseCrawler):
    """Crawler for BRSN research list — https://www.brsn.net/list/research/"""

    site_id = "brsn-net-list"
    site_name = "Custom: brsn-net-list"
    base_url = "https://www.brsn.net"

    _LIST_URL_P1 = "https://www.brsn.net/list/research/index.html"
    _LIST_URL_PN = "https://www.brsn.net/list/research/index_{}.html"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    # ------------------------------------------------------------------
    # Network helper
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch URL via curl with TLS support and redirect following."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Referer: https://www.brsn.net/list/research/index.html",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw and len(raw) > 200:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = 3 ** attempt  # 1s, 3s
                    print(f"[brsn-net-list] Short/empty response ({url}), retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[brsn-net-list] curl error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[brsn-net-list] curl failed after {retries} attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # List page parser
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return list of {url, title, listed_date, post_number} dicts."""
        items = []
        parts = html.split('<div class="list-item">')
        for part in parts[1:]:
            try:
                # URL: href comes before class="img-l" in the anchor tag
                m = re.search(
                    r'href=["\']([^"\']+\.html)["\'][^>]+class=["\']img-l["\']', part
                )
                if not m:
                    # Fallback: first external .html href with target="_blank"
                    m = re.search(
                        r'href=["\']([^"\']+\.html)["\'][^>]+target=["\']_blank["\']', part
                    )
                if not m:
                    continue
                url = m.group(1).strip()
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://www.brsn.net" + url

                # Title: inside <h2 class="img-r-title">
                tm = re.search(
                    r'img-r-title[^>]*>.*?<a[^>]+>([^<]+)</a>', part, re.S
                )
                title = tm.group(1).strip() if tm else ""

                # Date shown in list
                dm = re.search(r'(\d{4}-\d{2}-\d{2})', part)
                listed_date = dm.group(1) if dm else ""

                # Post number from URL filename  e.g. /19447833_Title.html
                pn = re.search(r'/(\d{7,12})_', url)
                post_number = pn.group(1) if pn else None

                if url:
                    items.append({
                        "url": url,
                        "title": title,
                        "listed_date": listed_date,
                        "post_number": post_number,
                    })
            except Exception as exc:
                print(f"[brsn-net-list] list item parse error: {exc}")
        return items

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    @staticmethod
    def _meta(html, name):
        """Extract meta tag content by name or property (case-insensitive)."""
        for pat in [
            r'(?i)<meta\s[^>]*\bname=["\']' + re.escape(name) + r'["\'][^>]*\bcontent=["\']([^"\']*)["\']',
            r'(?i)<meta\s[^>]*\bcontent=["\']([^"\']*)["\'][^>]*\bname=["\']' + re.escape(name) + r'["\']',
            r'(?i)<meta\s[^>]*\bproperty=["\']' + re.escape(name) + r'["\'][^>]*\bcontent=["\']([^"\']*)["\']',
            r'(?i)<meta\s[^>]*\bcontent=["\']([^"\']*)["\'][^>]*\bproperty=["\']' + re.escape(name) + r'["\']',
        ]:
            m = re.search(pat, html)
            if m:
                return m.group(1).strip()
        return ""

    def _extract_abstract(self, html):
        """Extract abstract: meta description → BeautifulSoup paragraphs → regex fallback."""
        abstract = self._meta(html, "description") or self._meta(html, "og:description")

        if len(abstract) >= 100:
            return abstract

        # BeautifulSoup paragraph extraction
        try:
            soup = _make_soup(html)
            if soup:
                texts = []
                for p in soup.find_all("p"):
                    t = p.get_text(separator=" ", strip=True)
                    if len(t) > 60 and not any(frag in t for frag in _NAV_FRAGMENTS):
                        texts.append(t)
                if texts:
                    candidate = "\n\n".join(texts[:8])
                    if len(candidate) > len(abstract):
                        abstract = candidate
        except Exception:
            pass

        if len(abstract) >= 100:
            return abstract

        # Regex fallback
        clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.S | re.I)
        clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.S | re.I)
        blocks = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.S | re.I)
        texts = []
        for b in blocks:
            t = re.sub(r'<[^>]+>', '', b).strip()
            if len(t) > 60 and not any(frag in t for frag in _NAV_FRAGMENTS):
                texts.append(t)
        if texts:
            candidate = "\n\n".join(texts[:8])
            if len(candidate) > len(abstract):
                abstract = candidate

        return abstract

    def _parse_detail(self, html, fallback_url=""):
        """Parse a detail page; returns dict with all metadata fields."""
        mc = self._meta

        external_id = mc(html, "contentid")
        post_number = external_id if external_id else None

        # Title: og:title is cleanest; fall back to <title> stripping site suffix
        title = mc(html, "og:title")
        if not title:
            m = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
            if m:
                raw = m.group(1).strip()
                title = re.split(r'\s*[-–—|]\s*(?:CHINA|BRSN|一带一路|智库)', raw)[0].strip()

        # Published date (YYYY-MM-DD)
        pub_date = mc(html, "publishdate") or mc(html, "og:release_date")
        if pub_date:
            dm = re.match(r'(\d{4}-\d{2}-\d{2})', pub_date)
            pub_date = dm.group(1) if dm else ""

        # Authors: comma/Chinese-comma → semicolon
        authors_raw = mc(html, "author")
        authors = re.sub(r'[,，]\s*', '; ', authors_raw).strip('; ') if authors_raw else ""

        publisher = mc(html, "source")
        keywords = mc(html, "keywords")
        category = mc(html, "catalogs")

        abstract = self._extract_abstract(html)

        # PDF link if any
        pdf_m = re.search(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
        pdf_url = pdf_m.group(1) if pdf_m else None

        canon_url = mc(html, "og:url") or fallback_url

        metadata = {
            "contentid": external_id,
            "source": publisher,
            "posted_date": "",  # filled by crawl() with listed_date
            "filetype": mc(html, "filetype"),
            "publishedtype": mc(html, "publishedtype"),
            "pagetype": mc(html, "pagetype"),
            "catalogs": category,
            "og_type": mc(html, "og:type"),
            "og_url": mc(html, "og:url"),
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date,
            "authors": authors,
            "publisher": publisher,
            "keywords": keywords,
            "category": category,
            "pdf_url": pdf_url,
            "url": canon_url or fallback_url,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        page = 1
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._MAX_WALL_SECS:
                print(f"[brsn-net-list] Wall-clock budget ({self._MAX_WALL_SECS}s) reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[brsn-net-list] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            list_url = self._LIST_URL_P1 if page == 1 else self._LIST_URL_PN.format(page)

            html = self._curl_get(list_url)
            if not html:
                print(f"[brsn-net-list] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[brsn-net-list] No items on page {page}. Done.")
                break

            if page == 1 or page % 10 == 0:
                print(f"[brsn-net-list] page {page}: saved {saved}/{limit_str}")

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)

                    detail_html = self._curl_get(url)
                    if not detail_html:
                        print(f"[brsn-net-list] Failed to fetch: {url}")
                        continue

                    detail = self._parse_detail(detail_html, fallback_url=url)

                    listed_date = item.get("listed_date", "")
                    detail["metadata"]["posted_date"] = listed_date

                    title = detail.get("title") or item["title"]
                    abstract = detail.get("abstract", "")

                    if len(abstract.strip()) < 50:
                        print(f"[brsn-net-list] Skip (abstract {len(abstract)}c): {title[:60]}")
                        continue

                    external_id = detail.get("external_id") or item.get("post_number") or url
                    post_number = detail.get("post_number") or item.get("post_number")
                    published_date = detail.get("published_date") or listed_date
                    final_url = detail.get("url") or url

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "authors": detail.get("authors", ""),
                        "publisher": detail.get("publisher", ""),
                        "journal": "",
                        "url": final_url,
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords", ""),
                        "category": detail.get("category", ""),
                        "doi": "",
                        "department": "",
                        "original_filename": None,
                        "metadata": json.dumps(
                            detail.get("metadata", {}), ensure_ascii=False
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[brsn-net-list] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[brsn-net-list] item {url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[brsn-net-list] page {page}: all URLs already seen. Done.")
                break

            page += 1

        print(f"[brsn-net-list] Done. Total saved: {saved}")
        return saved
