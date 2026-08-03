# -*- coding: utf-8 -*-
"""Crawler for minagric.gr press releases (ΥπΑΑΤ Γραφείο Τύπου)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_GREEK_MONTHS = {
    "ιαν": "01", "φεβ": "02", "μάρ": "03", "μαρ": "03",
    "απρ": "04", "μάι": "05", "μαϊ": "05", "μαι": "05",
    "ιούν": "06", "ιουν": "06", "ιούλ": "07", "ιουλ": "07",
    "αύγ": "08", "αυγ": "08", "σεπ": "09", "οκτ": "10",
    "νοέ": "11", "νοε": "11", "δεκ": "12",
}


class MinagricGrTheMinistry2Crawler(BaseCrawler):
    site_id = "minagric-gr-the-ministry-2"
    site_name = "Custom: minagric-gr-the-ministry-2"
    base_url = "https://minagric.gr"

    START_URL = "https://minagric.gr/the-ministry-2/grafeiotypou/press-releases-ypaat-current-year"
    CURL_TIMEOUT = 45
    BACKOFF = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    MIN_SAVE_ABSTRACT_CHARS = 100
    _CURL_MARKER = "__MINAGRIC_META__:"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        list_url = self.START_URL
        raw = self._curl_get(list_url, context="list page")
        if not raw:
            print(f"[{self.site_id}] failed to fetch list page; stopping")
            return 0

        soup = self._make_soup(raw, context="list page")
        if soup is None:
            print(f"[{self.site_id}] failed to parse list page; stopping")
            return 0

        records = self._parse_list(soup, list_url)
        if not records:
            print(f"[{self.site_id}] no records found on list page; stopping")
            return 0

        print(f"[{self.site_id}] list page: discovered {len(records)} records")

        for idx, record in enumerate(records, start=1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_WALL:
                print(f"[{self.site_id}] wall-clock budget (25 min) exceeded; stopping")
                break

            try:
                detail_url = record.get("url") or ""
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                time.sleep(self._delay)
                detail_raw = self._curl_get(
                    detail_url, context=f"item {idx} detail", referer=list_url
                )
                if not detail_raw:
                    raise RuntimeError("detail fetch failed after retries")

                detail_soup = self._make_soup(detail_raw, context=f"item {idx} detail")
                if detail_soup is None:
                    raise RuntimeError("could not parse detail HTML")

                parsed = self._parse_detail(detail_soup, record)
                abstract = parsed.get("abstract") or ""
                if len(abstract) < self.MIN_ABSTRACT_CHARS:
                    print(
                        f"[{self.site_id}] item {idx} skipped: abstract too short "
                        f"({len(abstract)} chars)"
                    )
                    continue
                if len(abstract) < self.MIN_SAVE_ABSTRACT_CHARS:
                    print(
                        f"[{self.site_id}] item {idx} skipped: abstract below 100-char threshold"
                    )
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": parsed["external_id"],
                    "url": parsed["url"],
                    "title": parsed["title"],
                    "abstract": abstract,
                    "published_date": parsed["published_date"],
                    "posted_date": parsed["listed_date"],
                    "authors": parsed.get("authors"),
                    "publisher": parsed.get("publisher"),
                    "keywords": parsed.get("keywords"),
                    "pdf_url": parsed.get("pdf_url"),
                    "original_filename": parsed.get("original_filename"),
                    "metadata": json.dumps(parsed.get("metadata", {}), ensure_ascii=False),
                }
                self._save_paper(paper)
                saved += 1
                limit_label = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] saved {saved}/{limit_label}: {parsed['title'][:80]}")

                if idx % 10 == 0:
                    print(f"[{self.site_id}] page 1: saved {saved}/{limit_label}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list(self, soup, list_url):
        records = []
        seen: set[str] = set()

        raxo = soup.select_one("div.raxo-allmode-pro")
        container = raxo if raxo else soup.select_one("main, #sp-main-body") or soup

        for article in container.select("article"):
            link = article.select_one("h4.raxo-title a[href], a[href]")
            if link is None:
                continue
            href = link.get("href", "").strip()
            if not href or "deltiatypou" not in href:
                continue
            url = urljoin(self.base_url, href)
            if url in seen:
                continue
            seen.add(url)

            title = re.sub(r"\s+", " ", link.get_text(strip=True)).strip()
            date_node = article.select_one(".raxo-date, .raxo-info span")
            date_text = date_node.get_text(strip=True) if date_node else ""

            post_number = self._post_number_from_url(url)
            records.append({
                "title": title,
                "url": url,
                "external_id": post_number or href.rstrip("/").split("/")[-1],
                "post_number": post_number,
                "listed_date": self._parse_greek_date(date_text),
                "listed_date_text": date_text,
                "list_url": list_url,
            })

        # fallback: scan all anchor tags in main
        if not records:
            main = soup.select_one("main, #sp-main-body") or soup
            for a in main.find_all("a", href=True):
                href = a["href"]
                if "deltiatypou" not in href:
                    continue
                url = urljoin(self.base_url, href)
                if url in seen:
                    continue
                seen.add(url)
                title = re.sub(r"\s+", " ", a.get_text(strip=True)).strip()
                if not title:
                    continue
                post_number = self._post_number_from_url(url)
                records.append({
                    "title": title,
                    "url": url,
                    "external_id": post_number or href.rstrip("/").split("/")[-1],
                    "post_number": post_number,
                    "listed_date": "",
                    "listed_date_text": "",
                    "list_url": list_url,
                })

        return records

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, soup, record):
        url = record.get("url", "")

        # Title from breadcrumb (most reliable)
        title = ""
        bc_active = soup.select("li.mod-breadcrumbs__item.active span, ol.breadcrumb li.active span")
        if bc_active:
            title = re.sub(r"\s+", " ", bc_active[-1].get_text(strip=True)).strip()
        if not title:
            # fallback: first centered <strong> inside article-details
            strong = soup.select_one("div.article-details p strong")
            if strong:
                title = re.sub(r"\s+", " ", strong.get_text(strip=True)).strip()
        if not title:
            title = record.get("title", "")

        # Date from <time datetime="...">
        published_date = ""
        time_node = soup.select_one("span.published time[datetime], time[datetime]")
        if time_node:
            dt_attr = time_node.get("datetime", "")
            m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_attr)
            if m:
                published_date = m.group(1)
        if not published_date:
            published_date = record.get("listed_date", "")

        # Category
        cat_node = soup.select_one("span.category-name a")
        category = cat_node.get_text(strip=True) if cat_node else "Δελτία Τύπου"

        # Article body — div.article-details, skip .article-info and .article-can-edit
        article_div = soup.select_one("div.article-details")
        abstract = ""
        pdf_url = None
        original_filename = None

        if article_div:
            # Find PDF links first (before decomposing anything)
            for a in article_div.find_all("a", href=True):
                href = a["href"]
                if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                    pdf_url = urljoin(self.base_url, href)
                    tail = href.rstrip("/").split("/")[-1].split("?")[0]
                    original_filename = tail if tail else None
                    break

            # Extract text from paragraphs/list items, skipping info sections
            parts = []
            for tag in article_div.find_all(["p", "li", "h2", "h3", "h4"]):
                # skip tags inside article-info or article-can-edit
                if tag.find_parent(class_="article-info") or tag.find_parent(
                    class_="article-can-edit"
                ):
                    continue
                text = re.sub(r"\s+", " ", tag.get_text(separator=" ", strip=True)).strip()
                if text and text not in parts:
                    parts.append(text)

            abstract = "\n\n".join(parts).strip()
            if not abstract:
                # last-resort: raw text of entire div minus info sections
                clone_text = []
                for child in article_div.children:
                    if hasattr(child, "get") and child.get("class"):
                        cls = " ".join(child.get("class", []))
                        if "article-info" in cls or "article-can-edit" in cls:
                            continue
                    clone_text.append(
                        re.sub(r"\s+", " ", child.get_text(separator=" ", strip=True)).strip()
                        if hasattr(child, "get_text")
                        else ""
                    )
                abstract = "\n".join(t for t in clone_text if t).strip()

        post_number = record.get("post_number") or self._post_number_from_url(url)
        external_id = post_number or record.get("external_id") or url.rstrip("/").split("/")[-1]

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": record.get("listed_date", ""),
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "authors": None,
            "publisher": "Υπουργείο Αγροτικής Ανάπτυξης και Τροφίμων",
            "keywords": category,
            "metadata": {
                "posted_date": record.get("listed_date_text", ""),
                "category": category,
                "list_url": record.get("list_url", ""),
            },
        }

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: el-GR,el;q=0.9,en;q=0.7",
            "-w", "\n" + self._CURL_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10)
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _ = self._split_curl_output(stdout, url)
                if result.returncode != 0:
                    raise RuntimeError(f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} attempt {attempt}/3 failed: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} gave up after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        pos = raw.rfind("\n" + self._CURL_MARKER)
        if pos == -1:
            return raw, "", fallback_url
        body = raw[:pos]
        meta = raw[pos + 1 + len(self._CURL_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, eff_url = meta.split("\t", 1)
        return body, http_code.strip(), eff_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    @staticmethod
    def _post_number_from_url(url):
        path = urlparse(url).path
        # URLs like /deltiatypou/19466-dt290526
        m = re.search(r"/(\d+)-\w+/?$", path)
        if m:
            return m.group(1)
        # fallback: last numeric segment
        m = re.search(r"/(\d+)/?$", path)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _parse_greek_date(text):
        """Parse Greek date strings like 'Μάι 29, 2026' → '2026-05-29'."""
        if not text:
            return ""
        # Try ISO first
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            return m.group(1)
        # Greek abbreviated month
        m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
        if m:
            day, month_raw, year = m.group(1), m.group(2), m.group(3)
            key = month_raw.lower()[:3]
            # strip accents for matching
            key = key.replace("ά", "α").replace("έ", "ε").replace("ί", "ι").replace("ό", "ο").replace("ύ", "υ").replace("ώ", "ω")
            # Greek abbreviated with accent variations
            month_num = _GREEK_MONTHS.get(month_raw.lower()[:3])
            if not month_num:
                # try normalised key
                month_num = _GREEK_MONTHS.get(key)
            if month_num:
                return f"{int(year):04d}-{month_num}-{int(day):02d}"
        # "Μάι 29, 2026" format
        m = re.search(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", text)
        if m:
            month_raw, day, year = m.group(1), m.group(2), m.group(3)
            month_num = _GREEK_MONTHS.get(month_raw.lower()[:3])
            if month_num:
                return f"{int(year):04d}-{month_num}-{int(day):02d}"
        return ""
