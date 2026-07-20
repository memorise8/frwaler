# -*- coding: utf-8 -*-
"""YPEN (Greek Ministry of Environment and Energy) press announcements crawler.

Starting URL: https://ypen.gov.gr/category/anakoinoseis-typou/
Pagination:   /category/anakoinoseis-typou/page/N/
Access:       Firefox UA + --compressed required (Akamai WAF blocks Chrome/Googlebot)
API note:     WP REST API blocked (403); HTML scraping only.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://ypen.gov.gr"
_CATEGORY_URL = f"{_BASE}/category/anakoinoseis-typou/"
_HEADERS = [
    "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "-H", "Accept-Language: el-GR,el;q=0.9,en;q=0.8",
    "-H", "Accept-Encoding: gzip, deflate, br",
    "-H", "DNT: 1",
]

_GREEK_MONTHS = {
    "ιαν": "01", "φεβ": "02", "μάρ": "03", "μαρ": "03",
    "απρ": "04", "μάι": "05", "μαι": "05", "ιούν": "06",
    "ιουν": "06", "ιούλ": "07", "ιουλ": "07", "αύγ": "08",
    "αυγ": "08", "σεπ": "09", "οκτ": "10", "νοε": "11",
    "δεκ": "12",
}


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with Firefox UA, gzip, TLS 1.3, exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--compressed",
        "--max-time", str(timeout),
    ] + _HEADERS + [url]

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip() and "Access Denied" not in raw[:500]:
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[ypen-gov-gr-category] empty/denied for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[ypen-gov-gr-category] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[ypen-gov-gr-category] curl failed after {retries}: {exc}")
    return None


def _parse_iso_date(raw: str) -> str:
    """Extract YYYY-MM-DD from an ISO-8601 datetime string."""
    if not raw:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw.strip())
    return m.group(1) if m else ""


def _parse_badge_date(day: str, month_abbr: str, year_hint: str = "") -> str:
    """Convert Greek badge day/month to YYYY-MM-DD using year_hint if provided."""
    try:
        d = day.strip().zfill(2)
        key = month_abbr.strip().lower()
        m = _GREEK_MONTHS.get(key, "")
        if not m:
            # try first 3 chars
            m = _GREEK_MONTHS.get(key[:3], "")
        if not m or not d:
            return year_hint[:10] if year_hint else ""
        # Year from hint (ISO date like 2026-05-29) or default to current
        y = year_hint[:4] if year_hint and len(year_hint) >= 4 else str(datetime.now().year)
        return f"{y}-{m}-{d}"
    except Exception:
        return year_hint[:10] if year_hint else ""


def _parse_list_page(html: str) -> list[dict]:
    """Extract post items from the category list page."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[ypen-gov-gr-category] list soup error: {exc}")
        return []

    items = []
    # All posts live inside #post-list as .col.post-item divs
    post_list = soup.find(id="post-list")
    if not post_list:
        # Fallback: search whole page
        post_list = soup

    for col in post_list.find_all("div", class_="post-item"):
        try:
            title_tag = col.find("h5", class_="post-title")
            if not title_tag:
                continue
            a = title_tag.find("a")
            if not a:
                continue
            url = a.get("href", "").strip()
            if not url:
                continue
            if not url.startswith("http"):
                url = _BASE + url
            title = a.get_text(strip=True)
            if not title:
                continue

            # Short excerpt from list
            excerpt_el = col.find(class_="from_the_blog_excerpt")
            excerpt = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""
            # Strip trailing "[Διαβάστε περισσότερα]" etc.
            excerpt = re.sub(r"\[.*?\]$", "", excerpt).strip()

            # Date badge: day + month (no year on list page)
            day_el = col.find(class_="post-date-day")
            month_el = col.find(class_="post-date-month")
            badge_day = day_el.get_text(strip=True) if day_el else ""
            badge_month = month_el.get_text(strip=True) if month_el else ""

            items.append({
                "title": title,
                "url": url,
                "excerpt": excerpt,
                "badge_day": badge_day,
                "badge_month": badge_month,
            })
        except Exception as exc:
            print(f"[ypen-gov-gr-category] list row error: {exc}")
            continue

    return items


def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract post_id, date, full content text, pdf_url from a detail page."""
    post_id = ""
    published_date = ""
    abstract = ""
    pdf_url = ""
    original_filename = ""

    try:
        soup = _make_soup(html)

        # WP post numeric ID from body class or element id
        body = soup.find("body")
        if body:
            body_cls = " ".join(body.get("class", []))
            m = re.search(r"postid-(\d+)", body_cls)
            if m:
                post_id = m.group(1)

        if not post_id:
            post_el = soup.find(id=re.compile(r"^post-\d+$"))
            if post_el:
                post_id = re.sub(r"^post-", "", post_el.get("id", ""))

        # Date from <time> or JSON-LD
        time_el = soup.find("time", datetime=True)
        if time_el:
            published_date = _parse_iso_date(time_el["datetime"])

        if not published_date:
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
            if m:
                published_date = _parse_iso_date(m.group(1))

        # Content from .entry-content
        content_el = (
            soup.find("div", class_="entry-content")
            or soup.find("div", class_="post-content")
            or soup.find("div", class_=re.compile(r"entry-content"))
        )

        if content_el:
            # Remove share buttons, social icons, navigation cruft
            for tag in content_el.find_all(
                ["nav", "script", "style", "form", "aside",
                 "div"], class_=re.compile(r"social|share|nav|sidebar|widget")
            ):
                tag.decompose()

            # Find PDF links before stripping tags
            for a in content_el.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    if not href.startswith("http"):
                        href = _BASE + href
                    pdf_url = href
                    path_seg = urlparse(href).path.split("/")[-1]
                    try:
                        original_filename = path_seg.encode("latin-1").decode("utf-8")
                    except Exception:
                        original_filename = path_seg
                    break

            # Collect text from paragraphs/headings
            parts = []
            for el in content_el.find_all(["p", "li", "h1", "h2", "h3", "h4", "td", "th"]):
                t = el.get_text(" ", strip=True)
                if t and len(t) > 15:
                    parts.append(t)
            if parts:
                abstract = " ".join(parts)
            else:
                abstract = content_el.get_text(" ", strip=True)

            abstract = re.sub(r"\s+", " ", abstract).strip()
        else:
            # Fallback: use body text stripped of nav/script
            for tag in (soup.find_all(["script", "style", "nav", "header", "footer"])):
                tag.decompose()
            abstract = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()[:3000]

    except Exception as exc:
        print(f"[ypen-gov-gr-category] detail parse error ({page_url}): {exc}")
        # Regex fallback
        try:
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
            if m:
                published_date = _parse_iso_date(m.group(1))
            m2 = re.search(r'id="post-(\d+)"', html)
            if m2:
                post_id = m2.group(1)
            abstract = re.sub(r"<[^>]+>", " ", html)
            abstract = re.sub(r"\s+", " ", abstract).strip()[:3000]
        except Exception:
            pass

    return {
        "post_id": post_id,
        "published_date": published_date,
        "abstract": abstract,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
    }


def _extract_slug(url: str) -> str:
    """Extract the slug portion from a ypen.gov.gr post URL."""
    path = urlparse(url).path.strip("/")
    return path or url


class YpenGovGrCategoryCrawler(BaseCrawler):
    """Crawler for YPEN (Greek Ministry of Environment and Energy) press announcements."""

    site_id = "ypen-gov-gr-category"
    site_name = "Custom: ypen-gov-gr-category"
    base_url = "https://ypen.gov.gr"

    def crawl(self, limit=None):
        """Crawl /category/anakoinoseis-typou/ with full pagination.

        Walks pages until saved >= limit, page returns 0 new records,
        or 200-page safety cap is reached. Fetches each detail page for
        full content, post ID, and ISO date.
        """
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        max_wall = 25 * 60  # 25-minute budget
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(1, max_pages + 1):
            # Wall-clock budget check
            if time.time() - crawl_start > max_wall:
                print(f"[ypen-gov-gr-category] 25-minute wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num == 1:
                list_url = _CATEGORY_URL
            else:
                list_url = f"{_CATEGORY_URL}page/{page_num}/"

            raw = _curl_get(list_url)
            if not raw:
                print(f"[ypen-gov-gr-category] page {page_num}: failed to fetch, stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[ypen-gov-gr-category] page {page_num}: no items found. Done.")
                break

            if page_num % 10 == 0:
                print(f"[ypen-gov-gr-category] page {page_num}: saved {saved}/{limit_display}")

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - crawl_start > max_wall:
                    break

                item_url = item["url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)

                    detail_html = _curl_get(item_url)
                    if not detail_html:
                        print(f"[ypen-gov-gr-category] detail fetch failed: {item_url}")
                        continue

                    detail = _parse_detail_page(detail_html, item_url)

                    abstract = detail["abstract"]
                    if len(abstract) < 50:
                        print(f"[ypen-gov-gr-category] skipping short abstract "
                              f"({len(abstract)} chars): {item['title'][:60]}")
                        continue

                    published_date = detail["published_date"]
                    # Build badge date with year from detail ISO date
                    listed_date = _parse_badge_date(
                        item["badge_day"], item["badge_month"], published_date
                    ) or published_date

                    slug = _extract_slug(item_url)
                    post_id = detail["post_id"]
                    # external_id: prefer numeric WP post ID, fall back to slug
                    external_id = post_id if post_id else slug

                    pdf_url = detail["pdf_url"] or ""
                    original_filename = detail["original_filename"] or ""

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_id if post_id else None,
                        "title": item["title"],
                        "abstract": abstract,
                        "authors": "",
                        "publisher": "Υπουργείο Περιβάλλοντος και Ενέργειας",
                        "department": "",
                        "journal": "",
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "keywords": "",
                        "category": "Ανακοινώσεις Τύπου",
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "originalFilename": original_filename,
                            "slug": slug,
                            "wp_post_id": post_id,
                            "badge_day": item["badge_day"],
                            "badge_month": item["badge_month"],
                            "list_excerpt": item["excerpt"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[ypen-gov-gr-category] saved {saved}/{limit_display}: "
                          f"{item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ypen-gov-gr-category] item failed ({item_url}): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[ypen-gov-gr-category] page {page_num}: all items already seen. Done.")
                break

        if page_num >= max_pages:
            print(f"[ypen-gov-gr-category] safety cap of {max_pages} pages reached.")

        print(f"[ypen-gov-gr-category] Done. Total saved: {saved}")
        return saved
