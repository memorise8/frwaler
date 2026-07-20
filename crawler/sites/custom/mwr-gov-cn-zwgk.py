# -*- coding: utf-8 -*-
"""Crawler for MWR (Ministry of Water Resources) 政府信息公开年报.

Target: http://www.mwr.gov.cn/zwgk/gknb/
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "mwr-gov-cn-zwgk"
_BASE_URL = "http://www.mwr.gov.cn"
_LIST_URL = "http://www.mwr.gov.cn/zwgk/gknb/"
_ABSTRACT_MIN_CHARS = 50
_MAX_PAGES = 200


class MwrGovCnZwgkCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: mwr-gov-cn-zwgk"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value) -> str:
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str) -> list[dict]:
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        for div in soup.select("div.itemb"):
            try:
                a_tag = div.select_one("a[href]")
                if not a_tag:
                    continue
                href = a_tag.get("href", "").strip()
                if not href:
                    continue
                url = urljoin(_LIST_URL, href)

                link_text = self._one_line(a_tag.get_text(" ", strip=True))
                date_match = re.search(r"(\d{4}-\d{2}-\d{2})\s*$", link_text)
                if date_match:
                    listed_date = date_match.group(1)
                    title = link_text[: date_match.start()].strip()
                else:
                    listed_date = ""
                    title = link_text

                fname = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
                m = re.search(r"_(\d+)\.html$", fname)
                external_id = m.group(1) if m else fname.replace(".html", "")

                items.append({
                    "url": url,
                    "title": title,
                    "listed_date": listed_date,
                    "external_id": external_id,
                    "post_number": external_id,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list item parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _extract_abstract(self, soup) -> str:
        # Primary: .gknb_content div
        content_div = soup.select_one("div.gknb_content")
        if content_div:
            for bad in content_div.select("script, style, noscript"):
                bad.decompose()
            text = self._clean(content_div.get_text(" ", strip=True))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        # Fallback: .view.TRS_UEDITOR
        view_div = soup.select_one("div.view.TRS_UEDITOR, div.TRS_UEDITOR")
        if view_div:
            for bad in view_div.select("script, style, noscript"):
                bad.decompose()
            text = self._clean(view_div.get_text(" ", strip=True))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        # Fallback: .roll (the wider section container)
        roll_div = soup.select_one("div.roll")
        if roll_div:
            for bad in roll_div.select("script, style, noscript"):
                bad.decompose()
            text = self._clean(roll_div.get_text(" ", strip=True))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        return ""

    def _parse_detail(self, raw: str, item: dict) -> dict:
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed on detail page")

        url = item["url"]
        title = item.get("title", "")
        listed_date = item.get("listed_date", "")
        external_id = item["external_id"]
        post_number = item.get("post_number", "")

        # Prefer meta tag title (authoritative)
        meta_title = soup.find("meta", attrs={"name": "ArticleTitle"})
        if meta_title and meta_title.get("content"):
            t = self._one_line(meta_title.get("content", ""))
            if t:
                title = t

        # Date from PubDate meta
        published_date = listed_date
        meta_pubdate = soup.find("meta", attrs={"name": "PubDate"})
        if meta_pubdate and meta_pubdate.get("content"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", meta_pubdate.get("content", ""))
            if m:
                published_date = m.group(1)

        # Publisher
        publisher = "水利部"
        meta_source = soup.find("meta", attrs={"name": "ContentSource"})
        if meta_source and meta_source.get("content"):
            s = meta_source.get("content", "").strip()
            if s:
                publisher = s

        abstract = self._extract_abstract(soup)

        # PDF link scan
        pdf_url = None
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                if href.startswith("/"):
                    pdf_url = urljoin(_BASE_URL, href)
                    break
                if href.startswith("http"):
                    pdf_url = href
                    break

        metadata = {
            "source": "MWR 政府信息公开年报",
            "list_url": _LIST_URL,
            "posted_date": listed_date,
        }

        return {
            "id": external_id,
            "site_id": _SITE_ID,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "authors": "[]",
            "category": "政府信息公开年报",
            "keywords": "政府信息公开,年报,水利部",
            "published_date": published_date,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": publisher,
            "publisher": publisher,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        crawl_start = time.time()

        for page in range(_MAX_PAGES):
            elapsed = time.time() - crawl_start
            if elapsed > 25 * 60:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            try:
                raw = self._curl(_LIST_URL, referer=_BASE_URL + "/")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] failed to fetch list page {page}: {exc}")
                break

            if not raw:
                print(f"[{_SITE_ID}] empty list response at page {page}; stopping")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] no items on page {page}; stopping")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] all URLs on page {page} already seen; stopping")
                break

            if page == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._detail_delay)
                    raw_detail = self._curl(url, referer=_LIST_URL)
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {url} failed: empty detail response")
                        continue

                    paper = self._parse_detail(raw_detail, item)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] item {url} skipped: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

            # This site has a single non-paginated listing; stop after one pass.
            break

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
