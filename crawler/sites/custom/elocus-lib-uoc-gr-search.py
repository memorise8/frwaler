# -*- coding: utf-8 -*-
"""E-Locus institutional repository crawler (University of Crete).

Crawls the digital library at https://elocus.lib.uoc.gr/ for theses and
academic works.  Uses HTML scraping via curl since the site has no JSON API.

Pagination: offset=1,11,21,... (10 items per list page).
Detail pages: display_mode=detail&offset=N for full metadata + abstract.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler

_SEARCH_URL = "https://elocus.lib.uoc.gr/search/"

# Static param fragment shared across all requests (URL-encoded).
_BASE_PARAMS = (
    "search_type=advanced"
    "&wf_step=init"
    "&show_hidden=0"
    "&cclterm8=%2Fhierarchy%2Fmaterial%2F010"
    "&cclfield8=collection"
    "&cclop8=and"
    "&search_coll%5Bdlib%5D=1"
    "&stored_cclquery="
    "&skin="
    "&rss=0"
    "&store_query=1"
    "&show_form="
    "&clone_file="
    "&export_method=none"
)

_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_LEN = 50


def _make_soup(html: str):
    """Parse HTML with parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


class ElocusLibUocGrSearchCrawler(BaseCrawler):
    site_id = "elocus-lib-uoc-gr-search"
    site_name = "Custom: elocus-lib-uoc-gr-search"
    base_url = "https://elocus.lib.uoc.gr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch URL via curl with exponential-backoff retry."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sLk",
            "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: el,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.decode("utf-8", errors="replace")
                wait = 1 * (3 ** attempt)
                if attempt < retries - 1:
                    print(f"[{self.site_id}] Empty response (attempt {attempt+1}), "
                          f"retrying in {wait}s…")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                wait = 1 * (3 ** attempt)
                if attempt < retries - 1:
                    print(f"[{self.site_id}] Timeout (attempt {attempt+1}), "
                          f"retrying in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                wait = 1 * (3 ** attempt)
                if attempt < retries - 1:
                    print(f"[{self.site_id}] curl error: {exc}, retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    def _list_url(self, offset: int) -> str:
        return (f"{_SEARCH_URL}?{_BASE_PARAMS}"
                f"&display_mode=overview&number={_PAGE_SIZE}&offset={offset}")

    def _detail_url(self, offset: int) -> str:
        return (f"{_SEARCH_URL}?{_BASE_PARAMS}"
                f"&display_mode=detail&number=1&offset={offset}"
                f"&search_help=detail&keep_number={_PAGE_SIZE}")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _get_total(self, html: str) -> int | None:
        """Extract total record count from list page (embedded RSS link)."""
        m = re.search(r"number=(\d+)&(?:amp;)?rss=1", html)
        if m:
            return int(m.group(1))
        # Fallback: find highest page offset
        offsets = [int(x) for x in re.findall(r"offset=(\d+)", html)]
        if offsets:
            return max(offsets) + _PAGE_SIZE - 1
        return None

    def _parse_detail(self, html: str) -> dict | None:
        """Parse a detail page; return a metadata dict or None on error."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup error: {exc}")
            return None

        data: dict[str, str] = {}
        try:
            for row in soup.find_all("tr"):
                label_td = row.find("td", class_="detaillabel")
                value_td = row.find("td", class_="detailtext")
                if label_td and value_td:
                    label = label_td.get_text(strip=True)
                    value = value_td.get_text(separator=" ", strip=True)
                    if label:
                        data[label] = value
        except Exception as exc:
            print(f"[{self.site_id}] Row parse error: {exc}")

        # Abstract — prefer <abstract> XML tag, fall back to label text.
        abstract = ""
        try:
            abs_tag = soup.find("abstract")
            if abs_tag:
                abstract = abs_tag.get_text(separator=" ", strip=True)
        except Exception:
            pass
        if not abstract:
            abstract = data.get("Περίληψη", "")

        # PDF URL + original filename.
        pdf_url = None
        original_filename = None
        try:
            dotbl = soup.find("table", class_="DOtbl")
            if dotbl:
                for a in dotbl.find_all("a"):
                    href = (a.get("href") or "").strip()
                    if href.lower().endswith(".pdf"):
                        pdf_url = href
                        original_filename = href.rsplit("/", 1)[-1]
                        break
        except Exception:
            pass

        # Stable internal ID from srfile=... param.
        external_id = None
        meta_url = None
        try:
            m = re.search(r"srfile=(/dlib/[^\"'&\s]+)", html)
            if m:
                srfile = m.group(1)
                meta_url = self.base_url + srfile
                fname = srfile.rsplit("/", 1)[-1]
                external_id = fname.replace(".tkl", "")
        except Exception:
            pass

        # Numeric post_number — trailing digits of the internal ID.
        post_number = None
        if external_id:
            mn = re.search(r"-(\d+)$", external_id)
            if mn:
                post_number = mn.group(1)

        # Published date — normalise to YYYY-MM-DD or YYYY.
        raw_date = data.get("Ημερομηνία έκδοσης", "")
        published_date = None
        if raw_date:
            md = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
            if md:
                published_date = md.group(1)
            else:
                my = re.match(r"(\d{4})", raw_date)
                if my:
                    published_date = my.group(1)

        title = data.get("Τίτλος", "")
        author = data.get("Συγγραφέας", "")
        advisor = data.get("Σύμβουλος διατριβής", "") or data.get("Επιβλέπων", "")
        language = data.get("Γλώσσα", "")
        collection = data.get("Συλλογή", "")

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "authors": author,
            "published_date": published_date,
            "url": meta_url or "",
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": collection,
            "metadata": json.dumps({
                "advisor": advisor,
                "language": language,
                "collection": collection,
                "raw_date": raw_date,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl E-Locus and persist records to the DB.

        Iterates list pages (offset 1, 11, 21, …).  For each list item the
        corresponding detail page is fetched for the full abstract and PDF URL.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        page_num = 0
        list_offset = 1
        total: int | None = None

        while True:
            # ── time / limit / page-cap guards ──────────────────────────
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed >= _CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached, stopping cleanly.")
                break
            if page_num >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached, stopping.")
                break

            # ── fetch list page ─────────────────────────────────────────
            list_url = self._list_url(list_offset)
            try:
                list_html = self._curl_get(list_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] List fetch error at offset {list_offset}: {exc}")
                break

            if not list_html:
                print(f"[{self.site_id}] Empty list page at offset {list_offset}, stopping.")
                break

            # Get total on first page.
            if total is None:
                total = self._get_total(list_html)
                if total:
                    print(f"[{self.site_id}] Total records: {total}")

            # ── parse list page rows ────────────────────────────────────
            try:
                soup = _make_soup(list_html)
                rows = soup.find_all("tr", class_="resultsnorm")
            except Exception as exc:
                print(f"[{self.site_id}] List parse error at offset {list_offset}: {exc}")
                rows = []

            if not rows:
                print(f"[{self.site_id}] No result rows at offset {list_offset}, stopping.")
                break

            page_num += 1
            if page_num % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

            # ── process each item on the list page ──────────────────────
            for item_idx, _row in enumerate(rows):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _CRAWL_BUDGET_SECS:
                    break

                item_offset = list_offset + item_idx
                detail_url = self._detail_url(item_offset)

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                # Fetch detail page.
                detail_html = None
                try:
                    for attempt in range(3):
                        detail_html = self._curl_get(detail_url)
                        if detail_html:
                            break
                        wait = 1 * (3 ** attempt)
                        print(f"[{self.site_id}] item offset={item_offset} "
                              f"fetch attempt {attempt+1} failed, retrying in {wait}s…")
                        time.sleep(wait)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item offset={item_offset} failed: {exc}")
                    continue

                if not detail_html:
                    print(f"[{self.site_id}] item offset={item_offset}: "
                          f"no response after 3 attempts, skipping.")
                    continue

                # Parse detail page.
                try:
                    item_data = self._parse_detail(detail_html)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item offset={item_offset} failed: {exc}")
                    continue

                if not item_data:
                    print(f"[{self.site_id}] item offset={item_offset}: "
                          f"parse returned nothing, skipping.")
                    continue

                abstract = item_data.get("abstract", "")
                if not abstract or len(abstract) < _MIN_ABSTRACT_LEN:
                    print(f"[{self.site_id}] item offset={item_offset}: "
                          f"abstract too short ({len(abstract)} chars), skipping.")
                    continue

                title = item_data.get("title", "")
                if not title:
                    print(f"[{self.site_id}] item offset={item_offset}: "
                          f"no title, skipping.")
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": item_data.get("external_id") or f"offset-{item_offset}",
                    "post_number": item_data.get("post_number"),
                    "title": title,
                    "abstract": abstract,
                    "authors": item_data.get("authors") or "",
                    "published_date": item_data.get("published_date"),
                    "url": item_data.get("url") or detail_url,
                    "pdf_url": item_data.get("pdf_url"),
                    "original_filename": item_data.get("original_filename"),
                    "category": item_data.get("category") or "",
                    "keywords": "",
                    "doi": "",
                    "department": "",
                    "metadata": item_data.get("metadata") or "{}",
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item offset={item_offset} save failed: {exc}")
                    continue

                time.sleep(self._delay)

            list_offset += _PAGE_SIZE

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
