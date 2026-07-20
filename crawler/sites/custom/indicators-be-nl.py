# -*- coding: utf-8 -*-
"""Crawler for indicators.be – Belgian SDG indicators (Dutch / nl)."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class IndicatorsBeNlCrawler(BaseCrawler):
    site_id = "indicators-be-nl"
    site_name = "Custom: indicators-be-nl"
    base_url = "https://indicators.be"

    _START_URL = "https://indicators.be/nl/t/SDG/"
    _PUBLISHER = "Federaal Planbureau"
    _MIN_ABSTRACT_CHARS = 50
    _MAX_ITEMS = 200  # safety cap

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: nl-NL,nl;q=0.9,en;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s*\n\s*", "\n", text)
        return text.strip()

    @classmethod
    def _tag_text(cls, tag):
        if not tag:
            return ""
        return cls._clean_text(tag.get_text(" ", strip=True))

    @staticmethod
    def _parse_date_nl(raw):
        """Convert DD/MM/YYYY → YYYY-MM-DD."""
        text = str(raw or "").strip()
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if m:
            d, mo, y = m.groups()
            return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
        m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        return m.group(1) if m else ""

    # ------------------------------------------------------------------
    # Index page: discover all indicator URLs
    # ------------------------------------------------------------------

    def _extract_indicator_links(self, raw):
        """Return ordered list of unique indicator detail URLs from the SDG index."""
        soup = self._parse_html(raw)
        if not soup:
            return []
        links = []
        seen = set()
        for a in soup.select("a[href]"):
            href = str(a.get("href") or "")
            # Only /nl/i/{CODE}/{SLUG} links
            if re.match(r"^/nl/i/[A-Z0-9_]+/", href):
                url = urljoin(self.base_url, href)
                if url not in seen:
                    seen.add(url)
                    links.append(url)
        return links

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw, url):
        """Parse one indicator page. Returns paper dict or None on failure."""
        soup = self._parse_html(raw)
        if not soup:
            return None

        # --- External ID from URL path: /nl/i/G01_PSE/Slug ---
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        # parts: ['nl', 'i', 'G01_PSE', 'Slug']
        if len(parts) < 3:
            return None
        external_id = parts[2]  # e.g., 'G01_PSE'

        # --- Category = SDG group prefix (e.g., 'G01') ---
        cat_match = re.match(r"(G\d+)", external_id)
        category = cat_match.group(1) if cat_match else external_id

        # --- post_number: numeric indicator index from title like (i01) → '1' ---
        # Try to find it from the title text before we strip the icon
        title_tag = soup.select_one("h4")
        post_number = None
        title = ""
        if title_tag:
            # Remove icon children so get_text gives clean title
            for icon in title_tag.select("i"):
                icon.decompose()
            title = self._clean_text(title_tag.get_text(" ", strip=True))
        if not title:
            return None

        num_match = re.search(r"\(i(\d+)\)", title)
        if num_match:
            post_number = str(int(num_match.group(1)))  # '01' → '1'
        else:
            post_number = external_id

        # --- Date from calendar icon ---
        date_raw = ""
        for li in soup.select("li"):
            if li.find("i", class_="icon-calendar"):
                date_raw = self._tag_text(li)
                break
        published_date = self._parse_date_nl(date_raw)

        # --- Abstract: <p> sibling(s) of ul.blog-info before div.tab-v1 ---
        blog_info = soup.select_one("ul.blog-info")
        abstract_parts = []
        if blog_info:
            for sib in blog_info.next_siblings:
                tag_name = getattr(sib, "name", None)
                if tag_name is None:
                    continue
                if tag_name == "p":
                    text = self._clean_text(sib.get_text(" ", strip=True))
                    if text:
                        abstract_parts.append(text)
                elif tag_name == "div":
                    break
        abstract = " ".join(abstract_parts).strip()

        # If no abstract found via sibling traversal, try a broader search
        if not abstract:
            main = soup.select_one("div.col-md-9") or soup.select_one("div.tab-v1")
            if main:
                for p in main.select("p"):
                    text = self._clean_text(p.get_text(" ", strip=True))
                    if len(text) >= self._MIN_ABSTRACT_CHARS:
                        abstract = text
                        break

        # --- SDG target value from disabled button ---
        goal_btn = soup.select_one("button.disabled")
        sdg_goal = self._tag_text(goal_btn).strip() if goal_btn else ""

        # --- Evaluation status from icon class ---
        eval_icon = soup.select_one(".icon-round-sm")
        eval_class = " ".join(eval_icon.get("class", [])) if eval_icon else ""
        if "icon-bg-green" in eval_class:
            evaluation = "gunstig"
        elif "icon-bg-red" in eval_class:
            evaluation = "ongunstig"
        else:
            evaluation = "onbepaald"

        metadata = {
            "external_id": external_id,
            "sdg_goal": sdg_goal,
            "evaluation": evaluation,
            "language": "nl",
            "category": category,
        }
        if sdg_goal:
            metadata["sdg_target_value"] = sdg_goal

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "posted_date": published_date,
            "url": url,
            "pdf_url": None,
            "original_filename": None,
            "authors": "",
            "publisher": self._PUBLISHER,
            "department": self._PUBLISHER,
            "journal": "",
            "keywords": category,
            "category": category,
            "doi": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        max_wall = 25 * 60  # 25 minutes

        saved = 0
        seen_urls = set()

        # --- Step 1: fetch index page ---
        print(f"[{self.site_id}] Fetching index: {self._START_URL}")
        raw_index = self._curl(self._START_URL)
        if not raw_index:
            print(f"[{self.site_id}] Failed to fetch index page")
            return 0

        indicator_urls = self._extract_indicator_links(raw_index)
        total_found = len(indicator_urls)
        print(f"[{self.site_id}] Discovered {total_found} indicator URLs")

        if not indicator_urls:
            print(f"[{self.site_id}] No indicator URLs found on index page")
            return 0

        limit_or_inf = limit if limit is not None else total_found
        # Safety cap
        if len(indicator_urls) > self._MAX_ITEMS:
            print(f"[{self.site_id}] Safety cap: truncating to {self._MAX_ITEMS} items")
            indicator_urls = indicator_urls[: self._MAX_ITEMS]

        # --- Step 2: process each indicator ---
        for idx, url in enumerate(indicator_urls, start=1):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > max_wall:
                print(f"[{self.site_id}] Wall-clock budget (25 min) reached at item {idx}; stopping")
                break

            if url in seen_urls:
                print(f"[{self.site_id}] Skipping duplicate URL: {url}")
                continue
            seen_urls.add(url)

            # Progress log every 10 items (treating each batch of 10 as a "page")
            page_num = (idx - 1) // 10 + 1
            if (idx - 1) % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_or_inf}")

            try:
                time.sleep(self._delay)
                raw = self._curl(url)
                if not raw:
                    print(f"[{self.site_id}] item {idx} failed: fetch returned nothing for {url}")
                    continue

                paper = self._parse_detail(raw, url)
                if not paper:
                    print(f"[{self.site_id}] item {idx} failed: could not parse {url}")
                    continue

                if not paper.get("title"):
                    print(f"[{self.site_id}] item {idx} skipped: missing title for {url}")
                    continue

                abstract = paper.get("abstract") or ""
                if len(abstract.strip()) < self._MIN_ABSTRACT_CHARS:
                    print(
                        f"[{self.site_id}] item {idx} skipped: abstract too short "
                        f"({len(abstract.strip())} chars) for {url}"
                    )
                    continue

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
