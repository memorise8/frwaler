# -*- coding: utf-8 -*-
"""Crawler for economy-ni.gov.uk publications.

Starting URL: https://www.economy-ni.gov.uk/publications
Source 1: RSS feed  — 100 most-recent items, chronological order.
Source 2: Sitemaps  — /sitemaps/economy/sitemap.xml?page=2 and ?page=3
                      contain ~2356 older publication URLs.
Detail pages parsed with BeautifulSoup for abstract, node-id, PDFs, topics.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


def _make_soup(raw):
    """Parse HTML bytes with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


class EconomyNiGovUkPublicationsCrawler(BaseCrawler):
    site_id = "economy-ni-gov-uk-publications"
    site_name = "Custom: economy-ni-gov-uk-publications"
    base_url = "https://www.economy-ni.gov.uk"

    _RSS_URL = "https://www.economy-ni.gov.uk/publications/feed/economy"
    _SITEMAP_PAGES = [
        "https://www.economy-ni.gov.uk/sitemaps/economy/sitemap.xml?page=2",
        "https://www.economy-ni.gov.uk/sitemaps/economy/sitemap.xml?page=3",
    ]

    # ------------------------------------------------------------------ #
    # Low-level helpers                                                    #
    # ------------------------------------------------------------------ #

    def _curl(self, url, retries=3):
        """Fetch URL via curl with TLS workaround. Returns bytes or None."""
        for attempt in range(retries):
            try:
                r = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk",
                        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "--max-time", "30",
                        url,
                    ],
                    capture_output=True,
                    timeout=40,
                )
                if r.returncode == 0 and r.stdout:
                    return r.stdout
                print(f"[{self.site_id}] curl rc={r.returncode} for {url}")
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error for {url}: {exc}")
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1s, 3s, 9s
                print(f"[{self.site_id}] retry in {wait}s…")
                time.sleep(wait)
        return None

    @staticmethod
    def _parse_date(s):
        """Parse various date formats to ISO YYYY-MM-DD, or None."""
        if not s:
            return None
        # ISO-8601 (with optional time / timezone)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s.strip())
        if m:
            return m.group(1)
        # "8 May 2026", "29 April 2026"
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None

    @staticmethod
    def _unescape(s):
        """Minimal HTML entity decode for titles / descriptions."""
        for ent, rep in [
            ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
            ("&#039;", "'"), ("&apos;", "'"),
            ("&nbsp;", " "),
            ("&ndash;", "–"), ("&mdash;", "—"),
            ("&rsquo;", "’"), ("&lsquo;", "‘"),
            ("&rdquo;", "”"), ("&ldquo;", "“"),
        ]:
            s = s.replace(ent, rep)
        return s

    # ------------------------------------------------------------------ #
    # URL discovery                                                        #
    # ------------------------------------------------------------------ #

    def _rss_items(self):
        """Return list of item dicts from the RSS feed (up to 100 items)."""
        raw = self._curl(self._RSS_URL)
        if not raw:
            return []
        text = raw.decode("utf-8", errors="replace")
        results = []
        for m in re.finditer(r"<item>(.*?)</item>", text, re.DOTALL):
            blob = m.group(1)

            title_m = re.search(r"<title[^>]*>(.*?)</title>", blob, re.DOTALL)
            raw_title = title_m.group(1) if title_m else ""
            title = self._unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()

            link_m = re.search(r"<link>(.*?)</link>", blob, re.DOTALL)
            guid_m = re.search(r"<guid[^>]*>(.*?)</guid>", blob, re.DOTALL)
            url_src = link_m or guid_m
            url = url_src.group(1).strip() if url_src else ""
            if not url:
                continue

            desc_m = re.search(r"<description>(.*?)</description>", blob, re.DOTALL)
            desc = desc_m.group(1) if desc_m else ""
            desc = re.sub(r"<br\s*/?>", " ", desc, flags=re.I)
            desc = self._unescape(re.sub(r"<[^>]+>", "", desc))
            desc = re.sub(r"\s+", " ", desc).strip()

            pub_m = re.search(r"<pubDate>(.*?)</pubDate>", blob, re.DOTALL)
            pub_raw = pub_m.group(1).strip() if pub_m else ""

            creator_m = re.search(r"<dc:creator>(.*?)</dc:creator>", blob, re.DOTALL)
            creator = creator_m.group(1).strip() if creator_m else "Department for the Economy"

            results.append({
                "url": url,
                "title": title,
                "rss_desc": desc,
                "pub_raw": pub_raw,
                "creator": creator,
            })
        return results

    def _sitemap_urls(self):
        """Return list of publication URL dicts from sitemap pages 2 and 3."""
        items = []
        for sm_url in self._SITEMAP_PAGES:
            raw = self._curl(sm_url)
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            for url in re.findall(r"<loc>([^<]+)</loc>", text):
                url = url.strip()
                if "/publications/" in url:
                    items.append({
                        "url": url,
                        "title": "",
                        "rss_desc": "",
                        "pub_raw": "",
                        "creator": "Department for the Economy",
                    })
        return items

    # ------------------------------------------------------------------ #
    # Detail-page parsing                                                  #
    # ------------------------------------------------------------------ #

    def _build_abstract(self, soup, rss_desc=""):
        """Build abstract: page-summary + body text before Documents section."""
        parts = []

        # 1. page-summary div (primary)
        ps = soup.find(class_="page-summary")
        if ps:
            text = re.sub(r"\s+", " ", ps.get_text(" ", strip=True)).strip()
            if text:
                parts.append(text)

        # 2. Inner article-content div — direct children, stop at Documents h2
        inner_div = soup.find("div", class_="article-content")
        if inner_div and sum(len(p) for p in parts) < 300:
            body_parts = []
            stop = False
            for child in inner_div.children:
                if not hasattr(child, "name") or not child.name:
                    continue
                if child.name == "h2":
                    txt = child.get_text(strip=True).lower()
                    if re.search(r"document|contact|related|help", txt):
                        stop = True
                if stop:
                    break
                text = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
                # Skip bare file-link text
                if len(text) > 20 and not re.search(r"Adobe PDF|Help viewing", text, re.I):
                    body_parts.append(text)
            if body_parts:
                body_text = " ".join(body_parts)
                # Avoid duplicating page-summary content
                if body_text not in " ".join(parts):
                    parts.append(body_text)

        # 3. Fallback: RSS description
        if sum(len(p) for p in parts) < 50 and rss_desc:
            parts.append(rss_desc)

        result = " ".join(parts)
        return re.sub(r"\s+", " ", result).strip()

    def _fetch_detail(self, item):
        """Fetch and parse a publication detail page. Returns paper dict or None."""
        url = item["url"]
        raw = self._curl(url)
        if not raw:
            return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error for {url}: {exc}")
            return None
        if soup is None:
            return None

        # Title
        h1 = soup.find("h1", class_="page-title")
        title = h1.get_text(strip=True) if h1 else item.get("title", "")
        if not title:
            return None

        # Published date from <time datetime="…">
        dt_tag = soup.find("time")
        pub_date = None
        if dt_tag and dt_tag.get("datetime"):
            pub_date = self._parse_date(dt_tag["datetime"])
        if not pub_date:
            pub_date = self._parse_date(item.get("pub_raw", ""))

        # Listed date (RSS pubDate — when it appeared in the list)
        listed_date = self._parse_date(item.get("pub_raw", ""))

        # Abstract
        abstract = self._build_abstract(soup, item.get("rss_desc", ""))
        if len(abstract) < 50:
            print(f"[{self.site_id}] short abstract ({len(abstract)}c), skipping: {url}")
            return None

        # Node ID — used as external_id and post_number
        raw_text = raw.decode("utf-8", errors="replace")
        node_m = re.search(r"/node/(\d+)", raw_text)
        if not node_m:
            node_m = re.search(r'"nid"\s*:\s*(\d+)', raw_text)
        node_id = node_m.group(1) if node_m else url.rstrip("/").split("/")[-1]

        # PDF links
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.pdf", href, re.I):
                if not href.startswith("http"):
                    href = self.base_url + href
                if href not in pdf_links:
                    pdf_links.append(href)
        pdf_url = pdf_links[0] if pdf_links else None

        # Original filename from first PDF URL
        orig_fn = None
        if pdf_url:
            path_seg = pdf_url.split("?")[0].split("/")[-1]
            orig_fn = urllib.parse.unquote(path_seg)

        # Topics / keywords
        topics = []
        tlist = soup.find("ul", class_="site-topics--list")
        if tlist:
            for li in tlist.find_all("li"):
                t = re.sub(r"[,\s]+$", "", li.get_text(strip=True))
                if t:
                    topics.append(t)

        publisher = item.get("creator") or "Department for the Economy"

        return {
            "site_id": self.site_id,
            "external_id": node_id,
            "post_number": node_id,
            "url": url,
            "meta_url": url,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": publisher,
            "publisher": publisher,
            "department": "; ".join(topics) if topics else None,
            "pdf_url": pdf_url,
            "original_filename": orig_fn,
            "keywords": ", ".join(topics) if topics else None,
            "category": topics[0] if topics else None,
            "metadata": json.dumps({
                "node_id": node_id,
                "pdf_links": pdf_links,
                "topics": topics,
                "posted_date": item.get("pub_raw", ""),
            }),
        }

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Crawl publications and save to DB. Returns number of saved items."""
        start_time = time.time()
        MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes
        MAX_PAGES = 200           # safety cap: each "page" = 10 items

        seen_urls: set = set()
        saved = 0
        limit_eff = limit if limit is not None else float("inf")

        print(f"[{self.site_id}] crawl start, limit={limit}")

        # ── Phase 1: RSS (most recent 100) ──────────────────────────────
        print(f"[{self.site_id}] fetching RSS feed…")
        rss = self._rss_items()
        print(f"[{self.site_id}] RSS: {len(rss)} items")

        # ── Phase 2: Sitemaps (older items, only if needed) ──────────────
        sitemap: list = []
        if limit is None or limit > len(rss):
            print(f"[{self.site_id}] fetching sitemaps…")
            sitemap = self._sitemap_urls()
            print(f"[{self.site_id}] sitemaps: {len(sitemap)} items")

        # Build deduplicated queue: RSS first (newest), then sitemap (older)
        queue = []
        for item in rss + sitemap:
            u = item["url"]
            if u not in seen_urls:
                seen_urls.add(u)
                queue.append(item)
        print(f"[{self.site_id}] total unique URLs queued: {len(queue)}")

        # ── Process items ────────────────────────────────────────────────
        for idx, item in enumerate(queue):
            if saved >= limit_eff:
                break

            if time.time() - start_time > MAX_WALL_SECS:
                print(f"[{self.site_id}] wall-clock limit reached at item {idx}, stopping")
                break

            page_num = idx // 10
            if page_num >= MAX_PAGES:
                print(f"[{self.site_id}] safety cap {MAX_PAGES} pages reached, stopping")
                break

            if idx > 0 and idx % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit or 'inf'}")

            try:
                paper = self._fetch_detail(item)
                if paper is None:
                    time.sleep(self._delay)
                    continue
                self._save_paper(paper)
                saved += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {item.get('url', '?')} failed: {exc}")

            time.sleep(self._delay)

        print(f"[{self.site_id}] done. saved={saved}")
        return saved
