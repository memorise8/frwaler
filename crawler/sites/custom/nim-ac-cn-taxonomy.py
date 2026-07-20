# -*- coding: utf-8 -*-
"""Crawler for NIM China (Chinese) representative works taxonomy page.

Site: https://www.nim.ac.cn/taxonomy/term/170 (代表作)
CMS: Drupal 10. All papers rendered on a single list page (no pager).
Detail pages at /daibiaozuo/{id}. No prose abstract — synthesized from fields.
"""

import json
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class NimAcCnTaxonomyCrawler(BaseCrawler):
    site_id = "nim-ac-cn-taxonomy"
    site_name = "Custom: nim-ac-cn-taxonomy"
    base_url = "https://www.nim.ac.cn"

    _START_URL = "https://www.nim.ac.cn/taxonomy/term/170"
    _MAX_PAGES = 200
    _MAX_SECONDS = 25 * 60
    _BUDGET_MARGIN_SECONDS = 60
    _MIN_ABSTRACT_CHARS = 100

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = (1, 3, 9)
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = (result.stderr or b"").decode("utf-8", errors="replace")
                last_error = stderr.strip() or f"curl exit {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed for {url} "
                    f"(attempt {attempt + 1}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _make_soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        try:
            return BeautifulSoup("", "html.parser")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("　", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _field(self, soup, css_name):
        """Get first .field--item text from a Drupal field identified by its CSS name."""
        if soup is None:
            return ""
        el = soup.select_one(f".field--name-{css_name} .field--item")
        if el:
            return self._clean(el.get_text(" ", strip=True))
        return ""

    def _absolute_url(self, href):
        href = self._clean(href)
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            return ""
        return urljoin(self.base_url + "/", href)

    def _date_from_year(self, year_str):
        year_str = self._clean(year_str)
        m = re.search(r"\b(20\d{2}|19\d{2})\b", year_str)
        return f"{m.group(1)}-01-01" if m else ""

    def _split_authors(self, value):
        value = self._clean(value)
        if not value:
            return []
        parts = re.split(r"[;；、，,]+", value)
        return [self._clean(p) for p in parts if self._clean(p)]

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_items(self):
        """Fetch the taxonomy list page and return all paper rows (single page, no pager)."""
        raw = self._curl_get(self._START_URL)
        if not raw:
            print(f"[{self.site_id}] failed to fetch list page")
            return []
        soup = self._make_soup(raw)
        if soup is None:
            return []

        items = []
        seen_hrefs = set()

        # Each discipline is an independent embedded view block with its own .view-content.
        # Each block has one <h3> category header and multiple .views-row paper entries.
        for view_block in soup.select(".view-content"):
            # Extract category from h3 that does NOT contain a link (nav h3 contain links)
            h3 = view_block.find("h3", recursive=False)
            if h3 is None:
                # some blocks wrap the h3 differently; try non-recursive
                h3 = view_block.find("h3")
            if h3 and h3.find("a"):
                # nav header — skip
                continue
            category = self._clean(h3.get_text(" ", strip=True)) if h3 else ""

            for row in view_block.select(".views-row"):
                link = row.select_one("a[href^='/daibiaozuo/']")
                if link is None:
                    continue
                href = link.get("href", "")
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                title = self._clean(link.get_text(" ", strip=True))
                detail_url = self._absolute_url(href)
                m = re.search(r"/daibiaozuo/(\d+)", href)
                post_number = m.group(1) if m else None

                # Expand-panel metadata from list page
                list_journal = ""
                list_first_author = ""
                list_corr_author = ""
                list_year = ""
                list_dept = ""

                detail_div = row.select_one(".sh_expan_detail")
                if detail_div:
                    for col in detail_div.select("[class*='col-md']"):
                        paras = col.find_all("p")
                        if len(paras) >= 2:
                            label = self._clean(paras[0].get_text(" ", strip=True))
                            val = self._clean(paras[1].get_text(" ", strip=True))
                            if "发表刊物" in label:
                                list_journal = val
                            elif "第一作者" in label:
                                list_first_author = val
                            elif "通讯作者" in label:
                                list_corr_author = val
                        # Year uses a <div> tag inside the col
                        year_label = col.find("p", string=re.compile(r"年度"))
                        if year_label:
                            year_div = col.find("div")
                            if year_div:
                                list_year = self._clean(year_div.get_text(strip=True))

                # Department column next to the title link
                dept_col = row.select_one(".sh_expan_click .col-md-2")
                if dept_col:
                    list_dept = self._clean(dept_col.get_text(strip=True))

                items.append({
                    "url": detail_url,
                    "post_number": post_number,
                    "title": title,
                    "category": category,
                    "list_journal": list_journal,
                    "list_first_author": list_first_author,
                    "list_corr_author": list_corr_author,
                    "list_year": list_year,
                    "list_dept": list_dept,
                })

        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _external_id(self, detail_url):
        m = re.search(r"/daibiaozuo/(\d+)", detail_url)
        if m:
            return m.group(1)
        return detail_url.rstrip("/").split("/")[-1] or detail_url

    def _build_abstract(self, title, journal, year, first_author, corr_author, department, discipline):
        """Synthesize a >= 100-char abstract from available metadata fields."""
        parts = ["Representative work from NIM (National Institute of Metrology, China)."]
        if title:
            parts.append(f"Title: {title}.")
        if journal:
            suffix = f" ({year})." if year else "."
            parts.append(f"Published in: {journal}{suffix}")
        if first_author:
            parts.append(f"First author(s): {first_author}.")
        if corr_author:
            parts.append(f"Corresponding author(s): {corr_author}.")
        if department:
            parts.append(f"Department: {department}.")
        if discipline:
            parts.append(f"Discipline: {discipline}.")
        return " ".join(parts)

    def _parse_detail(self, row, raw):
        soup = self._make_soup(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        # Drupal field CSS names (hyphens in class = underscores in field name)
        title = self._field(soup, "name") or self._clean(row.get("title"))
        journal = self._field(soup, "journal-name") or self._clean(row.get("list_journal"))
        first_author = self._field(soup, "first-author") or self._clean(row.get("list_first_author"))
        corr_author = self._field(soup, "corresponding-author") or self._clean(row.get("list_corr_author"))
        department = self._field(soup, "bumen") or self._clean(row.get("list_dept"))
        year = self._field(soup, "year") or self._clean(row.get("list_year"))
        xuhao = self._field(soup, "xuhao")          # 序号 — serial number
        discipline = self._field(soup, "xueke") or self._clean(row.get("category"))

        detail_url = row.get("url", "")
        external_id = self._external_id(detail_url)
        post_number = row.get("post_number") or external_id

        published_date = self._date_from_year(year)

        # Merge first + corresponding authors, deduplicated
        all_authors = []
        for chunk in (first_author, corr_author):
            for a in self._split_authors(chunk):
                if a and a not in all_authors:
                    all_authors.append(a)

        # Check for any PDF link on the detail page
        pdf_url = None
        for a in soup.select("a[href]"):
            href = self._absolute_url(a.get("href", ""))
            if href and ".pdf" in href.lower():
                pdf_url = href
                break

        abstract = self._build_abstract(title, journal, year, first_author, corr_author, department, discipline)

        metadata = {
            "list_endpoint": self._START_URL,
            "detail_endpoint": detail_url,
            "drupal_route": f"daibiaozuo/{external_id}",
            "journal_raw": journal,
            "xuhao": xuhao,
            "discipline": discipline,
            "first_author_raw": first_author,
            "corresponding_author_raw": corr_author,
            "year": year,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "authors": json.dumps(all_authors, ensure_ascii=False),
            "abstract": abstract,
            "category": discipline or row.get("category", ""),
            "keywords": "",
            "published_date": published_date,
            "listed_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "publisher": department,
            "journal": journal,
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        start_time = time.monotonic()
        saved = 0
        limit_or_inf = limit if limit is not None else "inf"

        print(f"[{self.site_id}] fetching list page: {self._START_URL}")
        all_items = self._fetch_list_items()
        if not all_items:
            print(f"[{self.site_id}] no items found on list page")
            return 0
        print(f"[{self.site_id}] found {len(all_items)} papers on list page")

        seen_urls = set()
        page = 0
        pages_seen = 1

        while pages_seen <= self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time >= self._MAX_SECONDS - self._BUDGET_MARGIN_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                break

            # All items come from a single list page — page loop runs once
            page_items = all_items if page == 0 else []
            pages_seen += 1

            if not page_items:
                print(f"[{self.site_id}] page {page}: no rows; stopping")
                break

            new_on_page = 0
            for idx, row in enumerate(page_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= self._MAX_SECONDS - self._BUDGET_MARGIN_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                    return saved

                detail_url = row.get("url", "")
                ext_id = row.get("post_number") or self._external_id(detail_url)
                item_label = f"daibiaozuo/{ext_id}"

                if not detail_url:
                    print(f"[{self.site_id}] item {item_label} skipped: missing URL")
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    if self.detail_delay:
                        time.sleep(self.detail_delay)
                    raw_detail = self._curl_get(detail_url, referer=self._START_URL)
                    if not raw_detail:
                        raise RuntimeError("empty detail response after retries")

                    paper = self._parse_detail(row, raw_detail)
                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {item_label} skipped: missing title")
                        continue

                    abstract = self._clean(paper.get("abstract", ""))
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"short abstract ({len(abstract)} chars)"
                        )
                        continue
                    paper["abstract"] = abstract

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            # This site is single-page; no next page exists
            print(f"[{self.site_id}] page {page}: no next page (single-page site); stopping")
            break

        if pages_seen > self._MAX_PAGES:
            print(f"[{self.site_id}] reached safety page cap {self._MAX_PAGES}; stopping")

        return saved
