# -*- coding: utf-8 -*-
"""Crawler for the National Institute on Aging (NIA) News Releases feed.

Starting URL: https://www.nia.nih.gov/news/news-releases

The site is a Drupal 9/10 instance (USWDS card markup). The listing uses
the standard Drupal pager (``?page=N``, 0-indexed) and renders each item as
``li.usa-card.usa-card--flag`` with the headline link in
``h2.usa-card__heading a`` and the list date in
``.card--meta-info time[datetime]``. Detail pages expose the publish date
via ``.postdate time[datetime]``, the body in ``.content_full``, and the
numeric Drupal node id via the ``drupal-settings-json`` inline script
(``"currentPath":"node/1234"``). The site sits behind an AWS WAF that
returns a "Human Verification" challenge page (HTTP 405) if requests are
fired too quickly without a rate-limiting delay.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


SITE_ID = "nia-nih-gov-news"

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _make_soup(raw: str, *, context: str = ""):
    """Parse malformed HTML defensively: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup

    last_error = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            last_error = exc
            print(f"[{SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")
            continue
    print(f"[{SITE_ID}] all HTML parsers failed for {context}: {last_error}")
    return None


def _iso_date(raw: Any) -> Optional[str]:
    if not raw:
        return None
    text = _clean_text(raw)
    if not text:
        return None

    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if match:
        return match.group(1)

    try:
        normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
        return datetime.fromisoformat(normalized).date().isoformat()
    except Exception:
        pass

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _filename_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        from urllib.parse import unquote

        path = urlparse(url).path
        tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if "." in tail and 0 < len(tail) <= 220:
            return tail
    except Exception:
        return None
    return None


def _json_dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class NiaNihGovNewsCrawler(BaseCrawler):
    site_id = "nia-nih-gov-news"
    site_name = "Custom: nia-nih-gov-news"
    base_url = "https://www.nia.nih.gov"

    _LIST_PATH = "/news/news-releases"
    _CATEGORY_LABEL = "News Release"
    _PUBLISHER = "National Institute on Aging (NIA)"
    _SAFETY_CAP = 200
    _WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 50
    _BACKOFF_SECONDS = (5, 15, 45, 90)
    # Markers seen when the AWS WAF/bot-mitigation kicks in after a burst of
    # requests: either the documented "Human Verification" 405 challenge, or
    # (observed live) an HTTP-200 fallback shell whose body is just a
    # "JavaScript is disabled" notice with no real card/article markup. Both
    # must be treated as a failed fetch so _curl_get retries with backoff
    # instead of returning bogus content that silently truncates the crawl.
    _BLOCK_MARKERS = ("Human Verification", "JavaScript is disabled")

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        for page in range(self._SAFETY_CAP):
            if time.time() - start_time > self._WALL_BUDGET_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if (page + 1) % 10 == 0:
                print(f"[{self.site_id}] page {page + 1}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            try:
                raw = self._curl_get(list_url, label=f"list page {page}")
            except KeyboardInterrupt:
                raise

            if not raw:
                print(f"[{self.site_id}] page {page}: fetch failed or empty; stopping")
                break

            items = self._parse_list_page(raw, list_url)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                item_url = item.get("url")
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(item_url, label=f"item {index} (page {page})")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_url} failed: empty detail response")
                        continue

                    detail = self._parse_detail_page(detail_raw, item_url)
                    paper = self._build_paper(item, detail)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping '{paper.get('title', '')[:70]}' "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no unseen records; stopping")
                break
        else:
            print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP} pages reached")

        print(f"[{self.site_id}] done: saved {saved}")
        return saved

    def _list_url(self, page: int) -> str:
        base = f"{self.base_url}{self._LIST_PATH}"
        if page <= 0:
            return base
        return f"{base}?page={page}"

    def _curl_get(self, url: str, *, label: str = "", timeout: int = 45) -> Optional[str]:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            url,
        ]

        for attempt, wait in enumerate(self._BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                blocked = next((m for m in self._BLOCK_MARKERS if m in body[:4000]), None)
                if result.returncode == 0 and body.strip() and blocked is None:
                    return body

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                reason = stderr or f"curl exit {result.returncode}, body length {len(body)}"
                if blocked is not None:
                    reason = f"WAF/bot-mitigation challenge ({blocked!r})"
                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] {label or url} failed after 3 attempts: {reason}")
                    return None
                print(f"[{self.site_id}] {label or url} attempt {attempt}/3 failed: {reason}; retrying in {wait}s")
                time.sleep(wait)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] {label or url} failed after 3 attempts: {exc}")
                    return None
                print(f"[{self.site_id}] {label or url} attempt {attempt}/3 failed: {exc}; retrying in {wait}s")
                time.sleep(wait)
        return None

    def _parse_list_page(self, raw: str, page_url: str) -> list[dict]:
        soup = _make_soup(raw, context=page_url)
        if soup is None:
            return []

        items = []
        for card in soup.select("li.usa-card--flag"):
            try:
                heading = card.select_one("h2.usa-card__heading a[href]")
                if heading is None:
                    continue
                item_url = urljoin(self.base_url, heading.get("href", ""))
                if not item_url.startswith(self.base_url):
                    continue

                title = _clean_text(heading.get_text(" ", strip=True))
                if not title:
                    continue

                meta_el = card.select_one(".card--meta-info time[datetime]")
                listed_raw = meta_el.get("datetime") if meta_el else None
                if not listed_raw and meta_el:
                    listed_raw = meta_el.get_text(" ", strip=True)
                listed_date = _iso_date(listed_raw)

                body_el = card.select_one(".usa-card__body")
                list_snippet = _clean_text(body_el.get_text(" ", strip=True)) if body_el else ""

                items.append(
                    {
                        "title": title,
                        "url": item_url,
                        "list_snippet": list_snippet,
                        "listed_raw": listed_raw,
                        "listed_date": listed_date,
                        "list_endpoint": page_url,
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] list card parse failed: {exc}")
                continue

        return items

    def _parse_detail_page(self, raw: str, url: str) -> dict:
        soup = _make_soup(raw, context=url)
        if soup is None:
            return {"abstract": "", "url": url}

        title_el = soup.select_one("h1.page-title") or soup.select_one("h1")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else None

        time_tag = soup.select_one(".postdate time[datetime]") or soup.select_one("time[datetime]")
        published_raw = _clean_text(time_tag.get("datetime")) if time_tag else None
        if not published_raw and time_tag:
            published_raw = _clean_text(time_tag.get_text(" ", strip=True))
        published_date = _iso_date(published_raw)

        intro_el = soup.select_one(".special_intro")
        intro_text = _clean_text(intro_el.get_text(" ", strip=True)) if intro_el else ""

        body_el = soup.select_one(".content_full")
        body_text, pdf_url, doi = self._detail_body(body_el, url)

        abstract = " ".join(part for part in (intro_text, body_text) if part).strip()

        node_id = self._node_id_from_settings(soup)

        original_filename = _filename_from_url(pdf_url)

        return {
            "abstract": abstract,
            "doi": doi,
            "node_id": node_id,
            "original_filename": original_filename,
            "pdf_url": pdf_url,
            "published_date": published_date,
            "published_raw": published_raw,
            "title": title,
            "url": url,
        }

    def _detail_body(self, body_el, page_url: str) -> tuple[str, Optional[str], Optional[str]]:
        if body_el is None:
            return "", None, None

        clone = _make_soup(str(body_el), context="detail body fragment")
        if clone is None:
            return "", None, None

        pdf_url = None
        for link in clone.select("a[href]"):
            href = link.get("href") or ""
            if ".pdf" in href.lower():
                pdf_url = urljoin(page_url, href)
                break

        for noise in clone.find_all(["script", "style", "nav", "form", "footer", "header"]):
            noise.decompose()

        text = _clean_text(clone.get_text(" ", strip=True))
        doi_match = _DOI_RE.search(text)
        doi = doi_match.group(0).rstrip(".,)") if doi_match else None
        return text, pdf_url, doi

    def _node_id_from_settings(self, soup) -> Optional[str]:
        script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if script is None:
            return None
        # NOTE: html5lib marks <script> contents as a special NavigableString
        # subclass that get_text() silently drops; read .contents directly.
        raw = "".join(str(c) for c in script.contents).strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            match = re.search(r'"currentPath":\s*"node\\?/(\d+)"', raw)
            return match.group(1) if match else None
        current_path = (data.get("path") or {}).get("currentPath") or ""
        match = re.match(r"node/(\d+)", current_path)
        return match.group(1) if match else None

    def _build_paper(self, item: dict, detail: dict) -> dict:
        node_id = detail.get("node_id")
        external_id = node_id or self._slug_from_url(item.get("url") or detail.get("url"))
        post_number = node_id if node_id and re.fullmatch(r"\d+", node_id) else external_id

        title = detail.get("title") or item.get("title") or "(untitled)"
        published_date = detail.get("published_date") or item.get("listed_date")
        listed_date = item.get("listed_date") or published_date

        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)

        metadata = {
            "posted_date": item.get("listed_raw"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "published_raw": detail.get("published_raw"),
            "list_snippet": item.get("list_snippet"),
            "list_endpoint": item.get("list_endpoint"),
            "detail_endpoint": detail.get("url") or item.get("url"),
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": detail.get("abstract") or "",
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": None,
            "journal": None,
            "url": detail.get("url") or item.get("url"),
            "pdf_url": pdf_url,
            "keywords": None,
            "category": self._CATEGORY_LABEL,
            "doi": detail.get("doi"),
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def _slug_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        try:
            from urllib.parse import unquote

            return unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or None
        except Exception:
            return None
