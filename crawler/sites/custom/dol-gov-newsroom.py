# -*- coding: utf-8 -*-
"""Crawler for US Department of Labor newsroom releases.

Source: https://www.dol.gov/newsroom/releases
Strategy:
  - List page (Drupal 10 HTML, 20 items/page, numeric ?page= pagination).
  - Each item has data-history-node-id (node_id = post_number) and a teaser.
  - Detail page fetched with Sec-Fetch-* headers (required to bypass Akamai WAF).
  - Full body text assembled from <p> paragraphs on the detail page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class DolGovNewsroomCrawler(BaseCrawler):
    site_id = "dol-gov-newsroom"
    site_name = "Custom: dol-gov-newsroom"
    base_url = "https://www.dol.gov"

    _LIST_ENDPOINT = (
        "https://www.dol.gov/newsroom/releases"
        "?agency=All&state=All&topic=All&year=all&page={page}"
    )
    _PUBLISHER = "U.S. Department of Labor"
    _CATEGORY = "News Release"
    _RETRY_WAITS = (1, 3, 9)
    _SAFETY_PAGE_CAP = 200
    _MAX_CRAWL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _TIME_STOP_MARGIN_SECONDS = 30
    _MIN_ABSTRACT_CHARS = 50

    # Known DOL agency abbreviation → full name
    _AGENCY_NAMES = {
        "osha": "Occupational Safety and Health Administration",
        "whd": "Wage and Hour Division",
        "eta": "Employment and Training Administration",
        "olms": "Office of Labor-Management Standards",
        "odep": "Office of Disability Employment Policy",
        "osec": "Office of the Secretary",
        "bls": "Bureau of Labor Statistics",
        "ebsa": "Employee Benefits Security Administration",
        "ilab": "Bureau of International Labor Affairs",
        "mine": "Mine Safety and Health Administration",
        "msha": "Mine Safety and Health Administration",
        "ofccp": "Office of Federal Contract Compliance Programs",
        "owcp": "Office of Workers Compensation Programs",
        "vets": "Veterans Employment and Training Service",
        "sol": "Office of the Solicitor",
        "opa": "Office of Public Affairs",
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, context="request", referer=None,
                  is_detail=False, timeout=45):
        """Fetch URL via curl with retry + exponential backoff.

        detail pages require Sec-Fetch-* headers to pass the Akamai WAF.
        """
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--fail", "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if is_detail:
            cmd.extend([
                "-H", "Sec-Fetch-Dest: document",
                "-H", "Sec-Fetch-Mode: navigate",
                "-H", "Sec-Fetch-Site: same-origin",
            ])
        cmd.append(url)

        last_error = "unknown error"
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = (
                    "empty response" if result.returncode == 0
                    else f"exit={result.returncode} stderr={stderr[:300]}"
                )

            if attempt < 3:
                wait = self._RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] {context} attempt {attempt}/3 "
                    f"failed ({last_error}); retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw, *, context="html"):
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = raw or ""

        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")

        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @classmethod
    def _parse_date(cls, raw):
        """Parse a date string to YYYY-MM-DD; return '' on failure."""
        raw = cls._one_line(raw)
        if not raw:
            return ""

        m = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
        if m:
            return m.group(0)

        m = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b", raw)
        if m:
            mo, dy, yr = m.groups()
            return f"{int(yr):04d}-{int(mo):02d}-{int(dy):02d}"

        cleaned = re.sub(r"^\w+,\s+", "", raw)
        cleaned = re.sub(r"\s+-\s+\d{1,2}:\d{2}.*$", "", cleaned)
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", cleaned, flags=re.I).strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    # ------------------------------------------------------------------
    # List page parser
    # ------------------------------------------------------------------

    def _parse_list_items(self, soup):
        """Return list of item dicts from one page of the releases listing."""
        if soup is None:
            return []

        items = []
        seen = set()

        for container in soup.select("div.left-teaser-text"):
            about = container.get("about", "").strip()
            if not about or "/newsroom/releases/" not in about:
                continue

            url = urljoin(self.base_url, about)
            if url in seen:
                continue
            seen.add(url)

            node_id = container.get("data-history-node-id", "").strip()

            # Title from <h3>
            h3 = container.find("h3")
            a_tag = container.find("a")
            if h3:
                title = self._one_line(h3.get_text(" ", strip=True))
            elif a_tag:
                title = self._one_line(a_tag.get_text(" ", strip=True))
            else:
                title = ""
            if not title:
                continue

            # Date from .dol-date-text
            date_node = container.find("p", class_="dol-date-text")
            raw_date = date_node.get_text(strip=True) if date_node else ""
            listed_date = self._parse_date(raw_date)

            # Short teaser from press body summary
            teaser_div = container.find(class_="field--name-field-press-body")
            teaser = self._one_line(teaser_div.get_text(" ", strip=True)) if teaser_div else ""

            # Agency from URL path: /newsroom/releases/{agency}/{slug}
            parts = urlparse(url).path.strip("/").split("/")
            agency = parts[2] if len(parts) >= 3 else ""
            slug = parts[3] if len(parts) >= 4 else ""

            items.append({
                "url": url,
                "node_id": node_id,
                "title": title,
                "listed_date": listed_date,
                "raw_date": raw_date,
                "teaser": teaser,
                "agency": agency,
                "slug": slug,
            })

        return items

    def _has_more_pages(self, soup, current_page):
        """True if the pager contains any link to a page > current_page."""
        if soup is None:
            return False
        pager = soup.find("nav", id="pag1") or soup.find(class_=re.compile(r"\bpager\b"))
        if pager is None:
            return False
        for a in pager.find_all("a", href=True):
            m = re.search(r"page=(\d+)", a.get("href", ""))
            if m and int(m.group(1)) > current_page:
                return True
        return False

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, raw, item):
        """Parse a DOL release detail page; return a paper dict."""
        soup = self._parse_html(raw, context=item.get("url") or "detail")
        if soup is None:
            raise ValueError("all HTML parsers failed")

        article = (
            soup.select_one("article[data-history-node-id]")
            or soup.select_one("article")
            or soup.select_one("main")
        )
        if article is None:
            raise ValueError("no article/main element found")

        # Guard: access denied page
        h1 = soup.find("h1")
        h1_text = self._one_line(h1.get_text(" ", strip=True) if h1 else "")
        if "access denied" in h1_text.lower():
            raise ValueError(f"access denied: {item.get('url')}")

        # node_id
        node_id = (
            article.get("data-history-node-id", "")
            or item.get("node_id", "")
        ).strip()

        # Title: prefer H1 over list title
        title_node = article.select_one("h1") or soup.select_one("h1")
        title = self._one_line(title_node.get_text(" ", strip=True) if title_node else "")
        if not title:
            title = item.get("title") or ""
        title = re.sub(r"\s*\|\s*U\.S\. Department of Labor.*$", "", title, flags=re.I).strip()

        # Date: scan article for "Month DD, YYYY" text in short leaf nodes;
        # fall back to the listed_date from the list page.
        date_str = ""
        date_pat = re.compile(
            r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},\s+20\d{2}$",
            re.I,
        )
        for node in (article or soup).find_all(
            lambda t: t.name in {"div", "p", "span", "time", "li"}
        ):
            txt = self._one_line(node.get_text(" ", strip=True))
            if date_pat.match(txt):
                date_str = self._parse_date(txt)
                break
        if not date_str:
            date_str = item.get("listed_date", "")

        # Body text: collect <p> paragraphs; stop at boilerplate sections
        _stop_pats = (
            r"^for more information",
            r"^to contact",
            r"^about the u\.?s\.? department of labor",
            r"^about the department of labor",
        )
        body_parts = []
        seen_texts: set = set()
        for p in (article or soup).find_all("p"):
            txt = self._one_line(p.get_text(" ", strip=True))
            if not txt or txt in seen_texts:
                continue
            lower = txt.lower()
            if any(re.match(pat, lower) for pat in _stop_pats):
                break
            seen_texts.add(txt)
            body_parts.append(txt)

        abstract = self._clean_text("\n\n".join(body_parts))

        # Fallback: full article text (rare but handles edge cases)
        if len(abstract) < self._MIN_ABSTRACT_CHARS and article:
            abstract = self._one_line(article.get_text(" ", strip=True))

        # PDF links
        pdf_url = None
        original_filename = None
        for a in (article or soup).find_all("a", href=True):
            href = a.get("href", "").strip()
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                pdf_url = urljoin(self.base_url, href)
                fname = href.rsplit("/", 1)[-1].split("?")[0]
                original_filename = fname if fname.lower().endswith(".pdf") else None
                break

        agency = item.get("agency", "")
        slug = item.get("slug", "")
        agency_full = self._AGENCY_NAMES.get(agency.lower(), agency.upper())

        metadata = {
            "node_id": node_id,
            "agency": agency,
            "agency_full": agency_full,
            "slug": slug,
            "raw_date": item.get("raw_date", ""),
            "listed_date": item.get("listed_date", ""),
            "list_teaser": item.get("teaser", ""),
        }

        return {
            "site_id": self.site_id,
            "external_id": node_id or slug,
            "post_number": node_id or slug,
            "title": title or item.get("title", ""),
            "abstract": abstract,
            "published_date": date_str,
            "url": item.get("url", ""),
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": self._PUBLISHER,
            "department": agency_full,
            "category": self._CATEGORY,
            "keywords": f"news release,DOL,{agency}" if agency else "news release,DOL",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Time budget
    # ------------------------------------------------------------------

    def _time_budget_exhausted(self, started_at):
        return (
            time.monotonic() - started_at
            >= self._MAX_CRAWL_SECONDS - self._TIME_STOP_MARGIN_SECONDS
        )

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Paginate DOL newsroom releases, fetch each detail page, save to DB.

        Parameters
        ----------
        limit:
            Maximum records to save (None = unlimited).
        """
        saved = 0
        page = 0
        seen_urls: set = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        hit_page_cap = False

        try:
            while page < self._SAFETY_PAGE_CAP:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_exhausted(started_at):
                    print(f"[{self.site_id}] 25-minute budget approaching; stopping cleanly")
                    break
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                list_url = self._LIST_ENDPOINT.format(page=page)
                list_html = self._curl_get(
                    list_url,
                    context=f"list page {page}",
                    referer=self.base_url + "/newsroom/releases",
                )
                if not list_html:
                    print(f"[{self.site_id}] empty response on list page {page}; stopping")
                    break

                soup = self._parse_html(list_html, context=f"list page {page}")
                if soup is None:
                    print(f"[{self.site_id}] parse failed on list page {page}; stopping")
                    break

                items = self._parse_list_items(soup)
                if not items:
                    print(f"[{self.site_id}] no items on page {page}; stopping")
                    break

                new_on_page = 0
                for idx, item in enumerate(items, start=1):
                    if limit is not None and saved >= limit:
                        break
                    if self._time_budget_exhausted(started_at):
                        print(f"[{self.site_id}] 25-minute budget approaching; stopping cleanly")
                        return saved

                    item_url = item.get("url") or f"page{page}-item{idx}"
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_on_page += 1

                    try:
                        time.sleep(self.detail_delay)
                        detail_html = self._curl_get(
                            item_url,
                            context=f"detail {item_url}",
                            referer=list_url,
                            is_detail=True,
                        )
                        if not detail_html:
                            print(f"[{self.site_id}] item {item_url} failed: empty response")
                            continue

                        paper = self._parse_detail(detail_html, item)
                        abstract = paper.get("abstract") or ""
                        if len(abstract) < self._MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_url} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                            f"{paper['title'][:80]}"
                        )
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_url} failed: {exc}")
                        continue

                if limit is not None and saved >= limit:
                    break
                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page} had 0 new records; stopping")
                    break
                if not self._has_more_pages(soup, page):
                    print(f"[{self.site_id}] no further pages after page {page}; stopping")
                    break

                page += 1
            else:
                hit_page_cap = True
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user")
            raise

        if hit_page_cap:
            print(
                f"[{self.site_id}] reached safety page cap ({self._SAFETY_PAGE_CAP}); stopping"
            )

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
