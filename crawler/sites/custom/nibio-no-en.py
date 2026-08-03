# -*- coding: utf-8 -*-
"""NIBIO (Norwegian Institute of Bioeconomy Research) English publications crawler.

Starting URL:
https://www.nibio.no/en/publications?filters=1&q=&publicationType%5B%5D=e4b9b2bc-82be-42d1-80f5-f1515ad07e2e

The publications list is server-rendered with all metadata (title, authors,
journal/volume/issue, abstract, external DOI/handle link) embedded directly
in collapsible panels on the paginated list page itself -- there is no
separate per-item detail page on nibio.no, so the site's own DOI/handle
link (when present) is used as the record's ``url``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_VOL_ISSUE_RE = re.compile(r"(\d+)\s*\((\d+)\)")
_PAGES_RE = re.compile(r"p\.\s*([\d\-–]+)")


class NibioNoEnCrawler(BaseCrawler):
    """Crawler for NIBIO English publications listing."""

    site_id = "nibio-no-en"
    site_name = "Custom: nibio-no-en"
    base_url = "https://www.nibio.no"

    _LIST_PATH = "/en/publications"
    _PTYPE = "e4b9b2bc-82be-42d1-80f5-f1515ad07e2e"
    _MIN_ABSTRACT = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
    _PUBLISHER = "Norwegian Institute of Bioeconomy Research (NIBIO)"

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _page_url(self, page: int) -> str:
        return (
            f"{self.base_url}{self._LIST_PATH}"
            f"?publicationType%5B%5D={self._PTYPE}&filters=1&q=&page={page}"
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _extract_uid(self, panel) -> str:
        collapse_div = panel.find("div", class_="panel-collapse")
        raw_id = (collapse_div.get("id") if collapse_div else "") or ""
        uid = raw_id
        if uid.startswith("publication--"):
            uid = uid[len("publication--"):]
        if uid.endswith("-collapse"):
            uid = uid[: -len("-collapse")]
        return uid.strip()

    def _extract_title_and_category(self, panel):
        title_div = panel.find("div", class_="title")
        h3 = title_div.find("h3") if title_div else None
        if not h3:
            return "", ""
        span = h3.find("span")
        category = ""
        if span is not None:
            category = span.get_text(strip=True).rstrip("–- ").strip()
            span.extract()
        title = h3.get_text(" ", strip=True)
        return title, category

    def _extract_authors(self, body) -> str:
        link_list = body.find("div", class_="link-list")
        if not link_list:
            return ""
        names = []
        for tag in link_list.find_all(["a", "span"]):
            name = tag.get_text(strip=True)
            if name:
                names.append(name)
        return "; ".join(names)

    def _extract_meta(self, panel) -> dict:
        meta_div = panel.find("div", class_="meta")
        p = meta_div.find("p") if meta_div else None
        raw = re.sub(r"\s+", " ", p.get_text(" ", strip=True)) if p else ""
        parts = [seg.strip() for seg in raw.split(",") if seg.strip()]
        journal = parts[0] if parts else ""

        years = _YEAR_RE.findall(raw)
        year = years[-1] if years else None

        vol_issue = _VOL_ISSUE_RE.search(raw)
        volume, issue = "", ""
        if vol_issue:
            volume, issue = vol_issue.group(1), vol_issue.group(2)

        pages_m = _PAGES_RE.search(raw)
        pages = pages_m.group(1) if pages_m else ""

        return {
            "raw": raw,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": issue,
            "pages": pages,
        }

    def _extract_abstract(self, body) -> str:
        h4 = body.find("h4")
        if not h4:
            return ""
        p = h4.find_next_sibling("p")
        if not p:
            return ""
        text = p.get_text(" ", strip=True)
        if text.lower() == "no abstract has been registered":
            return ""
        return text

    def _extract_doc_link(self, body) -> str:
        a = body.find("a", class_="til-dokument")
        if not a:
            return ""
        return (a.get("href") or "").strip()

    def _extract_reference(self, panel) -> str:
        ref = panel.find("div", class_="references")
        return ref.get_text(" ", strip=True) if ref else ""

    def _parse_panels(self, raw_html: str, page: int) -> list:
        entries = []
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for page {page}: {exc}")
            return entries

        panels = soup.find_all("div", class_="panel-default")
        for panel in panels:
            try:
                uid = self._extract_uid(panel)
                if not uid:
                    continue
                title, category = self._extract_title_and_category(panel)
                body = panel.find("div", class_="panel-body")
                authors = self._extract_authors(body) if body else ""
                meta = self._extract_meta(panel)
                abstract = self._extract_abstract(body) if body else ""
                doc_href = self._extract_doc_link(body) if body else ""
                reference = self._extract_reference(panel)
                year_attr = panel.get("data-year") or meta.get("year") or ""

                entries.append({
                    "uid": uid,
                    "title": title,
                    "category": category,
                    "authors": authors,
                    "meta": meta,
                    "abstract": abstract,
                    "doc_href": doc_href,
                    "reference": reference,
                    "year": year_attr,
                    "page": page,
                })
            except Exception as exc:
                print(f"[{self.site_id}] panel parse error (page {page}): {exc}")
                continue

        return entries

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NIBIO English publications, walking pagination to full depth."""
        saved = 0
        seen_uids: set = set()
        start_time = time.time()
        page = 1

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                url = self._page_url(page)
                raw = self._curl_get(url)
                if raw is None:
                    print(f"[{self.site_id}] Failed to fetch page {page} after retries. Skipping page.")
                    page += 1
                    continue

                entries = self._parse_panels(raw, page)
                if not entries:
                    print(f"[{self.site_id}] page {page}: 0 entries found. End of pagination.")
                    break

                new_this_page = 0
                for entry in entries:
                    if limit is not None and saved >= limit:
                        break

                    uid = entry["uid"]
                    if uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    new_this_page += 1

                    try:
                        title = entry["title"]
                        if not title:
                            print(f"[{self.site_id}] Empty title for uid {uid}, skipping.")
                            continue

                        abstract = entry["abstract"]
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] Short abstract ({len(abstract)}) for "
                                f"'{title[:50]}', skipping."
                            )
                            continue

                        doc_href = entry["doc_href"]
                        item_url = doc_href or f"{url}#publication--{uid}-collapse"

                        doi = ""
                        if doc_href.startswith("https://doi.org/") or doc_href.startswith("http://doi.org/"):
                            doi = doc_href.split("doi.org/", 1)[-1].strip()

                        year = entry["year"]
                        published_date = f"{year}-01-01" if year and str(year).isdigit() else None
                        listed_date = published_date

                        meta_info = entry["meta"]

                        metadata = {
                            "posted_date": year or "",
                            "journal_raw": meta_info.get("raw") or "",
                            "series": meta_info.get("journal") or "",
                            "volume": meta_info.get("volume") or "",
                            "issue": meta_info.get("issue") or "",
                            "pages": meta_info.get("pages") or "",
                            "publication_type": entry["category"] or "",
                            "reference": entry["reference"] or "",
                            "uid": uid,
                            "list_page": entry["page"],
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": uid,
                            "post_number": uid,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": entry["authors"],
                            "publisher": self._PUBLISHER,
                            "department": "",
                            "journal": meta_info.get("journal") or "",
                            "url": item_url,
                            "pdf_url": None,
                            "keywords": "",
                            "category": entry["category"] or "",
                            "doi": doi or None,
                            "original_filename": None,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item failed (uid={uid}): {exc}; continuing.")
                        continue

                if new_this_page == 0:
                    print(f"[{self.site_id}] page {page}: all {len(entries)} entries already seen. Stopping.")
                    break

                if page % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

                page += 1
                time.sleep(self._delay)

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
