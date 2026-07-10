# -*- coding: utf-8 -*-
"""Crawler for presse.economie.gouv.fr — Ministère de l'Économie (press releases).

Uses the site's RSS feed (/search/+/feed/rss2/?paged=N) which returns
full content:encoded per item — no separate detail-page fetch required.
"""

import json
import re
import subprocess
import time

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))

from crawler.base_crawler import BaseCrawler

_SITE_ID = "presse-economie-gouv-fr"
_RSS_BASE = "https://presse.economie.gouv.fr/search/+/feed/rss2/"
_PUBLISHER = "Ministère de l'Économie et des Finances"

# HTML entity map for common French typography entities in RSS CDATA
_HTML_ENTITIES = {
    "&#8217;": "’", "&#8216;": "‘",
    "&#8220;": "“", "&#8221;": "”",
    "&#8211;": "–", "&#8212;": "—",
    "&#8230;": "…", "&nbsp;": " ",
    "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#039;": "'",
}


def _decode_entities(text):
    """Decode common HTML entities in text (no full HTML parser needed)."""
    for ent, char in _HTML_ENTITIES.items():
        text = text.replace(ent, char)
    # Decimal numeric entities &#NNN;
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    return text


def _strip_html(html):
    """Strip HTML tags and normalise whitespace. Returns plain text."""
    if not html:
        return ""
    # Try BeautifulSoup with fallback chain
    try:
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                soup = BeautifulSoup(html, parser)
                text = soup.get_text(separator=" ")
                break
            except Exception:
                continue
        else:
            text = re.sub(r"<[^>]+>", " ", html)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)

    text = _decode_entities(text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove WordPress "The post X appeared first on Y." footer
    text = re.sub(
        r"The post .{1,300}? appeared first on .{1,200}?\.\s*$", "", text
    ).strip()
    return text


def _parse_rss_date(pub_date_str):
    """Parse RFC 2822 pubDate → ISO YYYY-MM-DD. Returns None on failure."""
    if not pub_date_str:
        return None
    try:
        from email.utils import parsedate
        t = parsedate(pub_date_str.strip())
        if t:
            return f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d}"
    except Exception:
        pass
    # Fallback regex for "13 May 2026"
    _MONTHS = {
        "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
        "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
        "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
    }
    m = re.search(r"(\d{1,2})\s+(\w{3})\w*\s+(\d{4})", pub_date_str)
    if m:
        day, mon, year = m.group(1), m.group(2)[:3].capitalize(), m.group(3)
        mo = _MONTHS.get(mon, "01")
        return f"{year}-{mo}-{int(day):02d}"
    return None


def _extract_cdata(tag, text):
    """Extract content from <tag><![CDATA[...]]></tag> or plain <tag>...</tag>."""
    m = re.search(
        rf"<{re.escape(tag)}[^>]*><!\[CDATA\[(.*?)\]\]></{re.escape(tag)}>",
        text, re.DOTALL
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        rf"<{re.escape(tag)}[^>]*>(.*?)</{re.escape(tag)}>",
        text, re.DOTALL
    )
    if m:
        return _decode_entities(m.group(1).strip())
    return None


def _parse_rss_items(rss_text):
    """Parse RSS XML text into a list of item dicts. Robust to malformed XML."""
    items = []
    raw_items = re.findall(r"<item>(.*?)</item>", rss_text, re.DOTALL)
    for raw in raw_items:
        try:
            title = _decode_entities(_extract_cdata("title", raw) or "")
            link = (_extract_cdata("link", raw) or "").strip()
            pub_date = (_extract_cdata("pubDate", raw) or "").strip()
            author = (_extract_cdata("dc:creator", raw) or "").strip()
            guid = (_extract_cdata("guid", raw) or "").strip()
            # content:encoded preferred over description
            content_html = _extract_cdata("content:encoded", raw) or ""
            if not content_html:
                content_html = _extract_cdata("description", raw) or ""

            # Categories (may appear multiple times). CDATA-wrapped values
            # take priority; only fall back to the plain-tag regex when no
            # CDATA categories were found — matching both unconditionally
            # double-collects the same category (once via CDATA, once via
            # the plain regex re-matching the CDATA markers as literal
            # text) and pollutes results with "CDATA" remnants + dupes.
            cats = re.findall(
                r"<category><!\[CDATA\[(.*?)\]\]></category>", raw, re.DOTALL
            )
            if not cats:
                cats = re.findall(r"<category>(.*?)</category>", raw, re.DOTALL)
            cats = [" ".join(c.split()) for c in cats]
            cats = list(dict.fromkeys(c for c in cats if c and "CDATA" not in c))

            # WordPress post ID from GUID
            post_id = None
            m = re.search(r"\?p=(\d+)", guid)
            if m:
                post_id = m.group(1)

            # Fix link: sometimes <link> is adjacent to CDATA in RSS and needs
            # special extraction (atom namespace style)
            if not link:
                link_m = re.search(r"<link>(https?://[^<]+)</link>", raw)
                if link_m:
                    link = link_m.group(1).strip()

            # Abstract: strip HTML from content:encoded
            abstract = _strip_html(content_html)

            # PDF links in content
            pdf_links = re.findall(
                r'href=["\']([^"\']+\.pdf[^"\']*)["\']', content_html, re.IGNORECASE
            )
            pdf_url = pdf_links[0] if pdf_links else None

            # Original filename from PDF URL
            original_filename = None
            if pdf_url:
                fn = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                if fn and "." in fn:
                    original_filename = fn

            # Press release number (N°NNN)
            release_num = None
            rn_m = re.search(r"N°\s*(\d+)", abstract)
            if rn_m:
                release_num = rn_m.group(1)

            # Category: first "press type" category, fallback to first
            category = None
            _PRESS_TYPES = {
                "communiqué de presse", "note aux rédactions",
                "agendas", "agenda", "discours",
            }
            for cat in cats:
                if cat.lower() in _PRESS_TYPES:
                    category = cat
                    break
            if not category and cats:
                category = cats[0]

            items.append({
                "post_id": post_id,
                "title": title,
                "link": link,
                "abstract": abstract,
                "content_html": content_html,
                "pub_date": pub_date,
                "published_date": _parse_rss_date(pub_date),
                "author": author,
                "categories": cats,
                "keywords": ", ".join(cats) if cats else None,
                "pdf_url": pdf_url,
                "original_filename": original_filename,
                "release_num": release_num,
                "category": category,
                "guid": guid,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] RSS item parse error: {exc}")
            continue
    return items


class PresseEconomieGouvCrawler(BaseCrawler):
    """Crawler for presse.economie.gouv.fr press releases via RSS."""

    site_id = "presse-economie-gouv-fr"
    site_name = "Custom: presse-economie-gouv-fr"
    base_url = "https://presse.economie.gouv.fr"

    _MAX_PAGES = 200
    _PAGE_SLEEP = 1.0  # seconds between RSS page fetches

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch URL with curl. Returns decoded text or None after retries."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=35
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error on {url} (attempt {attempt+1}): {exc}")
            if attempt < retries - 1:
                wait = (2 ** attempt)  # 1 s, 2 s
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl via RSS feed pages.

        Iterates /search/+/feed/rss2/?paged=N until *limit* is reached,
        no new items appear, or the safety cap of 200 pages is hit.
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, self._MAX_PAGES + 1):
            # 25-minute wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > 25 * 60:
                print(
                    f"[{self.site_id}] Wall-clock budget (25 min) reached "
                    f"at page {page}. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(
                    f"[{self.site_id}] page {page}: "
                    f"saved {saved}/{limit_str}"
                )

            # Fetch RSS page (up to 3 retries with exponential backoff)
            rss_text = None
            for attempt in range(3):
                rss_text = self._curl_get(f"{_RSS_BASE}?paged={page}")
                if rss_text:
                    break
                wait = 3 ** attempt  # 1 s, 3 s, 9 s
                print(
                    f"[{self.site_id}] page {page} fetch failed "
                    f"(attempt {attempt+1}/3), retrying in {wait}s..."
                )
                time.sleep(wait)

            if not rss_text:
                print(
                    f"[{self.site_id}] page {page}: "
                    "failed after 3 attempts. Stopping."
                )
                break

            items = _parse_rss_items(rss_text)

            if not items:
                print(f"[{self.site_id}] page {page}: no items. Done.")
                break

            # Dedup check: if ALL items on this page are already seen → stop
            new_count = sum(
                1 for it in items
                if it.get("link") and it["link"] not in seen_urls
            )
            if new_count == 0:
                print(
                    f"[{self.site_id}] page {page}: "
                    "all items already seen (pagination loop). Done."
                )
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item.get("link", "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                # Per-item failure isolation — one bad item never aborts the run
                try:
                    title = item.get("title", "").strip()
                    if not title:
                        print(
                            f"[{self.site_id}] item {url}: "
                            "no title, skipping"
                        )
                        continue

                    abstract = item.get("abstract", "").strip()
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {url}: "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue

                    post_id = item.get("post_id")
                    published_date = item.get("published_date")

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "post_number": post_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": item.get("author") or None,
                        "publisher": _PUBLISHER,
                        "url": url,
                        "pdf_url": item.get("pdf_url"),
                        "original_filename": item.get("original_filename"),
                        "keywords": item.get("keywords"),
                        "category": item.get("category"),
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": item.get("pub_date"),
                                "release_number": item.get("release_num"),
                                "categories": item.get("categories", []),
                                "post_id": post_id,
                                "guid": item.get("guid"),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_str}: "
                        f"{title[:60]}"
                    )

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            if page == self._MAX_PAGES:
                print(
                    f"[{self.site_id}] Safety cap of {self._MAX_PAGES} "
                    "pages reached. Stopping."
                )

            time.sleep(self._PAGE_SLEEP)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
