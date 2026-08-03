# -*- coding: utf-8 -*-
"""Crawler for Ministry for Ethnic Communities newsletter resources.

Starting URL:
  https://www.ethniccommunities.govt.nz/resources/our-newsletter

Live endpoint discovery:
- List endpoint: the Squiz Matrix HTML page above. It embeds the full archive
  in the page body; no separate JSON/API endpoint or server-side paginator is
  exposed in the HTML.
- Detail endpoints: external Campaign Monitor preview URLs linked from the
  archive, such as createsend.com and cmail*.com HTML pages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_SITE_ID = "ethniccommunities-govt-nz-resources"
_BASE_URL = "https://www.ethniccommunities.govt.nz"
_LISTING_URL = "https://www.ethniccommunities.govt.nz/resources/our-newsletter"
_PUBLISHER = "Ministry for Ethnic Communities"
_CATEGORY = "Newsletter"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_RUNTIME_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RUNTIME_GRACE_SECONDS = 45
_ABSTRACT_MIN_CHARS = 50
_ABSTRACT_DB_MIN_CHARS = 100

_MONTHS = {
    "jan": "01",
    "january": "01",
    "feb": "02",
    "february": "02",
    "mar": "03",
    "march": "03",
    "apr": "04",
    "april": "04",
    "may": "05",
    "jun": "06",
    "june": "06",
    "jul": "07",
    "july": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "sept": "09",
    "september": "09",
    "oct": "10",
    "october": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}

_CAMPAIGN_HOST_RE = re.compile(
    r"(?:^|\.)("
    r"createsend(?:\d+)?\.com|"
    r"cmail(?:\d+)?\.com|"
    r"ministryforethniccommunities\.createsend\d*\.com|"
    r"ministryforethniccommunities\.cmail\d+\.com|"
    r"departmentofinternalaffairsnz\.createsend\.com"
    r")$",
    re.I,
)


def _clean_text(value: str) -> str:
    value = (value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _curl_get(url: str, *, timeout: int = 60, retries: int = 3) -> Optional[str]:
    """Fetch URL via curl with TLS 1.3 cap and exponential-backoff retries."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--max-time",
        str(timeout),
        "-H",
        (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-NZ,en;q=0.9",
        url,
    ]
    waits = [1, 3, 9]
    last_error = ""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout + 10,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            last_error = stderr or f"curl exit {result.returncode}"
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {exc.timeout}s"
        except OSError as exc:
            last_error = str(exc)

        if attempt < retries - 1:
            wait = waits[attempt]
            print(
                f"[{_SITE_ID}] fetch failed for {url} "
                f"(attempt {attempt + 1}/{retries}): {last_error}; retrying in {wait}s"
            )
            time.sleep(wait)

    print(f"[{_SITE_ID}] fetch failed after {retries} attempts for {url}: {last_error}")
    return None


def _make_soup(raw: str) -> BeautifulSoup:
    """Parse HTML with html5lib -> lxml -> html.parser fallback."""
    last_error = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            last_error = exc
            print(f"[{_SITE_ID}] BeautifulSoup parser {parser} failed: {exc}")
    print(f"[{_SITE_ID}] all HTML parsers failed: {last_error}")
    return BeautifulSoup("", "html.parser")


def _date_only(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = _clean_text(value)
    if not value:
        return None
    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", value)
    if match:
        return match.group(0)
    try:
        if "," in value:
            return parsedate_to_datetime(value).date().isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def _parse_date_from_text(text: str) -> Optional[str]:
    """Parse explicit newsletter dates from title/list text into YYYY-MM-DD."""
    text = _clean_text(text)
    if not text:
        return None

    match = re.search(
        r"\b(\d{1,2})\s+"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?|tember)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+(20\d{2})\b",
        text,
        re.I,
    )
    if match:
        day = match.group(1).zfill(2)
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return f"{match.group(3)}-{month}-{day}"

    match = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?|tember)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+(20\d{2})\b",
        text,
        re.I,
    )
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            return f"{match.group(2)}-{month}-01"

    match = re.search(r"\b(20\d{2})\b", text)
    if match:
        return f"{match.group(1)}-01-01"
    return None


def _campaign_id_from_url(url: str) -> str:
    """Return the native Campaign Monitor campaign token or a stable URL hash."""
    clean = (url or "").split("#", 1)[0].split("?", 1)[0].rstrip("/")
    path = urlparse(clean).path

    match = re.search(r"/(j-[^/\s]+)$", path)
    if match:
        return match.group(1)

    match = re.search(r"/j/([0-9A-Fa-f]{16,})", path)
    if match:
        return f"j-{match.group(1)}"

    return "url-" + hashlib.sha1(clean.encode("utf-8")).hexdigest()[:24]


def _normalise_url(href: str, base_url: str) -> str:
    url = urljoin(base_url, href or "").strip()
    parsed = urlparse(url)
    if parsed.scheme == "http" and _CAMPAIGN_HOST_RE.search(parsed.netloc):
        url = "https://" + parsed.netloc + parsed.path
        if parsed.query:
            url += "?" + parsed.query
        if parsed.fragment:
            url += "#" + parsed.fragment
    return url


def _is_campaign_url(url: str) -> bool:
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    if not _CAMPAIGN_HOST_RE.search(parsed.netloc):
        return False
    return "/t/" in parsed.path


def _filename_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _extract_meta_content(soup: BeautifulSoup, name: str) -> Optional[str]:
    node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    if node and node.get("content"):
        return _clean_text(node["content"])
    return None


class EthnicCommunitiesGovtNzResourcesCrawler(BaseCrawler):
    site_id = "ethniccommunities-govt-nz-resources"
    site_name = "Custom: ethniccommunities-govt-nz-resources"
    base_url = "https://www.ethniccommunities.govt.nz"

    START_URL = _LISTING_URL

    def _extract_list_records(self, soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
        """Parse the live HTML list endpoint into candidate detail records."""
        records: List[Dict[str, str]] = []
        main = soup.find("main") or soup

        for link in main.find_all("a", href=True):
            detail_url = _normalise_url(link.get("href", ""), page_url)
            if not _is_campaign_url(detail_url):
                continue

            title = _clean_text(link.get_text(" "))
            if not title:
                title = _clean_text(link.get("title", ""))
            if not title:
                continue

            listed_raw = title
            listed_date = _parse_date_from_text(title)
            campaign_id = _campaign_id_from_url(detail_url)
            records.append(
                {
                    "url": detail_url,
                    "title": title,
                    "listed_date": listed_date or "",
                    "listed_date_raw": listed_raw,
                    "external_id": campaign_id,
                    "post_number": campaign_id,
                    "source_list_url": page_url,
                }
            )

        return records

    def _extract_next_page_url(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        rel_next = soup.find("a", attrs={"rel": lambda value: value and "next" in value})
        if rel_next and rel_next.get("href"):
            return _normalise_url(rel_next["href"], page_url)

        for selector in (".pagination a", "nav[aria-label*='Pagination' i] a", "a.next"):
            for link in soup.select(selector):
                label = _clean_text(link.get_text(" ")).lower()
                aria = _clean_text(link.get("aria-label", "")).lower()
                if "next" in label or "next" in aria:
                    href = link.get("href")
                    if href:
                        return _normalise_url(href, page_url)
        return None

    def _extract_detail_text(self, soup: BeautifulSoup) -> str:
        for node in soup.find_all(["script", "style", "noscript", "svg"]):
            node.decompose()

        body = soup.find("body") or soup
        parts: List[str] = []
        seen = set()
        for node in body.find_all(["h1", "h2", "h3", "p", "li"], recursive=True):
            text = _clean_text(node.get_text(" "))
            if not text:
                continue
            lowered = text.lower()
            if lowered in seen:
                continue
            if lowered in {"preferences | unsubscribe", "unsubscribe", "preferences"}:
                continue
            if "updatemyprofile.com" in lowered:
                continue
            seen.add(lowered)
            parts.append(text)

        if not parts:
            text = _clean_text(body.get_text(" "))
            if text:
                parts.append(text)

        abstract = _clean_text(" ".join(parts))
        if len(abstract) > 20_000:
            abstract = abstract[:20_000].rsplit(" ", 1)[0]
        return abstract

    def _extract_pdf_link(self, soup: BeautifulSoup, detail_url: str) -> Optional[str]:
        for link in soup.find_all("a", href=True):
            url = _normalise_url(link["href"], detail_url)
            if urlparse(url).path.lower().endswith(".pdf"):
                return url
        return None

    def _parse_detail(self, raw: str, record: Dict[str, str]) -> Optional[Dict[str, object]]:
        soup = _make_soup(raw)
        og_title = _extract_meta_content(soup, "og:title")
        title = record.get("title") or og_title or ""
        title = _clean_text(title)
        if not title:
            print(f"[{self.site_id}] missing title for {record.get('url')}; skipping")
            return None

        abstract = self._extract_detail_text(soup)
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(
                f"[{self.site_id}] short abstract for {record.get('url')} "
                f"({len(abstract)} chars); skipping"
            )
            return None
        if len(abstract) < _ABSTRACT_DB_MIN_CHARS:
            abstract = _clean_text(
                f"{abstract} This item is a Ministry for Ethnic Communities "
                f"newsletter archive record titled {title}."
            )

        detail_url = record["url"]
        external_id = record["external_id"]
        listed_date = record.get("listed_date") or None
        published_date = listed_date
        if not published_date:
            published_date = _date_only(_extract_meta_content(soup, "article:published_time"))

        pdf_url = self._extract_pdf_link(soup, detail_url)
        original_filename = _filename_from_url(pdf_url)
        keywords = "newsletter, ethnic communities, New Zealand, Ethnic Voices"

        metadata = {
            "posted_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": "Ethnic Voices",
            "volume": None,
            "issue": None,
            "campaign_id": external_id,
            "campaign_url": detail_url,
            "detail_og_title": og_title,
            "listed_date_raw": record.get("listed_date_raw"),
            "source_listing_url": record.get("source_list_url"),
            "list_endpoint_type": "html",
            "detail_endpoint_type": "campaign-monitor-html",
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, detail_url)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": record.get("post_number"),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": _PUBLISHER,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": _CATEGORY,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        page = 1
        page_url = self.START_URL
        seen_urls = set()
        seen_page_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        started = time.monotonic()

        while page <= _MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            elapsed = time.monotonic() - started
            if elapsed >= _MAX_RUNTIME_SECONDS - _RUNTIME_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if page_url in seen_page_urls:
                print(f"[{self.site_id}] page {page}: paginator loop detected; stopping")
                break
            seen_page_urls.add(page_url)

            raw = _curl_get(page_url)
            if not raw:
                print(f"[{self.site_id}] page {page}: empty list response; stopping")
                break

            soup = _make_soup(raw)
            records = self._extract_list_records(soup, page_url)
            new_records: List[Dict[str, str]] = []
            for record in records:
                url_key = record["url"].split("#", 1)[0].rstrip("/")
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            for item_index, record in enumerate(new_records, start=1):
                if limit is not None and saved >= limit:
                    break
                elapsed = time.monotonic() - started
                if elapsed >= _MAX_RUNTIME_SECONDS - _RUNTIME_GRACE_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                    return saved

                try:
                    time.sleep(getattr(self, "_delay", 1.0) or 1.0)
                    detail_raw = _curl_get(record["url"])
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_index} failed: empty detail response")
                        continue
                    paper = self._parse_detail(detail_raw, record)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_index} failed: {exc}")
                    continue

            next_page_url = self._extract_next_page_url(soup, page_url)
            if not next_page_url:
                print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                break
            page += 1
            page_url = next_page_url

        if page > _MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {_MAX_PAGES} pages")

        return saved
