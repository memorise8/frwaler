# -*- coding: utf-8 -*-
"""Crawler for Ames National Laboratory news.

Starting URL: https://www.ameslab.gov/news

The starting page is a landing page with three Drupal Views listings:
News Releases (/news/news-releases), In the News (/news/in-the-news), and
Feature Stories (/news/feature-stories). Each listing uses the standard
Drupal pager (``?page=N``, 0-indexed) and card markup
(``article.card a.card__link``). Detail pages expose the publish date via
``<time datetime="...">`` in the hero, the body text in ``.textual``, and
the numeric Drupal node id via the ``drupal-settings-json`` inline script
(``currentPath": "node/1234"``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from email.message import Message
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


SITE_ID = "ameslab-gov-news"

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
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if "." in tail and 0 < len(tail) <= 220:
            return tail
    except Exception:
        return None
    return None


def _filename_from_content_disposition(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    try:
        msg = Message()
        msg["content-disposition"] = header_value
        filename = msg.get_param("filename", header="content-disposition")
        if filename:
            filename = unquote(str(filename).strip().strip('"'))
            if 0 < len(filename) <= 220:
                return filename
    except Exception:
        return None
    return None


def _json_dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class AmeslabGovNewsCrawler(BaseCrawler):
    site_id = "ameslab-gov-news"
    site_name = "Custom: ameslab-gov-news"
    base_url = "https://www.ameslab.gov"

    _CATEGORIES = (
        ("news-releases", "News Release"),
        ("in-the-news", "In the News"),
        ("feature-stories", "Feature Story"),
    )
    _SAFETY_CAP = 200
    _WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 50
    _BACKOFF_SECONDS = (1, 3, 9)
    _PUBLISHER = "Ames National Laboratory"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"
        page_counter = 0
        budget_exceeded = False

        for slug, category_label in self._CATEGORIES:
            if budget_exceeded:
                break
            if limit is not None and saved >= limit:
                break

            for page in range(self._SAFETY_CAP):
                if time.time() - start_time > self._WALL_BUDGET_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
                    budget_exceeded = True
                    break

                if limit is not None and saved >= limit:
                    break

                page_counter += 1
                if page_counter % 10 == 0:
                    print(f"[{self.site_id}] page {page_counter}: saved {saved}/{limit_or_inf}")

                list_url = self._list_url(slug, page)
                try:
                    raw = self._curl_get(list_url, label=f"{slug} list page {page}")
                except KeyboardInterrupt:
                    raise

                if not raw:
                    print(f"[{self.site_id}] {slug} page {page}: fetch failed or empty; stopping category")
                    break

                items = self._parse_list_page(raw, list_url, category_label)
                if not items:
                    print(f"[{self.site_id}] {slug} page {page}: 0 records; stopping category")
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
                        detail_raw = self._curl_get(item_url, label=f"item {index} ({slug})")
                        if not detail_raw:
                            print(f"[{self.site_id}] item {item_url} failed: empty detail response")
                            continue

                        detail = self._parse_detail_page(detail_raw, item_url, category_label)
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
                    print(f"[{self.site_id}] {slug} page {page}: no unseen records; stopping category")
                    break
            else:
                print(f"[{self.site_id}] {slug}: category page loop exhausted range (unexpected)")

            if page_counter >= self._SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP} pages reached")
                break

        print(f"[{self.site_id}] done: saved {saved}")
        return saved

    def _list_url(self, slug: str, page: int) -> str:
        base = f"{self.base_url}/news/{slug}"
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
                if result.returncode == 0 and body.strip():
                    return body

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                reason = stderr or f"curl exit {result.returncode}, body length {len(body)}"
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

    def _curl_head_content_disposition(self, url: str) -> Optional[str]:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skIL",
            "--connect-timeout",
            "10",
            "--max-time",
            "20",
            "-A",
            self.USER_AGENT,
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=25, check=False)
            if result.returncode != 0:
                return None
            headers = result.stdout.decode("utf-8", errors="replace")
            for line in headers.splitlines():
                if line.lower().startswith("content-disposition:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            return None
        return None

    def _parse_list_page(self, raw: str, page_url: str, category_label: str) -> list[dict]:
        soup = _make_soup(raw, context=page_url)
        if soup is None:
            return []

        items = []
        for card in soup.select("article.card"):
            try:
                link = card.select_one("a.card__link[href]")
                if link is None:
                    continue
                item_url = urljoin(self.base_url, link.get("href", ""))
                if not item_url.startswith(self.base_url):
                    continue

                title_el = card.select_one("h3.card__title")
                title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
                if not title:
                    continue

                subtext_el = card.select_one("p.card__subtext")
                list_snippet = _clean_text(subtext_el.get_text(" ", strip=True)) if subtext_el else ""

                items.append(
                    {
                        "title": title,
                        "url": item_url,
                        "list_snippet": list_snippet,
                        "category": category_label,
                        "list_endpoint": page_url,
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] list card parse failed: {exc}")
                continue

        return items

    def _parse_detail_page(self, raw: str, url: str, category_label: str) -> dict:
        soup = _make_soup(raw, context=url)
        if soup is None:
            return {"abstract": "", "url": url}

        hero = soup.select_one("section.hero") or soup.select_one("main")
        title_el = (hero.select_one("h1") if hero else None) or soup.select_one("h1")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else None

        time_tag = (hero.select_one("time") if hero else None) or soup.select_one("time")
        published_raw = _clean_text(time_tag.get("datetime")) if time_tag else None
        if not published_raw and time_tag:
            published_raw = _clean_text(time_tag.get_text(" ", strip=True))
        published_date = _iso_date(published_raw)

        textual = soup.select_one(".textual")
        abstract, pdf_url, doi = self._detail_body(textual, url)

        tags_section = soup.select_one(".links-blue-no-underline")
        keywords = self._detail_tags(tags_section)

        node_id = self._node_id_from_settings(soup)

        original_filename = _filename_from_url(pdf_url)
        if pdf_url and not original_filename:
            original_filename = _filename_from_content_disposition(
                self._curl_head_content_disposition(pdf_url)
            )

        return {
            "abstract": abstract,
            "category": category_label,
            "doi": doi,
            "keywords": keywords,
            "node_id": node_id,
            "original_filename": original_filename,
            "pdf_url": pdf_url,
            "published_date": published_date,
            "published_raw": published_raw,
            "title": title,
            "url": url,
        }

    def _detail_body(self, textual, page_url: str) -> tuple[str, Optional[str], Optional[str]]:
        if textual is None:
            return "", None, None

        clone = _make_soup(str(textual), context="detail text fragment")
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

    def _detail_tags(self, tags_section) -> Optional[str]:
        if tags_section is None:
            return None
        tags = []
        for link in tags_section.select("a"):
            text = _clean_text(link.get_text(" ", strip=True))
            if text and text not in tags:
                tags.append(text)
        return ", ".join(tags) if tags else None

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
        published_date = detail.get("published_date")
        listed_date = published_date
        category = detail.get("category") or item.get("category")
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
        keywords = detail.get("keywords")

        metadata = {
            "posted_date": detail.get("published_raw"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "post_number": post_number,
            "published_raw": detail.get("published_raw"),
            "category": category,
            "keywords": keywords,
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
            "keywords": keywords,
            "category": category,
            "doi": detail.get("doi"),
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def _slug_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        try:
            return unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or None
        except Exception:
            return None
