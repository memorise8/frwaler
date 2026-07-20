# -*- coding: utf-8 -*-
"""Crawler for AMF France (Autorité des marchés financiers) communiqués.

Sources:
  - Sitemap: /sitemap.xml  (753 communiques-de-lamf/ URLs with lastmod)
  - Detail pages: each article page for full content
"""

import json
import re
import subprocess
import sys
import time
import urllib.parse
from xml.etree import ElementTree as ET

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BS4_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(raw_html):
    """Parse HTML with html5lib → lxml → html.parser fallback."""
    from bs4 import BeautifulSoup

    for parser in _BS4_PARSERS:
        try:
            return BeautifulSoup(raw_html, parser)
        except Exception:
            continue
    raise RuntimeError("All BeautifulSoup parsers failed")


def _curl_get(url, retries=3, timeout=30):
    """Fetch URL with curl; returns (text, headers_dict) or (None, None) on failure."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--tls-max", "1.3",
                    "-sk",
                    "-L",
                    "--max-time", str(timeout),
                    "-A",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "-H", "Accept-Language: fr-FR,fr;q=0.9",
                    "-D", "-",  # dump headers to stdout
                    url,
                ],
                capture_output=True,
                timeout=timeout + 5,
            )
            raw = result.stdout
            # Split headers from body
            parts = raw.split(b"\r\n\r\n", 1)
            if len(parts) == 2:
                body = parts[1]
            else:
                body = raw
            text = body.decode("utf-8", errors="replace")
            if text:
                return text, {}
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[amf-france-org-fr] empty response (attempt {attempt+1}/{retries}), retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[amf-france-org-fr] curl error {exc} (attempt {attempt+1}/{retries}), retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[amf-france-org-fr] curl failed after {retries} attempts for {url}: {exc}")
    return None, None


def _parse_amf_date(raw):
    """Parse AMF data-published-date like '202605281159' → 'YYYY-MM-DD'."""
    if not raw:
        return None
    raw = re.sub(r"[^0-9]", "", raw)
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _parse_sitemap_date(raw):
    """Parse lastmod '2023-11-21T17:04Z' → '2023-11-21'."""
    if not raw:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else None


def _extract_article(html, url):
    """Parse an AMF article detail page and return a dict of fields."""
    soup = _make_soup(html)

    # ── Node metadata from body data-* attributes ──
    body = soup.find("body")
    node_id = body.get("data-nid") if body else None
    raw_date = body.get("data-published-date") if body else None
    data_format = body.get("data-format", "")
    data_sujet = body.get("data-sujet", "")

    published_date = _parse_amf_date(raw_date)

    # ── Title ──
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if not title:
        og_title = soup.find("meta", {"property": "og:title"})
        title = og_title.get("content", "").strip() if og_title else None

    # ── Abstract / body text ──
    # Primary: .contentToc div contains full article text
    abstract = ""
    content_toc = soup.find("div", class_="contentToc")
    if content_toc:
        abstract = content_toc.get_text(separator=" ", strip=True)

    # Fallback: collect intro + wysiwyg paragraphs
    if len(abstract) < 100:
        parts = []
        intro_p = soup.find("p", class_="intro")
        if intro_p:
            parts.append(intro_p.get_text(separator=" ", strip=True))
        for para in soup.find_all("div", class_=re.compile(r"paragraph--type--wysiwyg")):
            parts.append(para.get_text(separator=" ", strip=True))
        if parts:
            abstract = " ".join(parts)

    # Fallback 2: og:description meta
    if len(abstract) < 100:
        og_desc = soup.find("meta", {"property": "og:description"})
        if og_desc:
            abstract = og_desc.get("content", "").strip()

    # ── PDF URL ──
    pdf_url = None
    original_filename = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower():
            pdf_url = href if href.startswith("http") else f"https://www.amf-france.org{href}"
            # Extract filename from URL path (before query string)
            path_part = urllib.parse.urlparse(pdf_url).path
            original_filename = path_part.rstrip("/").split("/")[-1]
            original_filename = urllib.parse.unquote(original_filename)
            break

    # ── Tags / keywords / category ──
    tags = []
    for tag_el in soup.find_all(class_="tag"):
        t = tag_el.get_text(strip=True)
        if t and t not in tags:
            tags.append(t)

    category = data_format.replace("-", " ").strip() if data_format else None
    if tags:
        category = tags[0]
    keywords = ",".join(tags[1:]) if len(tags) > 1 else None

    # ── Metadata ──
    slug = url.rstrip("/").split("/")[-1]
    meta = {
        "node_id": node_id,
        "data_format": data_format,
        "data_sujet": data_sujet,
        "slug": slug,
        "posted_date": raw_date,
        "originalFilename": original_filename,
    }

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "category": category,
        "keywords": keywords,
        "post_number": node_id,
        "external_id": node_id or slug,
        "metadata": json.dumps(meta, ensure_ascii=False),
        "_tags": tags,
    }


class AMFFranceOrgFrCrawler(BaseCrawler):
    """Crawler for https://www.amf-france.org communiqués de l'AMF."""

    site_id = "amf-france-org-fr"
    site_name = "Custom: amf-france-org-fr"
    base_url = "https://www.amf-france.org"

    SITEMAP_URL = "https://www.amf-france.org/sitemap.xml"
    TARGET_PATH_PREFIX = "/fr/actualites-publications/communiques/communiques-de-lamf/"
    RATE_SLEEP = 1.0
    MAX_PAGES = 200  # safety cap (one page = one article here)
    WALL_CLOCK_LIMIT = 25 * 60  # 25 minutes in seconds

    def _fetch_sitemap_urls(self):
        """Return list of (url, lastmod) for communiques-de-lamf, newest first."""
        print("[amf-france-org-fr] Fetching sitemap …")
        text, _ = _curl_get(self.SITEMAP_URL)
        if not text:
            print("[amf-france-org-fr] Sitemap fetch failed")
            return []

        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            print(f"[amf-france-org-fr] Sitemap XML parse error: {exc}")
            return []

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        entries = []
        for url_el in root.findall("sm:url", ns):
            loc_el = url_el.find("sm:loc", ns)
            lastmod_el = url_el.find("sm:lastmod", ns)
            if loc_el is None:
                continue
            loc = loc_el.text or ""
            lastmod = _parse_sitemap_date(lastmod_el.text) if lastmod_el is not None else None
            if self.TARGET_PATH_PREFIX in loc:
                entries.append((loc, lastmod))

        # Sort newest first (by lastmod, None → oldest)
        entries.sort(key=lambda x: x[1] or "0000-00-00", reverse=True)
        print(f"[amf-france-org-fr] Sitemap: {len(entries)} communiques-de-lamf URLs")
        return entries

    def crawl(self, limit=None):
        """Crawl AMF France communiqués and save to DB.

        Uses sitemap for URL discovery, visits each detail page for content.
        Returns number of records saved.
        """
        import time as _time

        start_time = _time.monotonic()
        limit_or_inf = limit if limit is not None else float("inf")

        sitemap_entries = self._fetch_sitemap_urls()
        if not sitemap_entries:
            print("[amf-france-org-fr] No URLs found in sitemap, aborting")
            return 0

        seen_urls = set()
        saved = 0
        pages_walked = 0

        try:
            for idx, (article_url, lastmod) in enumerate(sitemap_entries):
                # ── Limit checks ──
                if saved >= limit_or_inf:
                    break
                if pages_walked >= self.MAX_PAGES:
                    print(f"[amf-france-org-fr] Safety cap of {self.MAX_PAGES} pages reached, stopping")
                    break

                # ── Wall-clock budget ──
                elapsed = _time.monotonic() - start_time
                if elapsed > self.WALL_CLOCK_LIMIT:
                    print(f"[amf-france-org-fr] Wall-clock limit ({self.WALL_CLOCK_LIMIT}s) reached, stopping")
                    break

                # ── URL deduplication ──
                canonical = article_url.split("?")[0].split("#")[0].rstrip("/")
                if canonical in seen_urls:
                    continue
                seen_urls.add(canonical)

                pages_walked += 1

                # ── Progress logging ──
                if pages_walked % 10 == 0:
                    print(f"[amf-france-org-fr] page {pages_walked}: saved {saved}/{limit_or_inf}")

                # ── Per-item fetch + parse + save ──
                try:
                    time.sleep(self.RATE_SLEEP)
                    html, _ = _curl_get(article_url)
                    if not html:
                        print(f"[amf-france-org-fr] item {article_url} failed: empty response")
                        continue

                    fields = _extract_article(html, article_url)

                    title = fields.get("title") or ""
                    abstract = fields.get("abstract") or ""

                    if not title:
                        print(f"[amf-france-org-fr] item {article_url} skipped: no title")
                        continue

                    if len(abstract) < 50:
                        print(f"[amf-france-org-fr] item {article_url} skipped: abstract too short ({len(abstract)} chars)")
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": fields.get("external_id"),
                        "post_number": fields.get("post_number"),
                        "url": canonical,
                        "title": title,
                        "abstract": abstract,
                        "published_date": fields.get("published_date") or lastmod,
                        "listed_date": lastmod,
                        "authors": None,
                        "publisher": "AMF - Autorité des marchés financiers",
                        "department": None,
                        "journal": None,
                        "pdf_url": fields.get("pdf_url"),
                        "original_filename": fields.get("original_filename"),
                        "keywords": fields.get("keywords"),
                        "category": fields.get("category"),
                        "doi": None,
                        "metadata": fields.get("metadata"),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[amf-france-org-fr] item {article_url} failed: {exc}")
                    continue

        except KeyboardInterrupt:
            print(f"[amf-france-org-fr] Interrupted by user after {saved} saved")

        print(f"[amf-france-org-fr] Done: {saved} records saved")
        return saved
