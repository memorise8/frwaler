# -*- coding: utf-8 -*-
"""Crawler for Rigsrevisionen revisionssager (Danish National Audit Office).

All audit reports and notes published since 2000 are available at:
  https://www.rigsrevisionen.dk/revisionssager

Detail pages live at:
  https://www.rigsrevisionen.dk/revisionssager-arkiv/YEAR/MONTH/SLUG

The sitemap at /sitemap.xml lists all ~800 items; we use it as the
authoritative URL list instead of the dynamic-list API (which requires
encrypted Blazor payloads).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class RigsrevisionenDkRevisionssagerCrawler(BaseCrawler):
    site_id = "rigsrevisionen-dk-revisionssager"
    site_name = "Custom: rigsrevisionen-dk-revisionssager"
    base_url = "https://www.rigsrevisionen.dk"

    _SITEMAP_URL = "https://www.rigsrevisionen.dk/sitemap.xml"
    _ITEM_PATH_RE = re.compile(r"^/revisionssager-arkiv/\d{4}/[a-z]+/[^/]+$")
    _CURL_TIMEOUT = 45
    _MIN_ABSTRACT_CHARS = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _PAGE_SIZE = 10
    _CRAWL_BUDGET_SECS = 1500  # 25 minutes

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = detail_delay if detail_delay is not None else delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=None, referer=None):
        """Fetch URL via curl with up to 3 retries (1s, 3s, 9s backoff)."""
        timeout = timeout or self._CURL_TIMEOUT
        cmd = [
            "curl", "-g", "--tls-max", "1.3", "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: da-DK,da;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr[:120]}"

            if attempt < 2:
                w = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {w}s"
                )
                time.sleep(w)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _parse_html(raw):
        """Parse HTML; fallback chain: html5lib → lxml → html.parser."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(
                    f"[rigsrevisionen-dk-revisionssager] "
                    f"BeautifulSoup({parser}) failed: {exc}"
                )
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _tag_text(cls, tag, separator=" "):
        if not tag:
            return ""
        return re.sub(r"\s+", " ", tag.get_text(separator, strip=True)).strip()

    # ------------------------------------------------------------------
    # Sitemap parsing
    # ------------------------------------------------------------------

    def _parse_sitemap(self, raw):
        """Extract revisionssager-arkiv item URLs + lastmod from sitemap XML.

        Returns list of {"url": ..., "lastmod": ...} dicts, newest first.
        """
        entries = re.findall(
            r"<url>\s*<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]*)</lastmod>)?",
            raw,
            re.DOTALL,
        )
        items = []
        for loc, lastmod in entries:
            loc = loc.strip()
            parsed = urlparse(loc)
            if not self._ITEM_PATH_RE.match(parsed.path):
                continue
            items.append({"url": loc, "lastmod": (lastmod or "").strip()})
        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _extract_pdf(self, soup):
        """Return (pdf_url, original_filename) preferring the full report PDF."""
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not href:
                continue
            if ".pdf" in href.lower() or "/Media/" in href:
                label = self._tag_text(a).lower()
                pdf_links.append((href, label))

        chosen = ""
        for href, label in pdf_links:
            if "hele" in label or ("beretning" in label and "kort" not in label):
                chosen = href
                break
        if not chosen and pdf_links:
            chosen = pdf_links[-1][0]

        if not chosen:
            return "", ""

        full_url = urljoin(self.base_url, chosen)
        tail = full_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        filename = tail if ("." in tail and len(tail) <= 200) else ""
        return full_url, filename

    def _parse_detail(self, raw, page_url, sitemap_entry):
        """Parse a detail page and return a paper_dict for _save_paper."""
        soup = self._parse_html(raw)
        if soup is None:
            raise RuntimeError("detail HTML could not be parsed")

        # --- Title ---
        h1 = soup.find("h1")
        title = self._tag_text(h1) if h1 else ""
        if not title:
            og = soup.find("meta", property="og:title")
            if og:
                title = (og.get("content") or "").strip()
        if not title:
            title = urlparse(page_url).path.rstrip("/").split("/")[-1].replace("-", " ")

        # --- Date from span with data-date attribute ---
        raw_date = ""
        published_date = ""
        dt_el = soup.find(attrs={"data-date": True})
        if dt_el:
            raw_date = (dt_el.get("data-date") or "").strip()
        if not raw_date:
            # Fallback: look for datetime class
            dt_el2 = soup.find(class_=re.compile(r"\bdatetime\b"))
            if dt_el2:
                raw_date = (dt_el2.get("data-date") or dt_el2.get_text(strip=True) or "").strip()
        if raw_date:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
            if m:
                published_date = m.group(1)

        # --- Abstract and Statsrevisorernes content ---
        # Collect .rich-text sections that are long enough to be content
        # (skip footer sections with address/hours which are < 100 chars)
        content_sections = []
        for rt in soup.find_all(class_="rich-text"):
            text = self._clean_text(rt.get_text(" ", strip=True))
            if len(text) >= 100:
                content_sections.append(text)

        abstract = content_sections[0] if content_sections else ""
        statsrevisor_text = content_sections[1] if len(content_sections) > 1 else ""

        # If abstract is short, try supplementing with statsrevisor notes
        if len(abstract) < 100 and statsrevisor_text:
            abstract = (abstract + "\n\n" + statsrevisor_text).strip() if abstract else statsrevisor_text

        # --- PDF ---
        pdf_url, original_filename = self._extract_pdf(soup)

        # --- Category label ---
        category = ""
        label_el = soup.find(class_="label")
        if label_el:
            category = self._tag_text(label_el)

        # --- external_id: URL path without leading slash ---
        url_path = urlparse(page_url).path.strip("/")
        # e.g. "revisionssager-arkiv/2026/apr/beretning-om-..."
        external_id = url_path

        # post_number: ISO datetime from data-date (sortable string).
        # MAX(post_number) correctly identifies the most recently published item.
        post_number = raw_date or published_date or sitemap_entry.get("lastmod") or None

        # --- URL-derived metadata ---
        path_parts = url_path.split("/")
        year_from_url = path_parts[1] if len(path_parts) > 1 else ""
        month_from_url = path_parts[2] if len(path_parts) > 2 else ""

        metadata = {
            "posted_date": raw_date,
            "category": category,
            "year_from_url": year_from_url,
            "month_from_url": month_from_url,
            "sitemap_lastmod": sitemap_entry.get("lastmod") or "",
        }
        if statsrevisor_text:
            # Store up to 3000 chars of the review notes for downstream use
            metadata["statsrevisorernes_bemaerkninger"] = statsrevisor_text[:3000]
        if original_filename:
            metadata["originalFilename"] = original_filename
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", {}, [])}

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": page_url,
            "pdf_url": pdf_url or None,
            "publisher": "Rigsrevisionen",
            "department": "Rigsrevisionen",
            "category": category,
            "original_filename": original_filename or None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_label = str(limit) if limit is not None else "∞"
        print(f"[{self.site_id}] Starting crawl (limit={limit_label})")
        print(f"[{self.site_id}] Discovery via sitemap: {self._SITEMAP_URL}")

        # Step 1: fetch sitemap
        sitemap_raw = self._curl(self._SITEMAP_URL)
        if not sitemap_raw:
            print(f"[{self.site_id}] Failed to fetch sitemap; aborting")
            return 0

        # Step 2: parse item URLs
        sitemap_items = self._parse_sitemap(sitemap_raw)
        if not sitemap_items:
            print(f"[{self.site_id}] No revisionssager items found in sitemap; aborting")
            return 0

        # Newest first: lastmod is ISO datetime, lexicographic sort is chronological
        sitemap_items.sort(key=lambda x: x.get("lastmod") or "", reverse=True)
        print(f"[{self.site_id}] Found {len(sitemap_items)} items in sitemap")

        # Step 3: crawl detail pages
        saved = 0
        seen_urls: set = set()
        start_time = time.time()

        total_pages = min(
            self._MAX_PAGES,
            (len(sitemap_items) + self._PAGE_SIZE - 1) // self._PAGE_SIZE,
        )

        for page_idx in range(total_pages):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > self._CRAWL_BUDGET_SECS:
                print(
                    f"[{self.site_id}] Time budget exceeded ({elapsed:.0f}s); stopping cleanly"
                )
                break

            if page_idx % 10 == 0:
                print(
                    f"[{self.site_id}] page {page_idx + 1}: saved {saved}/{limit_label}"
                )

            page_items = sitemap_items[
                page_idx * self._PAGE_SIZE : (page_idx + 1) * self._PAGE_SIZE
            ]
            if not page_items:
                break

            for entry in page_items:
                if limit is not None and saved >= limit:
                    break

                url = entry["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self.detail_delay)
                    detail_raw = self._curl(
                        url, referer=f"{self.base_url}/revisionssager"
                    )
                    if not detail_raw:
                        raise RuntimeError("fetch failed after retries")

                    paper = self._parse_detail(detail_raw, url, entry)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping {url}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_label}: "
                        f"{paper['title'][:80]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

        if page_idx >= self._MAX_PAGES - 1:
            print(
                f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached; stopping"
            )

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
