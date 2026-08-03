# -*- coding: utf-8 -*-
"""Crawler for NRS Scotland publications.

Starting URL: https://www.nrscotland.gov.uk/publications/
Pagination   : ?page=N  (15 items/page, ~344 total = ~23 pages)
List items   : .ds_category-item  /  .ds_category-item__link
Detail page  : meta[name=description] = abstract,  DT/DD "Date published",
               /media/...pdf = PDF attachment
"""

from __future__ import annotations

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
    """Parse HTML (str or bytes) with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


class NRSScotlandPublicationsCrawler(BaseCrawler):
    site_id   = "nrscotland-gov-uk-publications"
    site_name = "Custom: nrscotland-gov-uk-publications"
    base_url  = "https://www.nrscotland.gov.uk"

    _START_URL   = "https://www.nrscotland.gov.uk/publications/"
    _MAX_PAGES   = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MIN_ABSTRACT = 50   # chars; items shorter than this are skipped
    _BUDGET_SECS  = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock cap

    # ------------------------------------------------------------------
    # Low-level network
    # ------------------------------------------------------------------

    def _curl(self, url, retries=3):
        """Fetch URL via curl (TLS-flexible). Returns str or None."""
        backoff = (1, 3, 9)
        for attempt in range(retries):
            try:
                r = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-skL", "--compressed",
                        "--max-time", "45", "--connect-timeout", "15",
                        "-A", self.USER_AGENT,
                        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                        "-H", "Accept-Language: en-GB,en;q=0.9",
                        url,
                    ],
                    capture_output=True,
                    timeout=55,
                )
                stdout = r.stdout or b""
                if r.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
                print(f"[{self.site_id}] curl rc={r.returncode} for {url}: {stderr[:120]}")
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error for {url}: {exc}")

            if attempt < retries - 1:
                wait = backoff[attempt]
                print(f"[{self.site_id}] retry in {wait}s…")
                time.sleep(wait)

        print(f"[{self.site_id}] {url} failed after {retries} attempts")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(s):
        """Convert various date strings to ISO YYYY-MM-DD, or None."""
        if not s:
            return None
        s = re.sub(r"\s+", " ", str(s)).strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None

    @staticmethod
    def _clean(s):
        return re.sub(r"\s+", " ", str(s)).strip() if s else ""

    def _abs_url(self, href):
        if not href:
            return ""
        if href.startswith("http"):
            return href
        return self.base_url + href if href.startswith("/") else self.base_url + "/" + href

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _list_items(self, soup):
        """Return list of item dicts from one publications list page."""
        results = []
        for el in soup.select(".ds_category-item"):
            link = el.select_one(".ds_category-item__link")
            if not link:
                continue
            href = self._clean(link.get("href", ""))
            if not href:
                continue

            title = self._clean(link.get_text(" ", strip=True))
            if not title:
                continue

            list_date = topic = pub_type = ""
            for mi in el.select(".ds_metadata__item"):
                dt = mi.select_one("dt")
                dd = mi.select_one("dd")
                if not dt or not dd:
                    continue
                key = dt.get_text(strip=True).lower()
                val = self._clean(dd.get_text(" ", strip=True))
                if "published" in key:
                    list_date = val
                elif "topic" in key:
                    topic = val
                elif "type" in key:
                    pub_type = val

            # Snippet: text inside the item card, between title and metadata block
            full_text = self._clean(el.get_text(" ", strip=True))
            snippet = ""
            # Try to isolate the description part (after title, before "Published")
            pub_idx = full_text.find("Published")
            if pub_idx > len(title):
                candidate = full_text[len(title):pub_idx].strip()
                # Remove trailing ellipsis artefact
                candidate = re.sub(r"\s*\.\.\.\s*$", "", candidate).strip()
                snippet = candidate

            results.append({
                "href": href,
                "title": title,
                "list_date": list_date,
                "topic": topic,
                "pub_type": pub_type,
                "snippet": snippet,
            })
        return results

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, item):
        """Fetch one detail page and return a paper dict, or None to skip."""
        url = self._abs_url(item["href"])

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

        # ── Abstract ──────────────────────────────────────────────────
        _NOISE = (
            "download", "share this page", "click here to share",
            "open government licence", "crown copyright",
            "cookie", "skip to content", "subscribe", "back to top",
        )

        def _is_useful(text):
            low = text.lower()
            return len(text) >= 40 and not any(n in low for n in _NOISE)

        abstract = ""
        for meta in soup.find_all("meta"):
            name = meta.get("name", "") or meta.get("property", "")
            if name in ("description", "og:description"):
                content = self._clean(meta.get("content", ""))
                if len(content) > len(abstract):
                    abstract = content

        # Supplement with body text when meta description is short (<200 chars).
        # Collect paragraphs and append until we reach ≥200 chars, skipping noise.
        if len(abstract) < 200:
            main = soup.find("main") or soup
            parts = [abstract] if abstract else []
            for p in main.find_all("p"):
                text = self._clean(p.get_text(" ", strip=True))
                if not _is_useful(text):
                    continue
                if text == abstract:
                    continue
                parts.append(text)
                if len(" ".join(parts)) >= 200:
                    break
            combined = " ".join(parts).strip()
            if combined:
                abstract = combined

        # Final fallback: list snippet
        if len(abstract) < self._MIN_ABSTRACT and item.get("snippet"):
            abstract = item["snippet"]

        if len(abstract) < self._MIN_ABSTRACT:
            print(
                f"[{self.site_id}] {url} skipped: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        # ── Published date ────────────────────────────────────────────
        published_date = None
        for dt_el in soup.find_all("dt"):
            if "date published" in dt_el.get_text(strip=True).lower():
                dd_el = dt_el.find_next_sibling("dd")
                if dd_el:
                    published_date = self._parse_date(dd_el.get_text(strip=True))
                    break
        if not published_date:
            published_date = self._parse_date(item["list_date"])

        # ── PDF link ──────────────────────────────────────────────────
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href_a = a["href"]
            if ".pdf" in href_a.lower():
                pdf_url = self._abs_url(href_a)
                seg = href_a.rstrip("/").split("/")[-1].split("?")[0]
                original_filename = urllib.parse.unquote(seg)
                break

        # ── Identifiers ───────────────────────────────────────────────
        slug = url.rstrip("/").split("/")[-1]
        listed_date = self._parse_date(item["list_date"])
        topic    = item.get("topic", "")
        pub_type = item.get("pub_type", "")
        keywords = ", ".join(filter(None, [topic, pub_type])) or None

        return {
            "site_id":           self.site_id,
            "external_id":       slug,
            "post_number":       None,          # no numeric IDs on this site
            "url":               url,
            "meta_url":          url,
            "title":             item["title"],
            "abstract":          abstract,
            "published_date":    published_date,
            "listed_date":       listed_date,
            "posted_date":       listed_date,
            "publisher":         "National Records of Scotland",
            "department":        topic or None,
            "category":          pub_type or None,
            "keywords":          keywords,
            "pdf_url":           pdf_url,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "source_format":   "html-list+html-detail",
                    "list_date_raw":   item["list_date"],
                    "topic":           topic,
                    "type":            pub_type,
                    "posted_date":     item["list_date"],
                    "originalFilename": original_filename,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NRS Scotland publications; return count of saved items."""
        start_time = time.time()
        seen_urls: set = set()
        saved = 0
        limit_eff = limit if limit is not None else float("inf")

        print(f"[{self.site_id}] crawl start, limit={limit}")

        reached_cap = False
        for page in range(1, self._MAX_PAGES + 1):
            if saved >= limit_eff:
                break

            elapsed = time.time() - start_time
            if elapsed > self._BUDGET_SECS:
                print(f"[{self.site_id}] wall-clock limit reached at page {page}, stopping")
                break

            # Progress log every 10 pages
            if page % 10 == 0:
                print(
                    f"[{self.site_id}] page {page}: "
                    f"saved {saved}/{limit if limit is not None else 'inf'}"
                )

            list_url = self._START_URL if page == 1 else f"{self._START_URL}?page={page}"
            raw = self._curl(list_url)
            if not raw:
                print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{self.site_id}] list page {page} parse error: {exc}; stopping")
                break
            if soup is None:
                print(f"[{self.site_id}] list page {page} soup is None; stopping")
                break

            items = self._list_items(soup)
            if not items:
                print(f"[{self.site_id}] page {page}: no items found; stopping")
                break

            # URL deduplication — prevents infinite loops if paginator wraps
            new_items = []
            for it in items:
                canonical = self._abs_url(it["href"])
                if canonical in seen_urls:
                    continue
                seen_urls.add(canonical)
                new_items.append(it)

            if not new_items:
                print(f"[{self.site_id}] page {page}: all items already seen (dedup); stopping")
                break

            # Fetch and save each detail page
            for it in new_items:
                if saved >= limit_eff:
                    break

                if time.time() - start_time > self._BUDGET_SECS:
                    print(f"[{self.site_id}] wall-clock limit reached; stopping item loop")
                    break

                try:
                    paper = self._fetch_detail(it)
                    if paper is None:
                        time.sleep(self._delay)
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}"
                        f"/{limit if limit is not None else 'inf'}: "
                        f"{it['title'][:80]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {it.get('href', '?')} failed: {exc}")

                time.sleep(self._delay)

            if saved >= limit_eff:
                break

            # Detect end of pagination: look for link to next page number
            next_page_marker = f"?page={page + 1}"
            has_next = any(
                next_page_marker in (a.get("href") or "")
                for a in soup.find_all("a", href=True)
            )
            if not has_next:
                print(f"[{self.site_id}] no next page after page {page}; done")
                break

            if page == self._MAX_PAGES:
                reached_cap = True

        if reached_cap:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; logged")

        print(f"[{self.site_id}] done. saved={saved}")
        return saved
