# -*- coding: utf-8 -*-
"""Crawler for the Australian Federal Police News Centre.

Starting URL: https://www.afp.gov.au/news-centre

The listing is a Drupal/District CMS view rendered as normal HTML. Pagination
uses the query endpoint /news-centre?page=N, where the first page is the bare
starting URL and later pages expose a "Show more" rel=next link.
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


SITE_ID = "afp-gov-au-news-centre"


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
    """Return YYYY-MM-DD from AFP datetime attributes or display dates."""
    if not raw:
        return None
    text = _clean_text(raw)
    if not text:
        return None

    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if match:
        return match.group(1)

    # JSON-LD sometimes uses +1000 instead of +10:00. Date-only extraction above
    # handles that, but keep a parser for other ISO-like variants.
    try:
        normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
        return datetime.fromisoformat(normalized).date().isoformat()
    except Exception:
        pass

    for fmt in ("%d %B %Y, %I:%M%p", "%d %B %Y", "%d %b %Y, %I:%M%p", "%d %b %Y"):
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


class AFPGovAuNewsCentreCrawler(BaseCrawler):
    site_id = "afp-gov-au-news-centre"
    site_name = "Custom: afp-gov-au-news-centre"
    base_url = "https://www.afp.gov.au"

    _START_URL = "https://www.afp.gov.au/news-centre"
    _PAGE_SIZE = 8
    _SAFETY_CAP = 200
    _WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 50
    _BACKOFF_SECONDS = (1, 3, 9)

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        for page in range(self._SAFETY_CAP):
            if time.time() - start_time > self._WALL_BUDGET_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            try:
                raw = self._curl_get(list_url, label=f"list page {page}")
            except KeyboardInterrupt:
                raise

            if not raw:
                print(f"[{self.site_id}] page {page}: fetch failed or empty; stopping")
                break

            items, next_url = self._parse_list_page(raw, list_url)
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
                    detail_raw = self._curl_get(item_url, label=f"item {item.get('node_id') or index}")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item.get('node_id') or index} failed: empty detail response")
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
                    print(f"[{self.site_id}] item {item.get('node_id') or index} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no unseen records; stopping")
                break

            if not next_url:
                print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                break

        else:
            print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP} pages reached")

        print(f"[{self.site_id}] done: saved {saved}")
        return saved

    def _list_url(self, page: int) -> str:
        if page <= 0:
            return self._START_URL
        return f"{self._START_URL}?page={page}"

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
            "Accept-Language: en-AU,en;q=0.9",
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

    def _parse_list_page(self, raw: str, page_url: str) -> tuple[list[dict], Optional[str]]:
        soup = _make_soup(raw, context=page_url)
        if soup is None:
            return [], None

        view = soup.select_one(".view-solr-news-centre")
        if view is None:
            print(f"[{self.site_id}] list view not found on {page_url}")
            return [], None

        items = []
        rows = view.select(".view-content .views-row")
        for row in rows:
            try:
                node = row.select_one("[data-history-node-id]")
                if node is None:
                    continue
                node_id = _clean_text(node.get("data-history-node-id"))
                node_classes = node.get("class", [])
                node_type = self._node_type_from_classes(node_classes)

                link = (
                    row.select_one(".card--link a[href]")
                    or row.select_one(".card--readmore a[href]")
                    or row.select_one("a[href*='/news-centre/']")
                )
                if link is None:
                    continue
                item_url = urljoin(self.base_url, link.get("href", ""))
                if not item_url.startswith(self.base_url):
                    continue

                title = self._list_title(row, link)
                if not title:
                    continue

                date_el = row.select_one(".card--date")
                time_tag = row.select_one(".card--date time") or row.find("time")
                listed_datetime = _clean_text(time_tag.get("datetime")) if time_tag else ""
                if time_tag:
                    listed_date_raw = time_tag.get_text(" ", strip=True)
                elif date_el:
                    listed_date_raw = date_el.get_text(" ", strip=True)
                else:
                    listed_date_raw = ""
                listed_date = _iso_date(listed_datetime) or _iso_date(listed_date_raw)

                category_el = row.select_one(".field--name-field-article-type")
                category = _clean_text(category_el.get_text(" ", strip=True)) if category_el else ""
                if not category and node_type:
                    category = node_type.replace("-", " ").title()

                image = row.select_one("img")
                image_url = urljoin(self.base_url, image.get("src", "")) if image and image.get("src") else None

                items.append(
                    {
                        "node_id": node_id,
                        "node_type": node_type,
                        "title": title,
                        "url": item_url,
                        "listed_date": listed_date,
                        "listed_date_raw": listed_date_raw,
                        "listed_datetime": listed_datetime,
                        "category": category,
                        "image_url": image_url,
                        "list_endpoint": page_url,
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] list row parse failed: {exc}")
                continue

        next_link = view.select_one("[data-drupal-views-infinite-scroll-pager] a[rel='next']")
        next_url = urljoin(page_url, next_link.get("href")) if next_link and next_link.get("href") else None
        return items, next_url

    def _parse_detail_page(self, raw: str, url: str) -> dict:
        soup = _make_soup(raw, context=url)
        if soup is None:
            return {"abstract": "", "url": url}

        block = soup.select_one("#block-system-main-block") or soup.select_one("main") or soup.body
        node = block.select_one("[data-history-node-id]") if block else None
        if node is None and soup.body:
            node = soup.body.select_one("[data-history-node-id]")

        json_ld = self._parse_json_ld(soup)
        title = self._detail_title(soup, node, json_ld)
        category = self._detail_category(node)
        published_raw = self._detail_published_raw(node, json_ld)
        published_date = _iso_date(published_raw)
        abstract = self._detail_abstract(node, block)
        pdf_url = self._detail_pdf_url(node, block, url)

        original_filename = _filename_from_url(pdf_url)
        if pdf_url and not original_filename:
            original_filename = _filename_from_content_disposition(
                self._curl_head_content_disposition(pdf_url)
            )

        node_id = _clean_text(node.get("data-history-node-id")) if node and node.has_attr("data-history-node-id") else ""
        node_type = self._node_type_from_classes(node.get("class", [])) if node else ""
        keywords = self._detail_keywords(soup)

        return {
            "abstract": abstract,
            "category": category,
            "json_ld": json_ld,
            "keywords": keywords,
            "node_id": node_id,
            "node_type": node_type,
            "original_filename": original_filename,
            "pdf_url": pdf_url,
            "published_date": published_date,
            "published_raw": published_raw,
            "title": title,
            "url": url,
        }

    def _build_paper(self, item: dict, detail: dict) -> dict:
        node_id = detail.get("node_id") or item.get("node_id") or ""
        external_id = node_id or self._slug_from_url(item.get("url") or detail.get("url"))
        post_number = node_id if re.fullmatch(r"\d+", node_id or "") else external_id

        title = detail.get("title") or item.get("title") or "(untitled)"
        listed_date = item.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        category = detail.get("category") or item.get("category") or None
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
        keywords = detail.get("keywords")

        metadata = {
            "posted_date": item.get("listed_date_raw") or item.get("listed_datetime") or listed_date,
            "posted_date_iso": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "node_type": detail.get("node_type") or item.get("node_type"),
            "post_number": post_number,
            "listed_datetime": item.get("listed_datetime"),
            "listed_date": listed_date,
            "published_raw": detail.get("published_raw"),
            "category": category,
            "keywords": keywords,
            "image_url": item.get("image_url"),
            "list_endpoint": item.get("list_endpoint"),
            "detail_endpoint": detail.get("url") or item.get("url"),
            "json_ld": detail.get("json_ld") or {},
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
            "publisher": "Australian Federal Police",
            "department": "Australian Federal Police",
            "journal": None,
            "url": detail.get("url") or item.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def _list_title(self, row, link) -> str:
        title_el = row.select_one(".field--name-node-title") or row.select_one(".card--title")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
        if not title:
            title = _clean_text(link.get("aria-label") or link.get_text(" ", strip=True))
        return title

    def _node_type_from_classes(self, classes: list[str]) -> str:
        for cls in classes or []:
            match = re.match(r"node--type-(.+)", cls)
            if match:
                return match.group(1)
        return ""

    def _parse_json_ld(self, soup) -> dict:
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.get_text("", strip=True)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if isinstance(parsed, dict):
                graph = parsed.get("@graph")
                if isinstance(graph, list):
                    for entry in graph:
                        if isinstance(entry, dict) and entry.get("@type") in ("NewsArticle", "Article", "WebPage"):
                            return entry
                return parsed
            if isinstance(parsed, list):
                for entry in parsed:
                    if isinstance(entry, dict):
                        return entry
        return {}

    def _detail_title(self, soup, node, json_ld: dict) -> Optional[str]:
        selectors = [
            ".article-page__title",
            ".file--info-name",
            ".publication-page h1",
            "h1",
        ]
        for selector in selectors:
            root = node if node is not None else soup
            el = root.select_one(selector)
            if el:
                title = _clean_text(el.get_text(" ", strip=True))
                if title and title != "News Centre":
                    return title

        title = _clean_text(json_ld.get("headline") or json_ld.get("name"))
        if title:
            return title

        meta = soup.find("meta", property="og:title")
        if meta and meta.get("content"):
            title = _clean_text(meta.get("content")).split(" | ", 1)[0]
            if title and title != "News Centre":
                return title
        return None

    def _detail_category(self, node) -> Optional[str]:
        if node is not None:
            category_el = node.select_one(".article-page__category")
            if category_el:
                category = _clean_text(category_el.get_text(" ", strip=True))
                if category:
                    return category
            node_type = self._node_type_from_classes(node.get("class", []))
            if node_type:
                return node_type.replace("-", " ").title()
        return None

    def _detail_published_raw(self, node, json_ld: dict) -> Optional[str]:
        if node is not None:
            time_tag = node.select_one(".article-page__date time") or node.find("time")
            if time_tag:
                return _clean_text(time_tag.get("datetime")) or _clean_text(time_tag.get_text(" ", strip=True))
        return _clean_text(json_ld.get("datePublished") or json_ld.get("dateModified")) or None

    def _detail_abstract(self, node, block) -> str:
        candidates = []
        if node is not None:
            for selector in (
                ".article-page__body",
                ".publication-page .field--name-field-content",
                ".field--name-field-content",
            ):
                el = node.select_one(selector)
                if el:
                    candidates.append(el)

            body_parts = node.select(".field--name-field-body")
            if body_parts:
                candidates.extend(body_parts)

        if not candidates and block is not None:
            candidates.append(block)

        parts = []
        for candidate in candidates:
            clone = _make_soup(str(candidate), context="detail text fragment")
            if clone is None:
                continue
            for noise in clone.find_all(["script", "style", "nav", "form", "footer", "header", "aside"]):
                noise.decompose()
            for noise in clone.select(
                ".page-feedback-form, .newsletter-subscriptions-form, "
                ".webform-ajax-form-wrapper, .views-element-container, "
                ".article-page__meta, .article-page__title"
            ):
                noise.decompose()
            text = _clean_text(clone.get_text(" ", strip=True))
            if text and text not in parts:
                parts.append(text)

        # Avoid duplicated publication tab text from nested selectors.
        if parts:
            best = max(parts, key=len)
            return best
        return ""

    def _detail_pdf_url(self, node, block, page_url: str) -> Optional[str]:
        roots = [root for root in (node, block) if root is not None]
        for root in roots:
            for selector in (".file--download a[href]", "a[href]"):
                for link in root.select(selector):
                    href = link.get("href") or ""
                    if ".pdf" not in href.lower():
                        continue
                    return urljoin(page_url, href)
        return None

    def _detail_keywords(self, soup) -> Optional[str]:
        keywords = []
        for meta_name in ("keywords", "news_keywords"):
            meta = soup.find("meta", attrs={"name": meta_name})
            if meta and meta.get("content"):
                keywords.extend([p.strip() for p in meta["content"].split(",")])
        for selector in (".field--name-field-tags a", ".field--name-field-topic a"):
            for tag in soup.select(selector):
                text = _clean_text(tag.get_text(" ", strip=True))
                if text:
                    keywords.append(text)
        seen = []
        for keyword in keywords:
            if keyword and keyword not in seen:
                seen.append(keyword)
        return ", ".join(seen) if seen else None

    def _slug_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        try:
            return unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or None
        except Exception:
            return None
