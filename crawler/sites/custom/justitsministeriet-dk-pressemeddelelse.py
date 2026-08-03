# -*- coding: utf-8 -*-
"""Justitsministeriet pressemeddelelse (Danish Ministry of Justice press releases).

Starting URL: https://www.justitsministeriet.dk/pressemeddelelse/
Pagination: /side/{N}/ (Danish for "page")
10 articles per listing page, WordPress post ID in article element.
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


def _try_bs4(html: str):
    """Parse HTML with BeautifulSoup, trying parsers in fallback order."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, ua: str, retries: int = 3):
    """Fetch URL via curl with TLS workaround and exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {ua}",
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", "Accept-Language: da,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[justitsministeriet-dk-pressemeddelelse] curl error ({url}): {exc}")
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return None


class JustitsministerietPressemeddelelseCrawler(BaseCrawler):
    """Crawler for Justitsministeriet pressemeddelelse."""

    site_id = "justitsministeriet-dk-pressemeddelelse"
    site_name = "Custom: justitsministeriet-dk-pressemeddelelse"
    base_url = "https://www.justitsministeriet.dk"

    _LIST_URL = "https://www.justitsministeriet.dk/pressemeddelelse/"
    _PAGE_URL = "https://www.justitsministeriet.dk/pressemeddelelse/side/{page}/"

    def _fetch(self, url: str):
        return _curl_get(url, self.USER_AGENT)

    def _parse_listing(self, html: str) -> list:
        """Extract article stubs from a listing page."""
        results = []
        soup = _try_bs4(html)
        if soup is None:
            return results

        for article in soup.find_all("article"):
            try:
                # WordPress post ID from id="post-XXXX"
                art_id = article.get("id", "")
                m = re.search(r"post-(\d+)", art_id)
                post_number = m.group(1) if m else None

                h2 = article.find("h2", class_="entry-title")
                if not h2:
                    continue
                a_tag = h2.find("a")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                url = (a_tag.get("href") or "").strip()
                if not url or not title:
                    continue

                time_tag = article.find("time", class_="entry-date")
                listed_date = ""
                if time_tag:
                    dt = time_tag.get("datetime", "")
                    if dt:
                        listed_date = dt[:10]  # 2026-04-14T... → 2026-04-14

                summary_div = article.find("div", class_="entry-summary")
                summary = summary_div.get_text(separator=" ", strip=True) if summary_div else ""

                results.append({
                    "post_number": post_number,
                    "title": title,
                    "url": url,
                    "listed_date": listed_date,
                    "listing_summary": summary,
                })
            except Exception as exc:
                print(f"[justitsministeriet-dk-pressemeddelelse] listing parse error: {exc}")
                continue

        return results

    def _parse_detail(self, html: str) -> dict:
        """Extract full content, date, and PDF from a detail page."""
        result = {
            "post_number": None,
            "published_date": "",
            "abstract": "",
            "pdf_url": None,
            "original_filename": None,
        }

        soup = _try_bs4(html)
        if soup is None:
            return result

        article = soup.find("article")
        if article:
            art_id = article.get("id", "")
            m = re.search(r"post-(\d+)", art_id)
            if m:
                result["post_number"] = m.group(1)

        time_tag = soup.find("time", class_="entry-date")
        if time_tag:
            dt = time_tag.get("datetime", "")
            if dt:
                result["published_date"] = dt[:10]

        summary_div = soup.find("div", class_="entry-summary")
        summary_text = summary_div.get_text(separator=" ", strip=True) if summary_div else ""

        content_div = soup.find("div", class_="entry-content")
        content_text = content_div.get_text(separator=" ", strip=True) if content_div else ""

        parts = []
        if summary_text:
            parts.append(summary_text)
        if content_text and content_text != summary_text:
            parts.append(content_text)
        result["abstract"] = "\n\n".join(parts)

        # First PDF in content area
        if content_div:
            for a in content_div.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    result["pdf_url"] = href
                    fname = href.rstrip("/").split("/")[-1].split("?")[0]
                    result["original_filename"] = fname if fname else None
                    break

        return result

    def crawl(self, limit=None):
        """Crawl press releases with full pagination."""
        saved = 0
        seen_urls: set = set()
        page = 1
        MAX_PAGES = 200
        WALL_LIMIT = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > WALL_LIMIT:
                print(f"[{self.site_id}] Wall-clock budget reached after {page - 1} pages. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            list_url = self._LIST_URL if page == 1 else self._PAGE_URL.format(page=page)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            # Fetch listing with retries
            html = None
            for attempt in range(3):
                html = self._fetch(list_url)
                if html:
                    break
                wait = [1, 3, 9][min(attempt, 2)]
                print(f"[{self.site_id}] Failed to fetch listing page {page}, retry {attempt + 1}/3 in {wait}s")
                time.sleep(wait)

            if not html:
                print(f"[{self.site_id}] Could not fetch page {page}. Stopping.")
                break

            items = self._parse_listing(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Stopping.")
                break

            for it in new_items:
                if limit is not None and saved >= limit:
                    break

                url = it["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)

                    detail_html = None
                    for attempt in range(3):
                        detail_html = self._fetch(url)
                        if detail_html:
                            break
                        wait = [1, 3, 9][min(attempt, 2)]
                        print(f"[{self.site_id}] Failed detail fetch {url}, retry {attempt + 1}/3 in {wait}s")
                        time.sleep(wait)

                    if not detail_html:
                        print(f"[{self.site_id}] Could not fetch detail {url}. Skipping.")
                        continue

                    detail = self._parse_detail(detail_html)

                    title = it["title"]
                    abstract = detail["abstract"]

                    # Fall back to listing summary if detail has no content
                    if not abstract.strip() and it["listing_summary"]:
                        abstract = it["listing_summary"]

                    if len(abstract.strip()) < 50:
                        print(f"[{self.site_id}] Abstract too short (<50 chars) for: {title[:60]}. Skipping.")
                        continue

                    pub_date = detail["published_date"] or it["listed_date"]
                    listed_date = it["listed_date"]
                    post_number = detail["post_number"] or it.get("post_number")
                    pdf_url = detail["pdf_url"]
                    original_filename = detail["original_filename"]
                    slug = url.rstrip("/").split("/")[-1]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_number or slug,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": "Justitsministeriet",
                        "authors": "",
                        "keywords": "",
                        "category": "Pressemeddelelse",
                        "doi": "",
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "post_id": post_number,
                            "slug": slug,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
