# -*- coding: utf-8 -*-
"""FAFO (Forskningsstiftelsen Fafo) English publications crawler.

Target: https://www.fafo.no/en/publications
Covers: fafo-reports, fafo-notes, briefs, other-fafo-publications
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.fafo.no"

_CATEGORIES = [
    "fafo-reports",
    "fafo-notes",
    "briefs",
    "other-fafo-publications",
]

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _curl_get(url, retries=3):
    """Fetch URL via curl with retries. Returns decoded text or None."""
    cmd = ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
           "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           url]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2  # 1s, 4s, 9s
                print(f"[fafo-no-en] curl error (attempt {attempt + 1}/{retries}): {exc}")
                time.sleep(wait)
    return None


def _make_soup(html):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    if not html:
        return None
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date_text(text):
    """Parse 'Day Month YYYY' or 'Month YYYY' → 'YYYY-MM-DD'."""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if m:
        day, mon, year = m.group(1), m.group(2).lower(), m.group(3)
        mm = _MONTH_MAP.get(mon)
        if mm:
            return f"{year}-{mm}-{day.zfill(2)}"
    m2 = re.search(r"(\w+)\s+(\d{4})", text)
    if m2:
        mon, year = m2.group(1).lower(), m2.group(2)
        mm = _MONTH_MAP.get(mon)
        if mm:
            return f"{year}-{mm}-01"
    return None


def _strip_tags(html_text):
    """Strip HTML tags and normalize whitespace."""
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


class FafoNoEnCrawler(BaseCrawler):
    """Crawler for FAFO (Forskningsstiftelsen Fafo) English publications."""

    site_id = "fafo-no-en"
    site_name = "Custom: fafo-no-en"
    base_url = "https://www.fafo.no"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_html(self, url):
        """Return (soup, raw_html) or (None, None) on failure."""
        raw = _curl_get(url)
        if not raw:
            return None, None
        soup = _make_soup(raw)
        return soup, raw

    def _extract_item_urls(self, soup, category):
        """Return unique detail-page URLs from a listing page."""
        urls = []
        seen = set()
        if soup is None:
            return urls
        prefix = f"/en/publications/{category}/"
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith(prefix):
                continue
            slug = href[len(prefix):]
            # Skip numeric slugs (pagination) and nested paths
            if not slug or slug.isdigit() or "/" in slug:
                continue
            full = _BASE + href
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

    def _parse_detail(self, url, category):
        """Fetch and parse one publication detail page.

        Returns a paper dict suitable for _save_paper(), or None on failure.
        """
        soup, raw = self._fetch_html(url)
        if soup is None or raw is None:
            return None

        # ---- Title ----
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
        if not title:
            t = soup.find("title")
            if t:
                title = t.get_text(strip=True)
        title = re.sub(r"\s+", " ", title).strip()

        # ---- Abstract: meta description, then body paragraphs ----
        abstract = ""
        meta_desc = soup.find("meta", {"name": "description"})
        if meta_desc:
            abstract = meta_desc.get("content", "").strip()

        if len(abstract) < 100:
            main = soup.find("main")
            if main:
                for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                paras = [p.get_text(" ", strip=True)
                         for p in main.find_all("p")
                         if len(p.get_text(strip=True)) > 40]
                if paras:
                    combined = " ".join(paras)
                    if len(combined) > len(abstract):
                        abstract = combined[:5000]

        # ---- Publication ID (numeric, used as external_id + post_number) ----
        pub_id = ""
        m = re.search(r"Publication\s+ID\.?:?\s*(\d+)", raw, re.I)
        if m:
            pub_id = m.group(1)

        # ---- Publication date ----
        pub_date = None
        m_date = re.search(r"Publication\s+date:?\s*([^<\n]{5,35})", raw, re.I)
        if m_date:
            pub_date = _parse_date_text(m_date.group(1))

        # ---- PDF URL ----
        pdf_url = None
        pdf_m = re.search(r'href=["\'](/images/pub/\d+/\d+\.pdf)["\'|]', raw, re.I)
        if pdf_m:
            pdf_url = _BASE + pdf_m.group(1)

        original_filename = pdf_url.rstrip("/").split("/")[-1] if pdf_url else None

        # ---- Authors from staff profile links in "Fafo researchers" section ----
        authors_list = []
        for a_tag in soup.find_all("a", href=re.compile(r"/en/staff/")):
            h3 = a_tag.find("h3")
            if h3 and "el-title" in " ".join(h3.get("class", [])):
                name = h3.get_text(strip=True)
                if name and len(name) >= 3 and name not in authors_list:
                    authors_list.append(name)
        authors = "; ".join(authors_list) if authors_list else ""

        # ---- Fafo report / note number ----
        report_num = ""
        m_rep = re.search(
            r"(Fafo-(?:rapport|notat|brief|report|note|paper)\s+[\w:]+)",
            raw, re.I,
        )
        if m_rep:
            report_num = m_rep.group(1).strip()

        # ---- Publisher / commissioned by ----
        publisher = "Fafo"
        m_comm = re.search(r"Commis{1,2}ioned\s+by[:\s]+([^<\n]{3,120})", raw, re.I)
        if m_comm:
            raw_pub = _strip_tags(m_comm.group(1)).strip()
            if raw_pub:
                publisher = raw_pub

        # ---- Keywords from "Research areas" ----
        keywords_list = []
        ra_node = soup.find(string=re.compile(r"Research\s+areas?", re.I))
        if ra_node:
            container = ra_node.find_parent("li") or ra_node.find_parent("div")
            if container:
                for a_tag in container.find_all("a"):
                    kw = a_tag.get_text(strip=True)
                    if kw and len(kw) > 2:
                        keywords_list.append(kw)
        keywords = ", ".join(keywords_list) if keywords_list else ""

        # ---- DOI ----
        doi = ""
        m_doi = re.search(r"doi\.org/(10\.[^\s\"'<>]+)", raw, re.I)
        if m_doi:
            doi = "https://doi.org/" + m_doi.group(1)

        # ---- Metadata (unmapped raw fields) ----
        metadata_dict = {
            "category": category,
            "report_number": report_num,
            "publication_id": pub_id,
        }
        if doi:
            metadata_dict["doi"] = doi

        external_id = pub_id if pub_id else url.rstrip("/").split("/")[-1]

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": pub_id if pub_id else None,
            "url": url,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date,
            "listed_date": pub_date,
            "authors": authors,
            "publisher": publisher,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "doi": doi,
            "category": category,
            "metadata": json.dumps(metadata_dict, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl FAFO publications. Returns count of saved records.

        Paginates through each category using numeric page suffixes
        (/fafo-reports, /fafo-reports/2, /fafo-reports/3, …) until the
        page returns no new URLs, the limit is reached, or the 200-page
        safety cap fires.
        """
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        for category in _CATEGORIES:
            if saved >= limit_or_inf:
                break

            page = 1
            safety_cap = 200

            while page <= safety_cap:
                if time.time() - start_time > max_seconds:
                    print(f"[fafo-no-en] 25-minute budget reached. Stopping.")
                    return saved

                if saved >= limit_or_inf:
                    break

                if page == safety_cap:
                    print(f"[fafo-no-en] {category}: safety cap ({safety_cap} pages) reached.")

                list_url = (
                    f"{_BASE}/en/publications/{category}"
                    if page == 1
                    else f"{_BASE}/en/publications/{category}/{page}"
                )

                soup, _ = self._fetch_html(list_url)
                item_urls = self._extract_item_urls(soup, category) if soup else []
                new_urls = [u for u in item_urls if u not in seen_urls]

                if not new_urls:
                    msg = "no items on page 1, skipping." if page == 1 else f"end at page {page}."
                    print(f"[fafo-no-en] {category}: {msg}")
                    break

                for url in new_urls:
                    if saved >= limit_or_inf:
                        break
                    seen_urls.add(url)

                    try:
                        time.sleep(self._delay)
                        detail = self._parse_detail(url, category)

                        if detail is None:
                            print(f"[fafo-no-en] parse failed: {url}")
                            continue

                        abstract = detail.get("abstract") or ""
                        if len(abstract) < 50:
                            print(f"[fafo-no-en] abstract too short ({len(abstract)} chars), skip: {url}")
                            continue

                        self._save_paper(detail)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "∞"
                        print(f"[fafo-no-en] saved {saved}/{lim_str}: {detail.get('title', '')[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[fafo-no-en] item {url} failed: {exc}")
                        continue

                if page % 10 == 0:
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[fafo-no-en] page {page}: saved {saved}/{lim_str}")

                page += 1

        print(f"[fafo-no-en] Done. Total saved: {saved}")
        return saved
