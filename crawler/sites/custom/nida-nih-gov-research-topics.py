# -*- coding: utf-8 -*-
"""Crawler for NIDA NIH publications via Elasticsearch API.

Starting URL: https://nida.nih.gov/research-topics/publications
API:          https://api.nida.nih.gov/api/content_index/search
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class NidaNihGovResearchTopicsCrawler(BaseCrawler):
    site_id = "nida-nih-gov-research-topics"
    site_name = "Custom: nida-nih-gov-research-topics"
    base_url = "https://nida.nih.gov"

    _API_URL = "https://api.nida.nih.gov/api/content_index/search"
    _PAGE_SIZE = 25
    # Note: "contentCateogry" typo is intentional — that is the live API param name.
    _CONTENT_CATEGORIES = "Publications,Research Report,DrugFacts"
    _CATEGORY = "Publications"
    _DEPARTMENT = "National Institute on Drug Abuse"
    _RETRY_WAITS = (1, 3, 9)
    _SAFETY_PAGE_CAP = 200
    _MAX_CRAWL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _TIME_STOP_MARGIN_SECONDS = 30
    _MIN_ABSTRACT_CHARS = 50      # skip item entirely if abstract < this
    _MIN_SAVE_ABSTRACT_CHARS = 100  # try detail fetch if API summary < this

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, context="request", accept=None, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: en-US,en;q=0.9",
        ]
        if accept:
            cmd.extend(["-H", f"Accept: {accept}"])
        cmd.append(url)

        last_error = "unknown error"
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode}" + (
                    f" {stderr[:200]}" if stderr else ""
                )
            if attempt < 3:
                wait = self._RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] {context} failed {attempt}/3: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _fetch_api_page(self, from_offset):
        params = urlencode({
            "sort": "unified_date:desc",
            "from": from_offset,
            "size": self._PAGE_SIZE,
            "contentCateogry": self._CONTENT_CATEGORIES,
        })
        url = f"{self._API_URL}?{params}"
        return self._curl_get(
            url, context=f"API offset={from_offset}", accept="application/json"
        )

    def _fetch_detail_page(self, url):
        return self._curl_get(
            url,
            context=f"detail {url[:80]}",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw, *, context="html"):
        text = (
            raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        )
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
        if not raw:
            return ""
        if isinstance(raw, (int, float)):
            try:
                return datetime.utcfromtimestamp(raw).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                return ""
        raw_str = cls._one_line(str(raw))
        if not raw_str:
            return ""
        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw_str)
        if match:
            return match.group(0)
        raw_str = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw_str, flags=re.I)
        for fmt in (
            "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y",
            "%m/%d/%y", "%d %B %Y", "%d %b %Y",
        ):
            try:
                return datetime.strptime(raw_str, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    @staticmethod
    def _first(src, *keys, default=""):
        """Return first non-None element from list-valued fields in src."""
        for key in keys:
            val = src.get(key)
            if isinstance(val, list) and val:
                v = val[0]
                return v if v is not None else default
            if val is not None:
                return val
        return default

    def _meta_content(self, soup, *keys):
        if soup is None:
            return ""
        for key in keys:
            for attr in ("name", "property"):
                node = soup.find("meta", attrs={attr: key})
                if node and node.get("content"):
                    return node["content"].strip()
        return ""

    def _extract_article_text(self, article):
        if article is None:
            return ""
        stop_prefixes = (
            "about the national institute on drug abuse",
            "about the national institutes of health",
            "nih...turning discovery into health",
            "nih…turning discovery into health",
            "disclaimer:",
        )
        parts = []
        for node in article.find_all(["h2", "h3", "p", "li"], recursive=True):
            text = self._one_line(node.get_text(" ", strip=True))
            if not text:
                continue
            lower = text.lower()
            if node.name in {"h2", "h3"} and lower.startswith(("reference", "references")):
                break
            if lower.startswith(stop_prefixes) or lower.startswith("related articles"):
                break
            parts.append(text)
        if not parts:
            text = self._one_line(article.get_text(" ", strip=True))
            if text:
                parts.append(text)
        return self._clean_text("\n\n".join(parts))

    def _fetch_detail_abstract(self, url):
        """Fetch detail page HTML and extract the best available abstract text."""
        raw = self._fetch_detail_page(url)
        if not raw:
            return ""
        soup = self._parse_html(raw, context=url[:80])
        if soup is None:
            return ""
        meta_desc = self._one_line(
            self._meta_content(soup, "description", "abstract", "og:description")
        )
        article = (
            soup.select_one("article[data-history-node-id].content-river")
            or soup.select_one("article[data-history-node-id]")
            or soup.select_one("main article")
            or soup.select_one("main")
        )
        body_text = self._extract_article_text(article) if article else ""

        parts = []
        seen = set()
        for candidate in [meta_desc, body_text]:
            cand = self._clean_text(candidate)
            if not cand:
                continue
            key = cand.lower()[:200]
            if key in seen:
                continue
            seen.add(key)
            parts.append(cand)
            if len("\n\n".join(parts)) >= 500:
                break
        return self._clean_text("\n\n".join(parts))

    def _time_budget_exhausted(self, started_at):
        return (
            time.monotonic() - started_at
            >= self._MAX_CRAWL_SECONDS - self._TIME_STOP_MARGIN_SECONDS
        )

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        from_offset = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        page = 0

        try:
            while page < self._SAFETY_PAGE_CAP:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_exhausted(started_at):
                    print(
                        f"[{self.site_id}] approaching 25 minute crawl budget; stopping cleanly"
                    )
                    break
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                raw = self._fetch_api_page(from_offset)
                if not raw:
                    print(
                        f"[{self.site_id}] empty API response at offset {from_offset}; stopping"
                    )
                    break

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(
                        f"[{self.site_id}] JSON decode error at offset {from_offset}: {exc}"
                    )
                    break

                body = data.get("body", data)
                hits_data = body.get("hits", {})
                total = hits_data.get("total", {}).get("value", 0)
                hits = hits_data.get("hits", [])

                if page == 0:
                    print(f"[{self.site_id}] total records available: {total}")

                if not hits:
                    print(
                        f"[{self.site_id}] no more results at offset {from_offset}; stopping"
                    )
                    break

                new_on_page = 0
                for hit in hits:
                    if limit is not None and saved >= limit:
                        break
                    if self._time_budget_exhausted(started_at):
                        print(
                            f"[{self.site_id}] approaching 25 minute crawl budget; "
                            f"stopping cleanly"
                        )
                        return saved

                    try:
                        src = hit.get("_source", {})
                        hit_id = hit.get("_id", "")

                        url_path = self._first(src, "url")
                        if not url_path:
                            continue
                        full_url = urljoin(self.base_url, url_path)
                        if full_url in seen_urls:
                            continue
                        seen_urls.add(full_url)
                        new_on_page += 1

                        title = self._one_line(self._first(src, "title"))
                        if not title:
                            continue

                        nid = self._first(src, "nid")
                        if nid:
                            external_id = str(nid)
                        elif hit_id:
                            external_id = hit_id.split(":")[-1].split("/")[-1]
                        else:
                            external_id = url_path.rsplit("/", 1)[-1] or url_path

                        api_summary = self._one_line(self._first(src, "summary"))
                        date_raw = self._first(src, "unified_date", "date")
                        published_date = self._parse_date(date_raw)
                        category = (
                            self._one_line(self._first(src, "content_category_name"))
                            or self._CATEGORY
                        )
                        research_topics = src.get("research_topic") or []
                        drug_topics = src.get("drug_topics_name") or []
                        if not isinstance(research_topics, list):
                            research_topics = []
                        if not isinstance(drug_topics, list):
                            drug_topics = []
                        seen_kw: set = set()
                        keywords_list = []
                        for kw in research_topics + drug_topics:
                            if kw and kw not in seen_kw:
                                seen_kw.add(kw)
                                keywords_list.append(kw)

                        abstract = api_summary
                        if len(abstract) < self._MIN_SAVE_ABSTRACT_CHARS:
                            time.sleep(self.detail_delay)
                            fetched = self._fetch_detail_abstract(full_url)
                            if fetched:
                                abstract = fetched

                        if len(abstract) < self._MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {full_url} skipped: "
                                f"abstract {len(abstract)} chars"
                            )
                            continue

                        paper = {
                            "id": external_id,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "title": title,
                            "authors": json.dumps([self._DEPARTMENT], ensure_ascii=False),
                            "abstract": abstract,
                            "category": category,
                            "keywords": json.dumps(keywords_list, ensure_ascii=False),
                            "published_date": published_date,
                            "url": full_url,
                            "pdf_url": "",
                            "doi": "",
                            "department": self._DEPARTMENT,
                            "metadata": json.dumps(
                                {
                                    "source": "NIDA Elasticsearch API",
                                    "hit_id": hit_id,
                                    "api_summary": api_summary,
                                    "content_category_id": self._first(
                                        src, "content_category_id"
                                    ),
                                    "research_topics": research_topics,
                                    "drug_topics": drug_topics,
                                },
                                ensure_ascii=False,
                            ),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_or_inf}: {title[:80]}"
                        )

                    except Exception as exc:
                        print(
                            f"[{self.site_id}] item {hit.get('_id', '?')} failed: {exc}"
                        )
                        continue

                if limit is not None and saved >= limit:
                    break
                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page} had 0 new records; stopping")
                    break
                if from_offset + self._PAGE_SIZE >= total:
                    print(f"[{self.site_id}] reached end of results at offset {from_offset}; stopping")
                    break

                from_offset += self._PAGE_SIZE
                page += 1
            else:
                print(
                    f"[{self.site_id}] reached safety page cap of {self._SAFETY_PAGE_CAP}; stopping"
                )

        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
