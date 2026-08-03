# -*- coding: utf-8 -*-
"""Crawler for Greek Ministry of Finance (minfin.gov.gr) press releases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class MinfinGovGrGrafeioTypouCrawler(BaseCrawler):
    site_id = "minfin-gov-gr-grafeio-typou"
    site_name = "Custom: minfin-gov-gr-grafeio-typou"
    base_url = "https://minfin.gov.gr"

    START_URL = "https://minfin.gov.gr/grafeio-typou/anakoinoseis-typou-el/"
    _API_URL = "https://minfin.gov.gr/wp-json/wp/v2/posts"
    _CATEGORY_ID = 62        # "Ανακοινώσεις Τύπου"
    _PER_PAGE = 100
    _BACKOFF = (1, 3, 9)
    _CURL_TIMEOUT = 45
    _MIN_ABSTRACT = 50       # chars — skip below this
    _MIN_SAVE = 100          # chars — skip save below this
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))         # safety cap
    _MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes
    _MARKER = "__MINFIN_META__:"

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        t_start = time.time()
        limit_display = limit if limit is not None else "∞"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")
                break
            if time.time() - t_start >= self._MAX_WALL_SECS:
                print(f"[{self.site_id}] wall-clock budget exceeded; stopping")
                break

            api_url = (
                f"{self._API_URL}?categories={self._CATEGORY_ID}"
                f"&per_page={self._PER_PAGE}&page={page}"
                f"&_fields=id,date,title,link,content,excerpt"
            )
            raw = self._curl_get(api_url, context=f"API page {page}")
            if not raw:
                print(f"[{self.site_id}] API page {page} failed; stopping")
                break

            try:
                posts = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON parse error at page {page}: {exc}; stopping")
                break

            if not isinstance(posts, list) or len(posts) == 0:
                print(f"[{self.site_id}] page {page} returned no posts; done")
                break

            if page % 10 == 0 or page == 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            for post in posts:
                if limit is not None and saved >= limit:
                    break
                try:
                    url = post.get("link", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    post_id = str(post.get("id", ""))
                    title_raw = (post.get("title") or {}).get("rendered", "")
                    title = _one_line(BeautifulSoup(title_raw, "html.parser").get_text(" ", strip=True))
                    if not title:
                        title = "(untitled)"

                    date_str = (post.get("date") or "")[:10]  # YYYY-MM-DD

                    content_html = (post.get("content") or {}).get("rendered", "")
                    abstract = _extract_text(content_html, self.site_id)

                    if len(abstract) < self._MIN_ABSTRACT:
                        print(f"[{self.site_id}] post {post_id} skipped: abstract {len(abstract)} chars < {self._MIN_ABSTRACT}")
                        continue
                    if len(abstract) < self._MIN_SAVE:
                        print(f"[{self.site_id}] post {post_id} skipped: abstract below save threshold ({len(abstract)} chars)")
                        continue

                    pdf_url = _find_pdf_url(content_html, self.base_url)
                    original_filename = _filename_from_url(pdf_url) if pdf_url else None

                    metadata = json.dumps({
                        "source": "minfin.gov.gr WP REST API",
                        "post_id": post.get("id"),
                        "date_raw": post.get("date", ""),
                        "api_page": page,
                        "posted_date": date_str,
                    }, ensure_ascii=False)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "post_number": post_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str,
                        "posted_date": date_str,
                        "url": url,
                        "pdf_url": pdf_url or None,
                        "original_filename": original_filename,
                        "authors": None,
                        "publisher": "Υπουργείο Εθνικής Οικονομίας και Οικονομικών",
                        "department": None,
                        "journal": None,
                        "keywords": "Ανακοινώσεις Τύπου",
                        "category": "Ανακοινώσεις Τύπου",
                        "doi": None,
                        "metadata": metadata,
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {title[:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] post {post.get('id', '?')} failed: {exc}")
                    continue

            # End-of-pagination: fewer results than requested
            if len(posts) < self._PER_PAGE:
                print(f"[{self.site_id}] last page reached (got {len(posts)}); done")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self._CURL_TIMEOUT),
            "-H", "Accept: application/json, text/html, */*;q=0.8",
            "-w", "\n" + self._MARKER + "%{http_code}\t%{url_effective}",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self._CURL_TIMEOUT + 10)
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _ = self._split_curl(stdout, url)
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl attempt {attempt}/3: {last_error}")
                if attempt < 3:
                    wait = self._BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _split_curl(self, raw: str, fallback_url: str):
        marker_pos = raw.rfind("\n" + self._MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, eff_url = meta.split("\t", 1)
        return body, http_code.strip(), eff_url.strip() or fallback_url


# ------------------------------------------------------------------
# Module-level helpers (no state needed)
# ------------------------------------------------------------------

def _one_line(value: str) -> str:
    text = unescape(str(value or "")).replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_text(html: str, site_id: str = "") -> str:
    if not html:
        return ""
    soup = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            soup = BeautifulSoup(html, parser)
            break
        except Exception as exc:
            if site_id:
                print(f"[{site_id}] BeautifulSoup({parser}) failed: {exc}")
    if soup is None:
        return re.sub(r"<[^>]+>", " ", html).strip()

    for bad in soup.select("script, style, noscript"):
        bad.decompose()

    parts = []
    for el in soup.find_all(["p", "li", "h2", "h3", "blockquote"]):
        txt = _one_line(el.get_text(" ", strip=True))
        if txt and txt not in parts:
            parts.append(txt)

    if parts:
        return "\n\n".join(parts)
    return _one_line(soup.get_text(" ", strip=True))


def _find_pdf_url(html: str, base_url: str) -> str | None:
    if not html:
        return None
    soup = None
    for parser in ("html.parser", "lxml"):
        try:
            soup = BeautifulSoup(html, parser)
            break
        except Exception:
            pass
    if soup is None:
        return None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
            return urljoin(base_url, href)
    return None


def _filename_from_url(url: str) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    name = path.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return name if name else None
