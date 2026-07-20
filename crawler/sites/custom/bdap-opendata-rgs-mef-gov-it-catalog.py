# -*- coding: utf-8 -*-
"""Crawler for BDAP Open Data catalog (Italian MEF / Ragioneria Generale dello Stato).

Target: https://bdap-opendata.rgs.mef.gov.it/catalog

Structure (Drupal 7 + spodata module):
  - List pages: /catalog?page=N (0-indexed), 10 items per page, ~363 pages.
    Out-of-range pages silently repeat the last valid page's items (no error),
    so URL de-duplication is what actually detects the end of pagination.
  - Each list item: <li class="metadata-search-result"> with title link,
    truncated description snippet, source org, creation date (DD/MM/YYYY),
    and theme/category.
  - Detail pages: /content/<slug> — node id embedded in <body class="... page-node-<NID> ...">,
    full description, creation/last-update dates (DD/Mon/YYYY, Italian abbrev.
    months), keywords, publisher, and an optional PDF attachment under "Allegati".
"""

import json
import re
import subprocess
import sys
import os
import time
from datetime import datetime

# Allow absolute import from project root (spec_from_file_location has no package context)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False


_ITALIAN_MONTHS = {
    "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
    "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
}


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_it_date(raw: str):
    """Convert DD/MM/YYYY or DD/Mon/YYYY (Italian abbrev month) to YYYY-MM-DD.

    Returns None if the string can't be parsed.
    """
    if not raw:
        return None
    s = raw.strip()
    try:
        return datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})/([A-Za-z]{3})/(\d{4})$", s)
    if m:
        day, mon_abbr, year = m.groups()
        mon = _ITALIAN_MONTHS.get(mon_abbr.lower())
        if mon:
            try:
                return datetime(int(year), mon, int(day)).strftime("%Y-%m-%d")
            except ValueError:
                return None
    return None


class BdapOpendataRgsMefGovItCatalogCrawler(BaseCrawler):
    """Crawler for the BDAP Open Data catalog."""

    site_id = "bdap-opendata-rgs-mef-gov-it-catalog"
    site_name = "Custom: bdap-opendata-rgs-mef-gov-it-catalog"
    base_url = "https://bdap-opendata.rgs.mef.gov.it"

    _LIST_URL = "https://bdap-opendata.rgs.mef.gov.it/catalog"
    _PAGE_CAP = 200        # safety: never fetch more than 200 listing pages
    _MAX_WALL = 25 * 60    # 25-minute wall-clock budget
    _MIN_ABSTRACT_LEN = 50

    # ------------------------------------------------------------------
    # Low-level fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_html(raw: bytes) -> str:
        """Decode response bytes, sniffing encoding to avoid mojibake."""
        for enc in ("utf-8", "windows-1252", "iso-8859-1"):
            try:
                html_text = raw.decode(enc)
                if "�" not in html_text:
                    return html_text
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _curl(self, url: str, retries: int = 3):
        """GET via curl with TLS workaround and exponential backoff (1s, 3s, 9s)."""
        for attempt in range(retries):
            if attempt > 0:
                wait = [1, 3, 9][min(attempt - 1, 2)]
                print(f"[{self.site_id}] Retry {attempt}/{retries - 1} in {wait}s: {url}")
                time.sleep(wait)
            try:
                result = subprocess.run(
                    [
                        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30", "-L",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if raw and raw.strip():
                    return self._decode_html(raw)
                print(f"[{self.site_id}] Empty response (attempt {attempt + 1}/{retries}): {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_listing(self, html: str) -> list:
        """Return list of {url, title, snippet, source, category, creation_raw} dicts."""
        try:
            soup = _make_soup(html)
            if not soup:
                return []
            items = []
            for li in soup.find_all("li", class_=lambda c: c and "metadata-search-result" in c.split()):
                h3 = li.find("h3", class_="title")
                a = h3.find("a", href=True) if h3 else None
                if not a:
                    continue
                url = a["href"].strip()
                if url.startswith("/"):
                    url = self.base_url + url
                title = a.get_text(separator=" ", strip=True)
                title = " ".join(title.split())
                if not url or not title:
                    continue

                snippet_tag = li.find("p", class_="search-snippet")
                snippet = snippet_tag.get_text(separator=" ", strip=True) if snippet_tag else ""

                source = ""
                author_tag = li.find(attrs={"itemprop": "author"})
                if author_tag:
                    name_tag = author_tag.find(attrs={"itemprop": "name"})
                    if name_tag:
                        source = name_tag.get_text(strip=True)

                creation_raw = ""
                cd = li.find("span", id="creationdate")
                if cd:
                    creation_raw = cd.get_text(strip=True)

                categories = []
                for term in li.find_all("div", class_="taxonomy-result-term"):
                    for cat_a in term.find_all("a"):
                        txt = cat_a.get_text(strip=True)
                        if txt:
                            categories.append(txt)

                items.append({
                    "url": url,
                    "title": title,
                    "snippet": snippet,
                    "source": source,
                    "category": "; ".join(categories) if categories else None,
                    "creation_raw": creation_raw,
                })
            return items
        except Exception as exc:
            print(f"[{self.site_id}] Listing parse error: {exc}")
            return []

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str) -> dict:
        """Extract full metadata from a dataset detail page."""
        out = {
            "node_id": None,
            "title": "",
            "abstract": "",
            "creationdate_raw": "",
            "lastupdate_raw": "",
            "keywords": [],
            "category": None,
            "source": "",
            "publisher": "",
            "pdf_url": None,
            "original_filename": None,
            "csv_download_url": None,
            "datasource": None,
            "license": None,
        }
        try:
            soup = _make_soup(html)
            if not soup:
                return out

            body = soup.find("body")
            if body:
                body_class = " ".join(body.get("class", []))
                m = re.search(r"page-node-(\d+)", body_class)
                if m:
                    out["node_id"] = m.group(1)

            main_div = soup.find("div", id="maind")
            if main_div:
                name_span = main_div.find("span", itemprop="name")
                if name_span:
                    out["title"] = " ".join(name_span.get_text(separator=" ", strip=True).split())

            body_field = soup.find("div", class_=lambda c: c and "field-name-body" in c.split())
            if body_field:
                item_div = body_field.find("div", class_=lambda c: c and "field-item" in c.split())
                if item_div:
                    p = item_div.find("p")
                    if p:
                        out["abstract"] = p.get_text(separator=" ", strip=True)
                    else:
                        out["abstract"] = item_div.get_text(separator=" ", strip=True)

            cd = soup.find("span", id="creationdate")
            if cd:
                out["creationdate_raw"] = cd.get_text(strip=True)
            lu = soup.find("span", id="lastupdate")
            if lu:
                out["lastupdate_raw"] = lu.get_text(strip=True)

            kw_field = soup.find("div", class_=lambda c: c and "field-name-metadata-type" in c.split())
            if kw_field:
                out["keywords"] = [a.get_text(strip=True) for a in kw_field.find_all("a") if a.get_text(strip=True)]

            cat_field = soup.find("div", class_=lambda c: c and "field-name-metadata-category" in c.split())
            if cat_field:
                cats = [a.get_text(strip=True) for a in cat_field.find_all("a") if a.get_text(strip=True)]
                if cats:
                    out["category"] = "; ".join(cats)

            author_field = soup.find(attrs={"itemprop": "author"})
            if author_field:
                name_tag = author_field.find(id="field-author-label") or author_field.find(attrs={"itemprop": "name"})
                if name_tag:
                    out["source"] = name_tag.get_text(strip=True)

            pub_div = soup.find("div", class_="metadata-publisher")
            if pub_div:
                p_a = pub_div.find("a")
                if p_a:
                    out["publisher"] = p_a.get_text(strip=True)

            attach_div = soup.find("div", class_=lambda c: c and "extra-attach-link" in c.split())
            if attach_div:
                a_tag = attach_div.find("a", href=True)
                if a_tag:
                    href = a_tag["href"].strip()
                    lower = href.lower()
                    if ".pdf" in lower:
                        out["pdf_url"] = href
                        from urllib.parse import unquote
                        fname = unquote(href.rstrip("/").split("/")[-1].split("?")[0])
                        if fname:
                            out["original_filename"] = fname

            for li in soup.find_all("li", id="Scarica"):
                a_tag = li.find("a", href=True)
                if a_tag:
                    out["csv_download_url"] = a_tag["href"].strip()
                    break

            ds_field = soup.find("div", class_="field-datasource")
            if ds_field:
                ds_a = ds_field.find("a")
                if ds_a:
                    out["datasource"] = ds_a.get_text(strip=True)

            lic_img = soup.find("div", class_=lambda c: c and "field-name-metadata-license" in c.split())
            if lic_img:
                img = lic_img.find("img")
                if img and img.get("alt"):
                    out["license"] = img["alt"].strip()

        except Exception as exc:
            print(f"[{self.site_id}] BS4 detail parse error for {url}: {exc}")
        return out

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the BDAP Open Data catalog page by page (list -> detail)."""
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "inf"
        start_time = time.time()

        for page in range(self._PAGE_CAP):
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed > self._MAX_WALL:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping.")
                break
            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")
            if page == self._PAGE_CAP - 1:
                print(f"[{self.site_id}] Safety cap of {self._PAGE_CAP} pages reached.")

            list_url = f"{self._LIST_URL}?page={page}"
            html = self._curl(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            items = self._parse_listing(html)
            if not items:
                print(f"[{self.site_id}] Page {page}: no items found. Stopping.")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] Page {page}: all items already seen (pagination looped). Stopping.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget reached mid-page.")
                    break

                url = item["url"]
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl(url)
                    if not detail_html:
                        print(f"[{self.site_id}] Failed to fetch detail: {url}")
                        continue

                    detail = self._parse_detail(detail_html, url)

                    title = detail["title"] or item["title"]
                    if not title:
                        print(f"[{self.site_id}] No title at {url}, skipping.")
                        continue

                    abstract = detail["abstract"] or item["snippet"]
                    if not abstract or len(abstract) < self._MIN_ABSTRACT_LEN:
                        print(f"[{self.site_id}] Short abstract ({len(abstract)} chars) at {url}, skipping.")
                        continue

                    node_id = detail["node_id"]
                    post_number = node_id if node_id else url.rstrip("/").split("/")[-1]

                    published_raw = detail["creationdate_raw"] or item["creation_raw"]
                    published_date = _parse_it_date(published_raw)
                    listed_raw = detail["lastupdate_raw"] or published_raw
                    listed_date = _parse_it_date(listed_raw) or published_date

                    keywords = detail["keywords"]
                    category = detail["category"] or item["category"]
                    source = detail["source"] or item["source"]

                    metadata = {
                        "posted_date": listed_raw,
                        "originalFilename": detail["original_filename"],
                        "node_id": node_id,
                        "creationdate_raw": detail["creationdate_raw"] or item["creation_raw"],
                        "lastupdate_raw": detail["lastupdate_raw"],
                        "keywords_raw": keywords,
                        "datasource": detail["datasource"],
                        "license": detail["license"],
                        "csv_download_url": detail["csv_download_url"],
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_number,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "authors": source or None,
                        "publisher": detail["publisher"] or None,
                        "department": None,
                        "journal": None,
                        "keywords": ", ".join(keywords) if keywords else None,
                        "category": category,
                        "doi": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item {url} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
