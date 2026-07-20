# -*- coding: utf-8 -*-
"""Defense.gov news releases crawler.

Starting URL: https://www.defense.gov/News/Releases/

The public list/detail HTML pages are often blocked by Akamai for non-browser
clients, but the same DNN ArticleCS module exposes a reachable RSS endpoint:
https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945
"""

from __future__ import annotations

import email.utils
import json
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


_SITE_ID = "defense-gov-news"
_BASE_URL = "https://www.defense.gov"
_FETCH_HOST = "https://www.defense.gov"
_RSS_ENDPOINT = f"{_FETCH_HOST}/DesktopModules/ArticleCS/RSS.ashx"
_START_URL = f"{_BASE_URL}/News/Releases/"
_CONTENT_TYPE = "9"
_SITE = "945"
_PAGE_SIZE = 100
_RSS_MAX = 500
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_MIN_ABSTRACT_CHARS = 50


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(value).replace("\xa0", " ")
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _make_soup(raw: str):
    """Parse malformed HTML with a defensive BeautifulSoup fallback chain."""
    from bs4 import BeautifulSoup

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = _clean_text(raw)
    try:
        return email.utils.parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            from datetime import datetime

            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"\b(20\d{2}|19\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", raw)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return None


def _canonical_url(url: str | None) -> str | None:
    """Save Defense.gov URLs even when the feed redirects to war.gov."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        url = urljoin(_BASE_URL, url)
        parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in {"www.war.gov", "war.gov", "www.defense.gov", "defense.gov"}:
        parsed = parsed._replace(scheme="https", netloc="www.defense.gov")
    return urlunparse(parsed)


def _article_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/Article/(\d+)(?:/|$)", url)
    return match.group(1) if match else None


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    tail = path.rsplit("/", 1)[-1]
    return tail or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if "." in tail and len(tail) <= 200:
        return unescape(tail)
    return None


def _curl_text(url: str, *, retries: int = 3, timeout: int = 35) -> tuple[str | None, int | None]:
    """Fetch text with curl, retrying command/network failures with backoff."""
    backoffs = (1, 3, 9)
    marker = "\n__DEFENSE_GOV_STATUS__:"
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-sk",
        "-L",
        "--connect-timeout",
        "15",
        "--max-time",
        str(timeout),
        "-A",
        BaseCrawler.USER_AGENT,
        "-w",
        marker + "%{http_code}",
        url,
    ]

    for attempt in range(retries):
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout + 10,
                check=False,
            )
            body = proc.stdout.decode("utf-8", errors="replace")
            status = None
            if marker in body:
                body, status_raw = body.rsplit(marker, 1)
                try:
                    status = int(status_raw.strip()[:3])
                except ValueError:
                    status = None

            if proc.returncode == 0 and status is not None and 200 <= status < 300:
                return body, status

            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            if status is not None and 400 <= status < 500:
                print(f"[{_SITE_ID}] HTTP {status} for {url}; not retrying")
                return None, status

            print(
                f"[{_SITE_ID}] fetch error (attempt {attempt + 1}/{retries}) "
                f"for {url}: curl={proc.returncode} status={status} {stderr[:160]}"
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{_SITE_ID}] fetch error (attempt {attempt + 1}/{retries}) for {url}: {exc}")

        if attempt < retries - 1:
            time.sleep(backoffs[attempt])

    print(f"[{_SITE_ID}] all {retries} fetch attempts failed for {url}")
    return None, None


def _rss_url(max_items: int) -> str:
    return (
        f"{_RSS_ENDPOINT}?ContentType={_CONTENT_TYPE}"
        f"&Site={_SITE}&isdashboardselected=0&max={max_items}"
    )


def _parse_rss_items(raw: str) -> list[dict]:
    """Parse RSS items from the ArticleCS feed."""
    try:
        root = ET.fromstring(raw.encode("utf-8", errors="replace"))
        items = []
        for item in root.findall(".//item"):
            link = _clean_text(item.findtext("link"))
            guid = _clean_text(item.findtext("guid"))
            raw_date = _clean_text(item.findtext("pubDate"))
            creator = _clean_text(item.findtext("{http://purl.org/dc/elements/1.1/}creator"))
            items.append(
                {
                    "title": _clean_text(item.findtext("title")),
                    "description": _clean_text(item.findtext("description")),
                    "link_raw": link,
                    "guid": guid,
                    "pub_date_raw": raw_date,
                    "creator": creator,
                }
            )
        return items
    except Exception:
        pass

    soup = _make_soup(raw)
    if soup is None:
        return []
    items = []
    for item in soup.find_all("item"):
        def _tag_text(name: str) -> str:
            tag = item.find(name)
            return _clean_text(tag.get_text(" ", strip=True) if tag else "")

        items.append(
            {
                "title": _tag_text("title"),
                "description": _tag_text("description"),
                "link_raw": _tag_text("link"),
                "guid": _tag_text("guid"),
                "pub_date_raw": _tag_text("pubdate"),
                "creator": _tag_text("creator"),
            }
        )
    return items


def _extract_detail_fields(raw: str, detail_url: str) -> dict:
    """Best-effort parser for detail pages when Akamai allows them."""
    soup = _make_soup(raw)
    if soup is None:
        return {}

    for dead in soup.find_all(["script", "style", "noscript", "nav", "header", "footer"]):
        try:
            dead.decompose()
        except Exception:
            continue

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = _clean_text(h1.get_text(" ", strip=True))
    if not title:
        og_title = soup.find("meta", property="og:title")
        title = _clean_text(og_title.get("content") if og_title else "")

    raw_date = ""
    for selector in (
        "time",
        ".article-date",
        ".date",
        ".pub-date",
        ".news-date",
        "[itemprop='datePublished']",
    ):
        node = soup.select_one(selector)
        if node:
            raw_date = _clean_text(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
            if raw_date:
                break

    candidates = []
    for selector in ("article", "main", ".article-body", ".body", ".content", ".news-release"):
        node = soup.select_one(selector)
        if node:
            text = _clean_text(node.get_text("\n", strip=True))
            if len(text) > 50:
                candidates.append(text)
    abstract = max(candidates, key=len) if candidates else ""

    pdf_url = None
    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        if ".pdf" in href.lower():
            pdf_url = _canonical_url(urljoin(detail_url, href))
            break

    return {
        "title": title,
        "abstract": abstract,
        "raw_date": raw_date,
        "published_date": _parse_date(raw_date),
        "pdf_url": pdf_url,
        "original_filename": _filename_from_url(pdf_url),
    }


class DefenseGovNewsCrawler(BaseCrawler):
    site_id = "defense-gov-news"
    site_name = "Custom: defense-gov-news"
    base_url = "https://www.defense.gov"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")
                break
            if time.time() - start_time >= _MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break

            fetch_max = min(page * _PAGE_SIZE, _RSS_MAX)
            raw, _status = _curl_text(_rss_url(fetch_max), retries=3)
            if not raw:
                print(f"[{self.site_id}] page {page}: list API fetch failed; stopping")
                break

            records = _parse_rss_items(raw)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            start_idx = (page - 1) * _PAGE_SIZE
            page_records = records[start_idx:start_idx + _PAGE_SIZE]
            if not page_records:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            new_on_page = 0
            for idx, record in enumerate(page_records, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    return saved

                raw_url = record.get("link_raw") or record.get("guid")
                canonical_url = _canonical_url(raw_url)
                if not canonical_url:
                    continue
                if canonical_url in seen_urls:
                    continue
                seen_urls.add(canonical_url)
                new_on_page += 1

                try:
                    title = record.get("title") or ""
                    description = record.get("description") or ""
                    raw_date = record.get("pub_date_raw") or ""
                    published_date = _parse_date(raw_date)
                    detail = {}

                    detail_raw, detail_status = _curl_text(raw_url or canonical_url, retries=3, timeout=25)
                    if detail_raw:
                        detail = _extract_detail_fields(detail_raw, canonical_url)
                    elif detail_status:
                        detail = {"detail_fetch_status": detail_status}

                    if detail.get("title"):
                        title = detail["title"]
                    abstract = detail.get("abstract") or description
                    if len(abstract) < 100 and title and description:
                        abstract = _clean_text(f"{title}\n\n{description}")
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] short abstract ({len(abstract)} chars) "
                            f"for {canonical_url}; skipping"
                        )
                        time.sleep(self._delay)
                        continue

                    if detail.get("published_date"):
                        published_date = detail["published_date"]

                    article_id = _article_id_from_url(canonical_url)
                    external_id = article_id or record.get("guid") or canonical_url
                    post_number = article_id or _slug_from_url(canonical_url)
                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)

                    metadata = {
                        "posted_date": raw_date,
                        "listed_date": published_date,
                        "originalFilename": original_filename,
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "article_id": article_id,
                        "node_id": article_id,
                        "slug": _slug_from_url(canonical_url),
                        "guid": record.get("guid"),
                        "link_raw": raw_url,
                        "canonical_url": canonical_url,
                        "start_url": _START_URL,
                        "rss_endpoint": _RSS_ENDPOINT,
                        "rss_params": {
                            "ContentType": _CONTENT_TYPE,
                            "Site": _SITE,
                            "max": fetch_max,
                        },
                        "content_type": _CONTENT_TYPE,
                        "site": _SITE,
                        "creator_raw": record.get("creator"),
                    }
                    if detail.get("raw_date"):
                        metadata["detail_date_raw"] = detail["raw_date"]
                    if detail.get("detail_fetch_status"):
                        metadata["detail_fetch_status"] = detail["detail_fetch_status"]

                    paper = {
                        "id": f"{self.site_id}:{external_id}",
                        "site_id": self.site_id,
                        "external_id": str(external_id),
                        "post_number": str(post_number) if post_number else None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "posted_date": published_date,
                        "authors": record.get("creator") or "",
                        "publisher": "U.S. Department of Defense",
                        "department": "Press Operations",
                        "journal": "",
                        "url": canonical_url,
                        "pdf_url": pdf_url,
                        "keywords": "",
                        "category": "News Release",
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {title[:70]}")
                    time.sleep(self._delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    label = canonical_url or f"page {page} item {idx}"
                    print(f"[{self.site_id}] item {label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all URLs already seen; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if fetch_max >= _RSS_MAX and (page * _PAGE_SIZE) >= _RSS_MAX:
                next_start = page * _PAGE_SIZE
                if next_start >= len(records):
                    print(f"[{self.site_id}] reached RSS feed depth ({len(records)} records)")
                    break

            page += 1

        print(f"[{self.site_id}] done. saved {saved}")
        return saved
