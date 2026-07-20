# -*- coding: utf-8 -*-
"""ISSI Bern journal publications crawler.

Target: https://www.issibern.ch/results/publications/journal-publications/
Uses the FacetWP REST API (template: journal_publications).
6 816 total records, 10 per page, 682 pages.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_FACETWP_URL = "https://www.issibern.ch/wp-json/facetwp/v1/refresh"
_LIST_URL = "https://www.issibern.ch/results/publications/journal-publications/"
_TEMPLATE = "journal_publications"
_URI = r"results\/publications\/journal-publications"
_PER_PAGE = 10
_MAX_PAGES = 200
_MAX_SECS = 25 * 60  # 25-minute wall-clock budget


# ---------------------------------------------------------------------------
# BeautifulSoup fallback chain
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


class IssibernChResultsCrawler(BaseCrawler):
    site_id = "issibern-ch-results"
    site_name = "Custom: issibern-ch-results"
    base_url = "https://www.issibern.ch"

    # ------------------------------------------------------------------
    # Low-level network helpers (curl — avoids TLS issues)
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> Optional[str]:
        for attempt in range(3):
            try:
                res = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-skL", "--max-time", "30", url],
                    capture_output=True, timeout=35,
                )
                body = res.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
            except Exception as exc:
                print(f"[issibern-ch-results] curl GET error (attempt {attempt+1}): {exc}")
            if attempt < 2:
                wait = (attempt + 1) ** 2
                print(f"[issibern-ch-results] Retrying GET in {wait}s...")
                time.sleep(wait)
        return None

    def _curl_post_json(self, url: str, payload: dict, nonce: str) -> Optional[dict]:
        body_str = json.dumps(payload)
        for attempt in range(3):
            try:
                res = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
                        "-X", "POST",
                        "-H", "Content-Type: application/json",
                        "-H", f"X-WP-Nonce: {nonce}",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "--data", body_str,
                        url,
                    ],
                    capture_output=True, timeout=35,
                )
                text = res.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        pass
            except Exception as exc:
                print(f"[issibern-ch-results] curl POST error (attempt {attempt+1}): {exc}")
            if attempt < 2:
                wait = (attempt + 1) ** 2
                print(f"[issibern-ch-results] Retrying POST in {wait}s...")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Nonce
    # ------------------------------------------------------------------

    def _get_nonce(self) -> Optional[str]:
        html = self._curl_get(_LIST_URL)
        if not html:
            return None
        m = re.search(r'"nonce"\s*:\s*"([^"]+)"', html)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # FacetWP page fetch
    # ------------------------------------------------------------------

    def _fetch_page(self, page: int, nonce: str) -> Optional[dict]:
        payload = {
            "action": "facetwp_refresh",
            "data": {
                "facets": {"search": "", "jp_groups": "", "fcpager": ""},
                "frozen_facets": {},
                "http_params": {
                    "get": [],
                    "uri": "results/publications/journal-publications",
                    "url_vars": [],
                },
                "template": _TEMPLATE,
                "extras": {"counts": True, "sort": "default"},
                "soft_refresh": 1,
                "is_bfcache": 1,
                "first_load": 0,
                "paged": page,
            },
        }
        return self._curl_post_json(_FACETWP_URL, payload, nonce)

    # ------------------------------------------------------------------
    # DOI helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_doi(href: str) -> Optional[str]:
        if not href:
            return None
        m = re.search(r'doi\.org/(10\.\S+)', href)
        if m:
            return m.group(1).rstrip(".")
        m = re.match(r'https?://(10\.\d{4,}/\S+)', href)
        if m:
            return m.group(1).rstrip(".")
        return None

    @staticmethod
    def _normalize_doi_url(href: str) -> str:
        """Convert bare http://10.xxx/yyy → https://doi.org/10.xxx/yyy."""
        m = re.match(r'https?://(10\.\d{4,}/\S+)', href)
        if m:
            return "https://doi.org/" + m.group(1)
        return href

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_items(self, html: str) -> list:
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[issibern-ch-results] BeautifulSoup error: {exc}")
            return []

        results = []
        for div in soup.find_all("div", class_="jp-item"):
            try:
                # Year + journal
                ctitle_tag = div.find("span", class_="ctitle")
                ctitle_text = ctitle_tag.get_text(strip=True) if ctitle_tag else ""
                year = journal = None
                if "|" in ctitle_text:
                    parts = ctitle_text.split("|", 1)
                    year = parts[0].strip()
                    journal = parts[1].strip()
                elif ctitle_text:
                    year = ctitle_text.strip()

                # Title
                h3 = div.find("h3")
                title = h3.get_text(strip=True) if h3 else None
                if not title:
                    continue

                # Abstract: first <p> with substantial text
                abstract = None
                for p_tag in div.find_all("p"):
                    text = p_tag.get_text(strip=True)
                    if len(text) >= 50:
                        abstract = text
                        break

                # Authors
                authors_ul = div.find("ul", class_="jp-block-item-authors-items")
                authors_list = []
                if authors_ul:
                    for li in authors_ul.find_all("li"):
                        name = li.get_text(strip=True)
                        if name:
                            authors_list.append(name)
                authors = "; ".join(authors_list) if authors_list else None

                # Info block: ISSN, Page, Issue, Volume
                info_ul = div.find("ul", class_="jp-block-item-info-items")
                issn = page_num = issue = volume = None
                if info_ul:
                    for li in info_ul.find_all("li"):
                        text = li.get_text(strip=True)
                        if text.startswith("ISSN:"):
                            issn = text[5:].strip()
                        elif text.startswith("Page:"):
                            page_num = text[5:].strip()
                        elif text.startswith("Issue:"):
                            issue = text[6:].strip()
                        elif text.startswith("Volume:"):
                            volume = text[7:].strip()

                # DOI / URL from "read more" button
                btn_div = div.find("div", class_="jp-block-item-button")
                href = None
                if btn_div:
                    a_tag = btn_div.find("a")
                    if a_tag:
                        href = (a_tag.get("href") or "").strip()

                doi = self._extract_doi(href) if href else None
                url = self._normalize_doi_url(href) if href else None

                # external_id: DOI preferred, else sanitised title
                external_id = doi or re.sub(r"\W+", "-", title)[:120].lower().strip("-")

                published_date = None
                if year and re.match(r"^\d{4}$", year):
                    published_date = f"{year}-01-01"

                meta = {}
                if issn:
                    meta["issn"] = issn
                if page_num:
                    meta["page"] = page_num
                if issue:
                    meta["issue"] = issue
                if volume:
                    meta["volume"] = volume
                if doi:
                    meta["doi"] = doi
                if journal:
                    meta["journal_raw"] = journal

                results.append({
                    "title": title,
                    "abstract": abstract,
                    "authors": authors,
                    "journal": journal,
                    "published_date": published_date,
                    "url": url,
                    "doi": doi,
                    "external_id": external_id,
                    "metadata": meta,
                })
            except Exception as exc:
                print(f"[issibern-ch-results] item parse error: {exc}")
                continue

        return results

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl ISSI Bern journal publications via FacetWP API.

        Walks pages until limit reached, no new items found, or safety caps hit.
        """
        limit_val = limit if limit is not None else float("inf")
        saved = 0
        seen_keys: set = set()
        start_time = time.time()

        nonce = self._get_nonce()
        if not nonce:
            print("[issibern-ch-results] Could not fetch nonce — aborting")
            return saved
        print(f"[issibern-ch-results] nonce={nonce}, starting crawl (limit={limit})")

        page = 1
        total_pages_remote = None

        try:
            while True:
                # Wall-clock budget
                if time.time() - start_time > _MAX_SECS:
                    print("[issibern-ch-results] 25-minute budget reached, exiting cleanly")
                    break

                # Safety cap
                if page > _MAX_PAGES:
                    print(f"[issibern-ch-results] Safety cap of {_MAX_PAGES} pages reached")
                    break

                # Limit satisfied
                if saved >= limit_val:
                    break

                if page % 10 == 1 and page > 1:
                    print(f"[issibern-ch-results] page {page}: saved {saved}/{limit if limit else 'inf'}")

                data = self._fetch_page(page, nonce)
                if not data:
                    print(f"[issibern-ch-results] page {page}: no response, stopping")
                    break

                # Update total pages from first response
                if total_pages_remote is None:
                    total_pages_remote = (
                        data.get("settings", {}).get("pager", {}).get("total_pages", 0)
                    )

                template_html = data.get("template", "")
                if not template_html or not template_html.strip():
                    print(f"[issibern-ch-results] page {page}: empty template, stopping")
                    break

                items = self._parse_items(template_html)
                if not items:
                    print(f"[issibern-ch-results] page {page}: no items parsed, stopping")
                    break

                # Dedup within this crawl run
                new_items = []
                for item in items:
                    key = item.get("url") or item.get("external_id") or ""
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    new_items.append(item)

                if not new_items:
                    print(f"[issibern-ch-results] page {page}: all items already seen, stopping")
                    break

                for item in new_items:
                    if saved >= limit_val:
                        break

                    abstract = item.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[issibern-ch-results] skip '{item.get('title','')[:40]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    try:
                        self._save_paper({
                            "site_id": self.site_id,
                            "external_id": item["external_id"],
                            "title": item["title"],
                            "abstract": abstract,
                            "authors": item.get("authors"),
                            "journal": item.get("journal"),
                            "published_date": item.get("published_date"),
                            "url": item.get("url"),
                            "doi": item.get("doi"),
                            "publisher": "International Space Science Institute",
                            "metadata": item.get("metadata"),
                        })
                        saved += 1
                    except Exception as exc:
                        print(
                            f"[issibern-ch-results] save error for "
                            f"'{item.get('title','')[:40]}': {exc}"
                        )
                        continue

                # Pagination end detection
                if total_pages_remote and page >= total_pages_remote:
                    print(f"[issibern-ch-results] Reached last page ({page}/{total_pages_remote})")
                    break

                page += 1
                time.sleep(self._delay)

        except KeyboardInterrupt:
            print(f"[issibern-ch-results] Interrupted at page {page}, saved={saved}")
            raise

        print(f"[issibern-ch-results] Done. saved={saved}")
        return saved
