# -*- coding: utf-8 -*-
"""École polytechnique press releases crawler (polytechnique-edu-presse).

Target: https://www.polytechnique.edu/presse/communiques-et-dossiers-de-presse
Structure: Drupal HTML site, ~12 items/page, paginated via ?page=N (0-indexed).
Each detail page carries a Drupal node ID (used as post_number/external_id).
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.polytechnique.edu"
_LIST_PATH = "/presse/communiques-et-dossiers-de-presse"
_PUBLISHER = "École polytechnique"


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _bs(html):
    """Build BeautifulSoup with fallback parsers: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw):
    """Convert 'DD.MM.YY' or 'DD.MM.YYYY' → 'YYYY-MM-DD', or None."""
    if not raw:
        return None
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s*$", raw.strip())
    if not m:
        return None
    day, month, year = m.group(1), m.group(2), m.group(3)
    if len(year) == 2:
        year = "20" + year
    return f"{year}-{int(month):02d}-{int(day):02d}"


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class PolytechniqueEduPresseCrawler(BaseCrawler):
    site_id = "polytechnique-edu-presse"
    site_name = "Custom: polytechnique-edu-presse"
    base_url = _BASE

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _fetch(self, url):
        """GET url via curl with up to 3 retries (1s, 3s, 9s backoff). Returns text or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = 3 ** attempt  # 1, 3, 9
                print(f"[{self.site_id}] empty response for {url} (attempt {attempt+1}/3), retry in {wait}s")
                if attempt < 2:
                    time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{self.site_id}] curl error for {url}: {exc} (attempt {attempt+1}/3)")
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, html):
        """Extract press release stubs from a listing page.

        Returns list of dicts with keys: url, title, date_raw, category.
        """
        try:
            soup = _bs(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup list parse error: {exc}")
            return []
        if not soup:
            return []

        items = []
        for block in soup.find_all("div", class_="node--communiques"):
            try:
                a = block.find("a", class_=lambda c: c and "stretched-link" in c)
                if not a:
                    continue
                href = a.get("href", "")
                if not href.startswith("/presse/communiques"):
                    continue

                title_span = a.find("span", class_=lambda c: c and "field--name-title" in c)
                title = title_span.get_text(strip=True) if title_span else a.get("title", "")

                # Date inside span.date (format: "• DD.MM.YY")
                date_raw = ""
                date_span = block.find("span", class_="date")
                if date_span:
                    date_raw = date_span.get_text(strip=True).lstrip("•·").strip()

                # Category from span.theme (text minus the nested date span)
                category = ""
                theme_span = block.find("span", class_="theme")
                if theme_span:
                    theme_text = theme_span.get_text(separator=" ", strip=True)
                    if date_raw and date_raw in theme_text:
                        category = theme_text.replace(date_raw, "").replace("•", "").strip()
                    else:
                        category = theme_text

                items.append({
                    "url": _BASE + href,
                    "title": title,
                    "date_raw": date_raw,
                    "category": category,
                })
            except Exception as exc:
                print(f"[{self.site_id}] list block parse error: {exc}")

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html):
        """Extract full content from a press release detail page.

        Returns dict with keys:
            title, node_id, date_raw, category, body, meta_desc, pdf_url, pdf_filename
        """
        try:
            soup = _bs(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup detail parse error: {exc}")
            return {}
        if not soup:
            return {}

        result = {}

        # Title from h1
        h1 = soup.find("h1")
        if h1:
            title_span = h1.find("span", class_=lambda c: c and "field--name-title" in c)
            result["title"] = title_span.get_text(strip=True) if title_span else h1.get_text(strip=True)
        else:
            result["title"] = ""

        # Drupal node ID from the embedded settings JSON
        node_id = None
        settings_tag = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if settings_tag and settings_tag.string:
            try:
                s = json.loads(settings_tag.string)
                cp = s.get("path", {}).get("currentPath", "")
                m = re.match(r"node/(\d+)$", cp)
                if m:
                    node_id = m.group(1)
            except Exception:
                pass
        result["node_id"] = node_id

        # Date from div.date > b
        date_raw = ""
        date_div = soup.find("div", class_="date")
        if date_div:
            b_tag = date_div.find("b")
            if b_tag:
                date_raw = b_tag.get_text(strip=True)
        result["date_raw"] = date_raw

        # Category from div.tags
        tags_div = soup.find("div", class_="tags")
        if tags_div:
            result["category"] = re.sub(r"\s+", " ", tags_div.get_text(separator=" ", strip=True))
        else:
            result["category"] = ""

        # Body text: all nce--wysiwyg divs joined
        parts = []
        for d in soup.find_all("div", class_="nce--wysiwyg"):
            text = d.get_text(separator="\n", strip=True)
            if text:
                parts.append(text)
        result["body"] = "\n\n".join(parts)

        # Fallback abstract from meta description tag
        meta = soup.find("meta", attrs={"name": "description"})
        result["meta_desc"] = meta.get("content", "") if meta else ""

        # First PDF attachment in div.liste-documents
        pdf_url = None
        pdf_filename = None
        docs_div = soup.find("div", class_="liste-documents")
        if docs_div:
            for a in docs_div.find_all("a", href=True):
                href = a.get("href", "")
                if ".pdf" in href.lower():
                    pdf_url = href if href.startswith("http") else _BASE + href
                    span = a.find("span")
                    if span:
                        pdf_filename = span.get_text(strip=True)
                    elif a.get("title"):
                        pdf_filename = a["title"]
                    else:
                        pdf_filename = href.rstrip("/").split("/")[-1].split("?")[0]
                    break
        result["pdf_url"] = pdf_url
        result["pdf_filename"] = pdf_filename

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        seen_urls = set()
        saved = 0
        page = 0
        start_time = time.time()
        MAX_PAGES = 200
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            elapsed = time.time() - start_time
            if elapsed > 25 * 60:
                print(f"[{self.site_id}] 25-minute time budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_url = f"{_BASE}{_LIST_PATH}?page={page}"
            html = self._fetch(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            items = self._parse_list(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # Dedup: skip URLs already seen (prevents infinite loop if paginator wraps)
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Stopping.")
                break
            for it in new_items:
                seen_urls.add(it["url"])

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(self._delay)
                    detail_html = self._fetch(item["url"])
                    if not detail_html:
                        print(f"[{self.site_id}] item {item['url']} failed: no response")
                        continue

                    detail = self._parse_detail(detail_html)

                    # Abstract: full body text, fallback to meta description
                    abstract = detail.get("body", "").strip()
                    if not abstract:
                        abstract = detail.get("meta_desc", "").strip()

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] abstract too short (<50 chars) for {item['url']}, skipping")
                        continue

                    date_raw = detail.get("date_raw") or item.get("date_raw", "")
                    published_date = _parse_date(date_raw)

                    title = detail.get("title") or item.get("title", "")
                    node_id = detail.get("node_id")
                    category = detail.get("category") or item.get("category", "")
                    pdf_url = detail.get("pdf_url")
                    pdf_filename = detail.get("pdf_filename")

                    paper = {
                        "site_id": self.site_id,
                        "external_id": node_id or item["url"],
                        "post_number": node_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": item["url"],
                        "pdf_url": pdf_url,
                        "original_filename": pdf_filename,
                        "category": category,
                        "publisher": _PUBLISHER,
                        "keywords": None,
                        "authors": None,
                        "doi": None,
                        "metadata": json.dumps({
                            "node_id": node_id,
                            "date_raw": date_raw,
                            "posted_date": date_raw,
                            "originalFilename": pdf_filename,
                            "category": category,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('url', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
