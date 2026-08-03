# -*- coding: utf-8 -*-
"""Pembina Institute media releases crawler."""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from crawler.base_crawler import BaseCrawler

_MONTHS = {
    'January': '01', 'February': '02', 'March': '03', 'April': '04',
    'May': '05', 'June': '06', 'July': '07', 'August': '08',
    'September': '09', 'October': '10', 'November': '11', 'December': '12',
}


def _parse_date_text(text):
    """Convert 'May 14, 2026' -> '2026-05-14'."""
    if not text:
        return ''
    m = re.search(
        r'(January|February|March|April|May|June|July|August'
        r'|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
        text,
    )
    if m:
        month = _MONTHS[m.group(1)]
        day = m.group(2).zfill(2)
        year = m.group(3)
        return f"{year}-{month}-{day}"
    return ''


class PembinaOrgMediaReleasesCrawler(BaseCrawler):
    """Crawler for Pembina Institute media releases."""

    site_id = "pembina-org-media-releases"
    site_name = "Custom: pembina-org-media-releases"
    base_url = "https://www.pembina.org"

    _LIST_URL = "https://www.pembina.org/media-releases"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl; returns decoded text or None on failure."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = (attempt + 1) ** 2
                if attempt < 2:
                    print(f"[{self.site_id}] Empty response ({url}), retry in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                wait = (attempt + 1) ** 2
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({url}): {exc}, retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts ({url}): {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """Parse HTML; fallback chain: html5lib -> lxml -> html.parser."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _parse_list_page(self, html):
        """Return list of card dicts from a listing page."""
        items = []
        try:
            soup = self._make_soup(html)
            if soup is None:
                return items

            cards = soup.find_all("div", class_=lambda c: c and "node-card" in c)
            for card in cards:
                try:
                    # URL
                    link = card.find("a", href=re.compile(r"^/media-release/"))
                    if not link:
                        continue
                    href = link["href"]
                    url = self.base_url + href
                    slug = href.rstrip("/").split("/")[-1]

                    # Date — first border-b div inside the card
                    date_text = ""
                    date_div = card.find(
                        "div",
                        class_=lambda c: c and "border-b" in c and "border-thin-border" in c,
                    )
                    if date_div:
                        date_text = date_div.get_text(strip=True)
                    listed_date = _parse_date_text(date_text)

                    # Category — dark-blue label
                    cat_div = card.find(
                        "div", class_=lambda c: c and "bg-dark-blue" in c
                    )
                    category = cat_div.get_text(strip=True) if cat_div else ""

                    # Title
                    h3 = card.find("h3")
                    title = h3.get_text(strip=True) if h3 else ""
                    if not title:
                        continue

                    # Teaser — sibling text after h3 in its parent div
                    teaser = ""
                    if h3 and h3.parent:
                        full = h3.parent.get_text(separator=" ", strip=True)
                        if full.startswith(title):
                            teaser = full[len(title):].strip()

                    items.append({
                        "url": url,
                        "slug": slug,
                        "listed_date": listed_date,
                        "category": category,
                        "title": title,
                        "teaser": teaser,
                    })
                except Exception as exc:
                    print(f"[{self.site_id}] card parse error: {exc}")
                    continue
        except Exception as exc:
            print(f"[{self.site_id}] list page parse error: {exc}")
        return items

    def _parse_detail_page(self, html, url):
        """Return a detail dict from a media-release page."""
        result = {
            "nid": None,
            "title": "",
            "subtitle": "",
            "date": "",
            "abstract": "",
            "authors": "",
            "contact_info": "",
            "pdf_url": None,
            "category": "",
        }
        try:
            soup = self._make_soup(html)
            if soup is None:
                return result

            # Node ID from body class
            nid_m = re.search(r"page-nid-(\d+)", html)
            if nid_m:
                result["nid"] = nid_m.group(1)

            # Title / subtitle
            h1 = soup.find("h1")
            if h1:
                result["title"] = h1.get_text(strip=True)
            h2 = soup.find("h2")
            if h2:
                result["subtitle"] = h2.get_text(strip=True)

            # Date from pub section
            pub = soup.find("div", class_=lambda c: c and "pub" in (c.split() if c else []))
            if pub:
                date_div = pub.find(
                    "div",
                    class_=lambda c: c and "border-b" in c and "border-thin-border" in c,
                )
                if date_div:
                    result["date"] = _parse_date_text(date_div.get_text(strip=True))

                cat_div = pub.find("div", class_=lambda c: c and "bg-dark-blue" in c)
                if cat_div:
                    result["category"] = cat_div.get_text(strip=True)

            # Article body
            article = soup.find("article")
            if article:
                parts = []
                for elem in article.find_all(["p", "h2", "h3", "h4", "li"]):
                    text = elem.get_text(separator=" ", strip=True)
                    if text:
                        parts.append(text)
                result["abstract"] = "\n\n".join(parts)

                # Contact section — extract spokesperson name
                contact_h = article.find(
                    lambda t: t.name in ("h3", "h4")
                    and re.search(r"contact", t.get_text(), re.IGNORECASE)
                )
                if contact_h:
                    contact_p = contact_h.find_next_sibling("p")
                    if contact_p:
                        links = contact_p.find_all("a")
                        if links:
                            result["authors"] = links[0].get_text(strip=True)
                        result["contact_info"] = contact_p.get_text(
                            separator="; ", strip=True
                        )

                # PDF links
                for a in article.find_all("a", href=True):
                    href = a["href"]
                    if ".pdf" in href.lower():
                        result["pdf_url"] = (
                            href if href.startswith("http") else self.base_url + href
                        )
                        break

        except Exception as exc:
            print(f"[{self.site_id}] detail parse error for {url}: {exc}")
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Pembina Institute media releases.

        Paginates through /media-releases?page=N until limit is reached,
        no new items are found, or the 200-page safety cap is hit.
        """
        saved = 0
        seen_urls = set()
        page = 0
        max_pages = 200
        start_time = time.time()
        max_wall_secs = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        limit_str = str(limit) if limit is not None else "∞"

        try:
            while page < max_pages:
                if time.time() - start_time > max_wall_secs:
                    print(
                        f"[{self.site_id}] 25-minute wall-clock budget reached "
                        f"at page {page}. Exiting cleanly."
                    )
                    break

                if limit is not None and saved >= limit:
                    break

                if page == max_pages - 1:
                    print(f"[{self.site_id}] Safety cap of {max_pages} pages reached.")
                    break

                list_url = f"{self._LIST_URL}?page={page}"
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                    break

                items = self._parse_list_page(raw)
                if not items:
                    print(f"[{self.site_id}] No items at page {page}. Done.")
                    break

                # URL deduplication — detect silent loop-back to page 1
                new_items = [it for it in items if it["url"] not in seen_urls]
                for it in new_items:
                    seen_urls.add(it["url"])

                if not new_items:
                    print(
                        f"[{self.site_id}] All items on page {page} already seen. Done."
                    )
                    break

                if page % 10 == 0:
                    print(
                        f"[{self.site_id}] page {page}: saved {saved}/{limit_str}"
                    )

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break

                    try:
                        time.sleep(self._delay)

                        detail_html = self._curl_get(item["url"])
                        if not detail_html:
                            print(
                                f"[{self.site_id}] Failed to fetch detail: {item['url']}"
                            )
                            continue

                        detail = self._parse_detail_page(detail_html, item["url"])

                        title = detail["title"] or item["title"]
                        if not title:
                            print(
                                f"[{self.site_id}] Skipping — no title: {item['url']}"
                            )
                            continue

                        subtitle = detail["subtitle"] or item["teaser"] or ""
                        abstract = detail["abstract"]

                        # Prepend subtitle if not already present
                        if subtitle and subtitle not in abstract:
                            abstract = subtitle + "\n\n" + abstract

                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] Skipping short abstract "
                                f"({len(abstract)} chars): {title[:60]}"
                            )
                            continue

                        published_date = detail["date"] or item["listed_date"]
                        listed_date = item["listed_date"]
                        category = detail["category"] or item["category"]
                        nid = detail["nid"]
                        slug = item["slug"]
                        external_id = nid or slug

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": nid,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": detail["authors"],
                            "publisher": "Pembina Institute",
                            "department": "",
                            "journal": "",
                            "url": item["url"],
                            "pdf_url": detail["pdf_url"],
                            "keywords": "",
                            "category": category,
                            "doi": "",
                            "original_filename": None,
                            "metadata": json.dumps(
                                {
                                    "posted_date": listed_date,
                                    "nid": nid,
                                    "slug": slug,
                                    "subtitle": subtitle,
                                    "teaser": item["teaser"],
                                    "contact_info": detail["contact_info"],
                                },
                                ensure_ascii=False,
                            ),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f"[{self.site_id}] item failed "
                            f"({item.get('url', '?')}): {exc}"
                        )
                        continue

                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved so far: {saved}")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
