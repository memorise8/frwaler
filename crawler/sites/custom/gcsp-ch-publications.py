# -*- coding: utf-8 -*-
"""Crawler for GCSP Publications (gcsp.ch) — Geneva Papers, Policy Briefs, etc."""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.gcsp.ch"
_LIST_URL = _BASE + "/publications"
_LIST_PARAMS = "type%5B230%5D=230&type%5B879%5D=879&type%5B600%5D=600&type%5B675%5D=675"

_MONTH_MAP = {
    "January": "01", "February": "02", "March": "03", "April": "04",
    "May": "05", "June": "06", "July": "07", "August": "08",
    "September": "09", "October": "10", "November": "11", "December": "12",
}

# BeautifulSoup parser fallback chain
def _make_soup(html):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    if not html:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html_fragment):
    text = re.sub(r"<[^>]+>", " ", html_fragment or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw):
    """Convert '23 April 2026' → '2026-04-23'. Returns raw on failure."""
    raw = raw.strip()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if m:
        day, month_name, year = m.groups()
        mon = _MONTH_MAP.get(month_name, "01")
        return f"{year}-{mon}-{int(day):02d}"
    return raw


class GCSPPublicationsCrawler(BaseCrawler):
    site_id = "gcsp-ch-publications"
    site_name = "Custom: gcsp-ch-publications"
    base_url = _BASE

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """GET via curl with retries and exponential backoff. Returns str or None."""
        waits = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                        "-A", self.USER_AGENT,
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if raw:
                    return raw.decode("utf-8", errors="replace")
            except Exception as exc:
                if attempt < retries - 1:
                    wait = waits[attempt]
                    print(f"[gcsp-ch-publications] curl error attempt {attempt+1}: {exc}, retrying in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[gcsp-ch-publications] curl failed after {retries} attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------

    def _get_links_on_page(self, page_num):
        """Return list of /publications/slug paths from listing page N (0-indexed)."""
        url = f"{_LIST_URL}?{_LIST_PARAMS}&page={page_num}"
        html = self._curl_get(url)
        if not html:
            return []

        soup = _make_soup(html)
        if soup:
            links = []
            for card in soup.find_all(class_="card-publications-teaser"):
                a = card.find("a", href=True)
                if a and a["href"].startswith("/publications/"):
                    links.append(a["href"])
            return links

        # Regex fallback
        return re.findall(r'href="(/publications/[^"?#]+)"', html)

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, url):
        """Fetch and parse a publication detail page. Returns dict or None."""
        html = self._curl_get(url)
        if not html:
            return None

        soup = _make_soup(html)

        # --- Title ---
        title = ""
        if soup:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(separator=" ", strip=True)
        if not title:
            m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            if m:
                title = _strip_tags(m.group(1))

        # --- External ID (Drupal node ID) ---
        external_id = ""
        m = re.search(r'"currentPath":"node/(\d+)"', html)
        if m:
            external_id = m.group(1)

        # --- Published date ---
        published_date = ""
        if soup:
            date_div = soup.find(class_="publications-date")
            if date_div:
                published_date = _parse_date(date_div.get_text(separator=" ", strip=True))
        if not published_date:
            m = re.search(r'publications-date[^>]*>\s*([^<\r\n]+)', html)
            if m:
                published_date = _parse_date(m.group(1).strip())

        # --- Authors (from card-expert-teaser .title divs) ---
        authors = []
        if soup:
            experts_div = soup.find(class_="publications-inner-content experts")
            if experts_div:
                for t in experts_div.find_all(class_="title"):
                    name = t.get_text(separator=" ", strip=True)
                    if name and name not in authors:
                        authors.append(name)
        if not authors:
            # Fallback: parse from og:description "by Author1, Author2"
            m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
            if m:
                by_m = re.search(r" by (.{3,150}?)(?:$|[.!\n])", m.group(1))
                if by_m:
                    authors = [a.strip() for a in by_m.group(1).split(",") if a.strip()]

        # --- Category (from og:description prefix, e.g. "Geneva Paper") ---
        category = ""
        og_desc = ""
        m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
        if m:
            og_desc = m.group(1)
            cat_m = re.match(r"^([A-Za-z ]+\d)", og_desc)
            if cat_m:
                category = re.sub(r"\s*\d.*", "", cat_m.group(1)).strip()

        # --- PDF URL ---
        pdf_url = ""
        if soup:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/sites/default/files/" in href and href.lower().endswith(".pdf"):
                    pdf_url = _BASE + href if href.startswith("/") else href
                    break
        if not pdf_url:
            m = re.search(r'href="(/sites/default/files/[^"]+\.pdf)"', html)
            if m:
                pdf_url = _BASE + m.group(1)

        # --- Abstract from div.basic-page ---
        abstract = ""
        if soup:
            basic = soup.find(class_="basic-page")
            if basic:
                # Remove disclaimer blocks
                for tag in basic.find_all(class_=re.compile(r"disclaimer")):
                    tag.decompose()
                for tag in basic.find_all(["script", "style"]):
                    tag.decompose()
                abstract = basic.get_text(separator=" ", strip=True)
                # Normalise whitespace
                abstract = re.sub(r"\s+", " ", abstract).strip()

        if not abstract:
            # Regex fallback: text inside basic-page until disclaimer
            m = re.search(
                r'class="basic-page">(.*?)(?:publications-disclaimer|GCSP is not responsible)',
                html, re.DOTALL
            )
            if m:
                abstract = _strip_tags(m.group(1))

        return {
            "title": title,
            "external_id": external_id,
            "published_date": published_date,
            "authors": authors,
            "category": category,
            "pdf_url": pdf_url,
            "abstract": abstract,
            "og_desc": og_desc,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        max_seconds = 25 * 60  # 25-minute wall-clock budget

        limit_display = str(limit) if limit is not None else "inf"

        for page_num in range(200):
            # Wall-clock budget
            if time.time() - start_time > max_seconds:
                print(f"[gcsp-ch-publications] 25-minute budget reached at page {page_num}. Stopping.")
                break

            # Limit already met
            if limit is not None and saved >= limit:
                break

            # Safety cap log
            if page_num == 199:
                print("[gcsp-ch-publications] Safety cap of 200 pages reached. Stopping.")

            # Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                print(f"[gcsp-ch-publications] page {page_num}: saved {saved}/{limit_display}")

            links = self._get_links_on_page(page_num)

            if not links:
                print(f"[gcsp-ch-publications] No links on page {page_num}. Done.")
                break

            new_links = [l for l in links if l not in seen_urls]
            if not new_links:
                print(f"[gcsp-ch-publications] All links on page {page_num} already seen. Done.")
                break
            seen_urls.update(new_links)

            for slug in new_links:
                if limit is not None and saved >= limit:
                    break

                if time.time() - start_time > max_seconds:
                    print("[gcsp-ch-publications] Budget reached mid-page. Stopping.")
                    break

                detail_url = _BASE + slug

                try:
                    time.sleep(self._delay)

                    # Retry detail fetch up to 3 times
                    detail = None
                    for attempt in range(3):
                        detail = self._parse_detail(detail_url)
                        if detail:
                            break
                        wait = [1, 3, 9][attempt]
                        print(f"[gcsp-ch-publications] detail fetch attempt {attempt+1} failed for {slug}, retrying in {wait}s")
                        time.sleep(wait)

                    if not detail:
                        print(f"[gcsp-ch-publications] item {slug} failed: no detail after 3 retries")
                        continue

                    abstract = detail.get("abstract", "")
                    if len(abstract) < 50:
                        print(f"[gcsp-ch-publications] Skipping {slug}: abstract too short ({len(abstract)} chars)")
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": detail["external_id"] or slug,
                        "title": detail["title"],
                        "authors": json.dumps(detail["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": detail.get("category", ""),
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": detail.get("published_date", ""),
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url", ""),
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps(
                            {"og_desc": detail.get("og_desc", ""), "slug": slug},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[gcsp-ch-publications] Saved {saved}/{limit_display}: {detail['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[gcsp-ch-publications] item {slug} failed: {exc}")
                    continue

        print(f"[gcsp-ch-publications] Done. Total saved: {saved}")
        return saved
