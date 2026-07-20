# -*- coding: utf-8 -*-
"""Crawler for Ifremer French press releases.

Discovered endpoints:
- List HTML: https://www.ifremer.fr/fr/espace-presse?page=N
- Detail HTML: /fr/presse/{slug}
- Drupal Views AJAX also exists at /fr/views/ajax, but the rendered list
  pages expose the same records and have simpler pagination semantics.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from datetime import datetime
from email.message import Message
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_FR_MONTHS = {
    "janvier": "01",
    "fevrier": "02",
    "mars": "03",
    "avril": "04",
    "mai": "05",
    "juin": "06",
    "juillet": "07",
    "aout": "08",
    "septembre": "09",
    "octobre": "10",
    "novembre": "11",
    "decembre": "12",
}


def _normalize_space(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _ascii_fold(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def _parse_date(value: str | None) -> str | None:
    """Parse ISO datetimes or French display dates to YYYY-MM-DD."""
    if not value:
        return None

    raw = _normalize_space(value)
    if not raw:
        return None

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    folded = _ascii_fold(raw)
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", folded)
    if m:
        day, month_name, year = m.groups()
        month = _FR_MONTHS.get(month_name)
        if month:
            return f"{year}-{month}-{int(day):02d}"

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _make_soup(raw: str):
    """Build BeautifulSoup with the required parser fallback chain."""
    from bs4 import BeautifulSoup

    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            last_exc = exc
            continue
    print(f"[ifremer-fr-fr] BeautifulSoup failed with all parsers: {last_exc}")
    return BeautifulSoup("", "html.parser")


def _meta_content(soup, *selectors: str) -> str | None:
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            value = _normalize_space(tag.get("content"))
            if value:
                return value
    return None


def _absolute_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href:
        return None
    return urljoin(base_url, href)


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    filename = unquote(path.split("/")[-1])
    if "." not in filename or len(filename) > 240:
        return None
    return filename


def _filename_from_content_disposition(header_value: str | None) -> str | None:
    if not header_value:
        return None
    msg = Message()
    msg["content-disposition"] = header_value
    filename = msg.get_filename()
    return _normalize_space(filename) if filename else None


def _extract_doi(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text, re.I)
    if not m:
        return None
    return m.group(0).rstrip(".,;:)")


def _extract_journal(text: str) -> str | None:
    if not text:
        return None
    patterns = (
        r"\bpubli\S*\s+dans\s+(?:la\s+revue\s+)?([A-Z][A-Za-z0-9 &'._-]{2,80}?)(?:\s+le\b|[.,;:])",
        r"\bparue?\s+dans\s+(?:la\s+revue\s+)?([A-Z][A-Za-z0-9 &'._-]{2,80}?)(?:\s+le\b|[.,;:])",
        r"\bpublication\s+dans\s+(?:la\s+revue\s+)?([A-Z][A-Za-z0-9 &'._-]{2,80}?)(?:\s+le\b|[.,;:])",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            journal = _normalize_space(m.group(1)).strip(" .,:;")
            if 2 < len(journal) <= 100:
                return journal
    return None


class IfremerFrFrCrawler(BaseCrawler):
    site_id = "ifremer-fr-fr"
    site_name = "Custom: ifremer-fr-fr"
    base_url = "https://www.ifremer.fr"

    START_URL = "https://www.ifremer.fr/fr/espace-presse"
    AJAX_URL = "https://www.ifremer.fr/fr/views/ajax"
    MAX_PAGES = 200
    MAX_SECONDS = 25 * 60
    CURL_TIMEOUT = 60
    MIN_ABSTRACT_CHARS = 50
    BACKOFFS = (1, 3, 9)

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        started = time.monotonic()

        try:
            for p in range(self.MAX_PAGES):
                if limit is not None and saved >= limit:
                    break

                if self._near_time_limit(started):
                    print(f"[{self.site_id}] approaching 25 minute budget; stopping cleanly")
                    break

                if p % 10 == 0:
                    print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

                list_url = self.START_URL if p == 0 else f"{self.START_URL}?page={p}"
                raw = self._curl_get(list_url, context=f"list page {p}")
                if not raw:
                    print(f"[{self.site_id}] page {p}: empty list response; stopping")
                    break

                soup = _make_soup(raw)
                items, has_next = self._parse_list_page(soup, p)
                if not items:
                    print(f"[{self.site_id}] page {p}: no list records; stopping")
                    break

                new_urls_on_page = 0
                for idx, item in enumerate(items, start=1):
                    if limit is not None and saved >= limit:
                        break
                    if self._near_time_limit(started):
                        print(f"[{self.site_id}] approaching 25 minute budget; stopping cleanly")
                        return saved

                    item_url = item.get("url")
                    if not item_url:
                        continue
                    if item_url in seen_urls:
                        continue

                    seen_urls.add(item_url)
                    new_urls_on_page += 1

                    try:
                        delay = getattr(self, "_delay", 1.0)
                        time.sleep(float(1.0 if delay is None else delay))
                        if self._process_item(item, p, idx):
                            saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_url} failed: {exc}")
                        continue

                if new_urls_on_page == 0:
                    print(f"[{self.site_id}] page {p}: no new records; stopping")
                    break

                if not has_next:
                    print(f"[{self.site_id}] page {p}: next page absent; stopping")
                    break
            else:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached")
        except KeyboardInterrupt:
            raise

        return saved

    def _near_time_limit(self, started: float) -> bool:
        return (time.monotonic() - started) >= (self.MAX_SECONDS - 30)

    def _curl_get(self, url: str, context: str = "") -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            url,
        ]
        for attempt, wait in enumerate(self.BACKOFFS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl {context} attempt {attempt}/3 failed "
                    f"(code {result.returncode}): {err[:200]}"
                )
            except subprocess.TimeoutExpired as exc:
                print(f"[{self.site_id}] curl {context} attempt {attempt}/3 timed out: {exc}")
            except Exception as exc:
                print(f"[{self.site_id}] curl {context} attempt {attempt}/3 error: {exc}")

            if attempt < 3:
                time.sleep(wait)
        return None

    def _curl_head_filename(self, url: str) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skLI",
            "--max-time",
            "20",
            "-A",
            self.USER_AGENT,
            url,
        ]
        for attempt, wait in enumerate(self.BACKOFFS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                headers = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and headers:
                    for line in headers.splitlines():
                        if line.lower().startswith("content-disposition:"):
                            return _filename_from_content_disposition(line.split(":", 1)[1].strip())
                    return None
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] head filename attempt {attempt}/3 failed "
                    f"(code {result.returncode}): {err[:200]}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] head filename attempt {attempt}/3 error: {exc}")
            if attempt < 3:
                time.sleep(wait)
        return None

    def _parse_list_page(self, soup, page_index: int) -> tuple[list[dict], bool]:
        items = []

        star = soup.select_one(".field--name-field-ref-press-star .node--type-press")
        if star:
            parsed = self._parse_list_node(star, page_index, is_star=True)
            if parsed:
                items.append(parsed)

        container = soup.select_one("#press-releases")
        if container:
            for row in container.select(".views-row"):
                node = row.select_one(".node--type-press") or row
                parsed = self._parse_list_node(node, page_index, is_star=False)
                if parsed:
                    items.append(parsed)

        deduped = []
        seen = set()
        for item in items:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(item)

        has_next = bool(soup.select_one("ul.pager-show-more a[href], .pager-show-more a[href]"))
        return deduped, has_next

    def _parse_list_node(self, node, page_index: int, is_star: bool) -> dict | None:
        link = node.select_one("a.node-title[href]")
        if not link:
            return None

        url = _absolute_url(self.base_url, link.get("href"))
        if not url:
            return None

        title = _normalize_space(link.get_text(" ", strip=True))
        time_el = node.select_one("time[datetime]")
        listed_date_raw = ""
        listed_date = None
        if time_el:
            listed_date_raw = _normalize_space(time_el.get_text(" ", strip=True))
            listed_date = _parse_date(time_el.get("datetime") or listed_date_raw)

        teaser_el = node.select_one(".field--name-field-teaser")
        teaser = _normalize_space(teaser_el.get_text(" ", strip=True)) if teaser_el else ""

        pdf_link = node.select_one('a.download[href*=".pdf"], a[href*=".pdf"]')
        pdf_url = _absolute_url(self.base_url, pdf_link.get("href")) if pdf_link else None

        return {
            "title": title,
            "url": url,
            "slug": urlparse(url).path.rstrip("/").split("/")[-1],
            "listed_date": listed_date,
            "listed_date_raw": listed_date_raw,
            "teaser": teaser,
            "pdf_url": pdf_url,
            "source_page": page_index,
            "is_star": is_star,
        }

    def _process_item(self, item: dict, page_index: int, item_index: int) -> bool:
        url = item["url"]
        raw = self._curl_get(url, context=f"detail p{page_index} item {item_index}")
        if not raw:
            print(f"[{self.site_id}] item {url} failed: detail fetch returned empty")
            return False

        soup = _make_soup(raw)
        detail = self._parse_detail_page(soup, raw, item)

        title = detail.get("title") or item.get("title")
        abstract = detail.get("abstract") or item.get("teaser") or ""
        abstract = _normalize_space(abstract)
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return False

        published_date = detail.get("published_date") or item.get("listed_date")
        listed_date = item.get("listed_date") or published_date
        pdf_url = detail.get("pdf_url") or item.get("pdf_url")
        original_filename = (
            _filename_from_url(pdf_url)
            or (self._curl_head_filename(pdf_url) if pdf_url else None)
        )
        node_id = detail.get("node_id")
        post_number = node_id or item.get("slug") or None
        external_id = node_id or item.get("slug") or url
        doi = _extract_doi(detail.get("full_text") or abstract)
        journal = _extract_journal(detail.get("full_text") or abstract)

        metadata = {
            "posted_date": item.get("listed_date_raw"),
            "listed_date": listed_date,
            "published_date_raw": detail.get("published_date_raw"),
            "originalFilename": original_filename,
            "journal_raw": journal,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "post_number": post_number,
            "slug": item.get("slug"),
            "source_page": item.get("source_page"),
            "is_star": item.get("is_star"),
            "list_endpoint": self.START_URL if page_index == 0 else f"{self.START_URL}?page={page_index}",
            "detail_endpoint": url,
            "drupal_current_path": detail.get("drupal_current_path"),
            "list_item": {
                "title": item.get("title"),
                "url": item.get("url"),
                "listed_date_raw": item.get("listed_date_raw"),
                "listed_date": item.get("listed_date"),
                "teaser": item.get("teaser"),
                "pdf_url": item.get("pdf_url"),
            },
            "detail": {
                "title": detail.get("title"),
                "canonical_url": detail.get("canonical_url"),
                "description": detail.get("description"),
                "pdf_url": detail.get("pdf_url"),
            },
        }

        paper = {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": "Ifremer",
            "department": None,
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": "Communique de presse",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }
        self._save_paper(paper)
        return True

    def _parse_detail_page(self, soup, raw: str, list_item: dict) -> dict:
        article = soup.select_one("article.node--type-press") or soup

        title_el = article.select_one("h1")
        title = _normalize_space(title_el.get_text(" ", strip=True)) if title_el else None
        if not title:
            title = _meta_content(soup, 'meta[property="og:title"]', 'meta[name="twitter:title"]')

        canonical = None
        canonical_el = soup.select_one('link[rel="canonical"][href]')
        if canonical_el:
            canonical = _absolute_url(self.base_url, canonical_el.get("href"))

        date_el = article.select_one(".date time[datetime]") or article.select_one("time[datetime]")
        published_date_raw = ""
        published_date = None
        if date_el:
            published_date_raw = _normalize_space(date_el.get_text(" ", strip=True))
            published_date = _parse_date(date_el.get("datetime") or published_date_raw)

        pdf_link = article.select_one('a[href*=".pdf"]')
        pdf_url = _absolute_url(self.base_url, pdf_link.get("href")) if pdf_link else None

        description = _meta_content(
            soup,
            'meta[name="description"]',
            'meta[property="og:description"]',
            'meta[name="twitter:description"]',
        )

        parts = []
        teaser_el = article.select_one(".chapo .field--name-field-teaser") or article.select_one(".field--name-field-teaser")
        if teaser_el:
            parts.append(_normalize_space(teaser_el.get_text(" ", strip=True)))

        for text_el in article.select(".paragraphs-container .field--name-field-text"):
            text = _normalize_space(text_el.get_text(" ", strip=True))
            if text:
                parts.append(text)

        if description:
            parts.insert(0, description)

        abstract = self._join_unique(parts)
        full_text = _normalize_space(" ".join(part for part in parts if part))

        settings = self._drupal_settings(soup)
        current_path = None
        node_id = None
        if settings:
            current_path = (settings.get("path") or {}).get("currentPath")
            if current_path:
                m = re.search(r"node/(\d+)", current_path)
                if m:
                    node_id = m.group(1)
        if not node_id:
            m = re.search(r'"currentPath"\s*:\s*"node\\?/(\d+)"', raw)
            if m:
                node_id = m.group(1)
                current_path = f"node/{node_id}"

        return {
            "title": title,
            "canonical_url": canonical,
            "published_date": published_date,
            "published_date_raw": published_date_raw,
            "pdf_url": pdf_url,
            "description": description,
            "abstract": abstract or list_item.get("teaser"),
            "full_text": full_text,
            "node_id": node_id,
            "drupal_current_path": current_path,
        }

    def _drupal_settings(self, soup) -> dict | None:
        script = soup.select_one('script[type="application/json"][data-drupal-selector="drupal-settings-json"]')
        if not script:
            return None
        raw = script.string or script.get_text()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _join_unique(parts: list[str]) -> str:
        result = []
        seen = set()
        for part in parts:
            text = _normalize_space(part)
            if not text:
                continue
            key = _ascii_fold(text)
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return _normalize_space(" ".join(result))
