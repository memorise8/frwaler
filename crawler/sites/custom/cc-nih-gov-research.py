# -*- coding: utf-8 -*-
"""Crawler for NIH Clinical Center Published Research.

Starting URL: https://www.cc.nih.gov/research/2025

The NIH Clinical Center research page is a Drupal HTML page. It does not expose
a usable JSON list API for this route; ``?_format=json`` returns a rendered
array error. Each record is embedded on the year page, with a stable ``h3`` id
anchor and an outbound publication link.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_SITE_ID = "cc-nih-gov-research"
_BASE_URL = "https://www.cc.nih.gov"
_START_URL = "https://www.cc.nih.gov/research/2025"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_CHARS = 50
_RETRY_WAITS = (1, 3, 9)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _make_soup(raw: str):
    """Parse malformed HTML without letting parser failures abort a crawl."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _parse_month_year(raw: str | None) -> str | None:
    """Convert month-level dates such as 'December 2025' to ISO YYYY-MM-DD."""
    if not raw:
        return None
    months = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
    }
    text = raw.lower()
    year = re.search(r"\b(19|20)\d{2}\b", text)
    if not year:
        return None
    for name, number in months.items():
        if name in text:
            return f"{year.group(0)}-{number}-01"
    return f"{year.group(0)}-01-01"


def _extract_pmid(url: str | None) -> str | None:
    if not url or "pubmed.ncbi.nlm.nih.gov" not in url.lower():
        return None
    match = re.search(r"/(\d+)/?$", url.rstrip())
    return match.group(1) if match else None


def _extract_doi(url: str | None) -> str | None:
    """Best-effort DOI extraction from common article URL shapes."""
    if not url:
        return None
    text = url.strip()

    # Common route: /doi/10.xxxx/yyyy or /article/10.xxxx/yyyy
    match = re.search(r"(10\.\d{4,9}/[^?#\s/]+(?:/[^?#\s/]+)*)", text)
    if not match:
        return None

    doi = match.group(1)
    doi = re.sub(r"/(?:full|fulltext|abstract|epdf|pdf)$", "", doi, flags=re.I)
    doi = doi.rstrip(" .),;")

    # Some publisher paths append non-DOI path segments after the DOI.
    if "/article/doi/" in text.lower():
        parts = doi.split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2])
    if "springer.com/article/" in text.lower():
        parts = doi.split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2])
    return doi


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    name = path.rstrip("/").split("/")[-1]
    if "." in name and len(name) <= 200:
        return name
    return None


class CcNihGovResearchCrawler(BaseCrawler):
    site_id = "cc-nih-gov-research"
    site_name = "Custom: cc-nih-gov-research"
    base_url = "https://www.cc.nih.gov"

    def _curl_get(self, url: str, retries: int = 3, max_time: int = 45) -> str | None:
        """Fetch text via curl with TLS 1.3 cap and 1/3/9s backoff."""
        for attempt in range(retries):
            try:
                cmd = [
                    "curl",
                    "--tls-max",
                    "1.3",
                    "-s",
                    "-k",
                    "-L",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    str(max_time),
                    url,
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    text = result.stdout.decode("utf-8", errors="replace")
                    lowered = text[:1000].lower()
                    if (
                        "access denied" not in lowered
                        and "checking your browser" not in lowered
                        and len(text.strip()) > 1000
                    ):
                        return text
                    print(
                        f"[{_SITE_ID}] fetch got short/blocked body "
                        f"attempt {attempt + 1}/{retries} for {url}"
                    )
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{_SITE_ID}] fetch failed attempt {attempt + 1}/{retries} "
                    f"for {url}: curl rc={result.returncode} {err[:160]}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{_SITE_ID}] fetch error attempt {attempt + 1}/{retries} "
                    f"for {url}: {exc}"
                )

            wait = _RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)]
            if attempt < retries - 1:
                time.sleep(wait)

        return None

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return _START_URL
        return f"{_START_URL}?page={page - 1}"

    def _has_next_page(self, raw: str) -> bool:
        soup = _make_soup(raw)
        if not soup:
            return False
        return bool(
            soup.select_one(
                "a[rel='next'], .pager__item--next a, li.next a, a[title*='next' i]"
            )
        )

    def _extract_node_id(self, raw: str) -> str | None:
        soup = _make_soup(raw)
        if not soup:
            return None
        shortlink = soup.find("link", rel="shortlink")
        href = shortlink.get("href", "") if shortlink else ""
        match = re.search(r"/node/(\d+)", href)
        if match:
            return match.group(1)
        settings = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if settings:
            match = re.search(r'"currentPath"\s*:\s*"node\\?/(\d+)"', settings.get_text())
            if match:
                return match.group(1)
        return None

    def _parse_date_line(self, text: str) -> tuple[str | None, str | None]:
        """Return (journal, raw_date) from 'Published in: Journal (Month YYYY)'."""
        if not text or "published in" not in text.lower():
            return None, None

        raw_date = None
        match = re.search(r"\(([^)]*(?:19|20)\d{2}[^)]*)\)", text)
        if match:
            raw_date = _clean_text(match.group(1))

        journal_part = re.sub(r"(?i)^published\s+in\s*:\s*", "", text).strip()
        if raw_date:
            journal_part = journal_part.replace(f"({raw_date})", "")
        journal_part = re.sub(r"\((?:19|20)\d{2}[^)]*\)", "", journal_part)
        journal_part = _clean_text(journal_part.strip(" :-"))
        return journal_part or None, raw_date

    def _parse_record_container(self, container, source_url: str, node_id: str | None) -> dict | None:
        h3 = container.find("h3")
        if not h3:
            return None

        title = _clean_text(h3.get_text(" ", strip=True))
        if not title:
            return None

        anchor_id = _clean_text(h3.get("id") or "") or _slugify(title)
        cc_detail_url = f"{_START_URL}#{anchor_id}" if anchor_id else _START_URL

        journal = None
        journal_raw = None
        date_raw = None
        abstract_parts = []
        article_url = None
        article_text = None

        content_col = container.select_one("div[class*='grid-col-8']") or container
        for p in content_col.find_all("p", recursive=False):
            text = _clean_text(p.get_text(" ", strip=True))
            if not text:
                continue

            if "published in" in text.lower():
                journal, date_raw = self._parse_date_line(text)
                journal_raw = text
                continue

            link = p.find("a", href=True)
            if link and "read" in text.lower() and "article" in text.lower():
                article_url = urljoin(_BASE_URL, link.get("href", "").strip())
                article_text = _clean_text(link.get_text(" ", strip=True))
                continue

            if len(text) >= _MIN_ABSTRACT_CHARS:
                abstract_parts.append(text)

        abstract = _clean_text(" ".join(abstract_parts))
        if not article_url and container.find("a", href=True):
            article_url = urljoin(_BASE_URL, container.find("a", href=True).get("href", "").strip())

        if not abstract or not article_url:
            return None

        published_date = _parse_month_year(date_raw)
        doi = _extract_doi(article_url)
        pmid = _extract_pmid(article_url)
        pdf_url = article_url if urlparse(article_url).path.lower().endswith(".pdf") else None
        original_filename = _filename_from_url(pdf_url)

        # PMID is the best numeric progression key when present. For non-PubMed
        # links, DOI or the CC anchor slug keeps incremental collection stable.
        post_number = pmid or doi or anchor_id or None
        external_id = post_number or article_url

        metadata = {
            "posted_date": date_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "anchor_id": anchor_id,
            "cc_detail_url": cc_detail_url,
            "article_url": article_url,
            "article_link_text": article_text,
            "source_page": source_url,
            "pmid": pmid,
            "doi": doi,
            "list_date_raw": date_raw,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SITE_ID}:{external_id}")),
            "site_id": _SITE_ID,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "posted_date": published_date,
            "authors": None,
            "publisher": "NIH Clinical Center",
            "department": "Clinical Center",
            "journal": journal,
            "url": article_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": "Published Research",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_records(self, raw: str, source_url: str) -> list[dict]:
        soup = _make_soup(raw)
        if not soup:
            return []

        node_id = self._extract_node_id(raw)
        records = []
        seen = set()

        for container in soup.select("article div.grid-row.grid-gap"):
            record = self._parse_record_container(container, source_url, node_id)
            if not record:
                continue
            key = record.get("url") or record.get("external_id")
            if key in seen:
                continue
            seen.add(key)
            records.append(record)

        return records

    def _parse_detail_record(self, raw: str, anchor_id: str, fallback: dict) -> dict:
        """Parse one anchored record from a fetched detail page."""
        if not raw or not anchor_id:
            return fallback

        soup = _make_soup(raw)
        if not soup:
            return fallback

        h3 = None
        expected_title = _clean_text(fallback.get("title"))
        for candidate in soup.find_all("h3", id=anchor_id):
            if not expected_title or _clean_text(candidate.get_text(" ", strip=True)) == expected_title:
                h3 = candidate
                break
        if not h3 and expected_title:
            for candidate in soup.find_all("h3"):
                if _clean_text(candidate.get_text(" ", strip=True)) == expected_title:
                    h3 = candidate
                    break
        if not h3:
            return fallback

        container = h3.find_parent("div", class_=lambda c: c and "grid-row" in c)
        if not container:
            return fallback

        parsed = self._parse_record_container(
            container,
            fallback.get("url") or _START_URL,
            self._extract_node_id(raw),
        )
        if not parsed:
            return fallback

        # Preserve the source page URL from the list parse in metadata.
        try:
            meta = json.loads(parsed.get("metadata") or "{}")
            old_meta = json.loads(fallback.get("metadata") or "{}")
            if old_meta.get("source_page"):
                meta["source_page"] = old_meta["source_page"]
            parsed["metadata"] = json.dumps(meta, ensure_ascii=False)
        except Exception:
            pass
        return parsed

    def crawl(self, limit=None):
        saved = 0
        page = 1
        start_time = time.time()
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached")
                break
            if time.time() - start_time >= _MAX_WALL_SECONDS:
                print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._page_url(page)
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] page {page}: fetch failed; stopping")
                break

            try:
                records = self._parse_records(raw, list_url)
            except Exception as exc:
                print(f"[{_SITE_ID}] page {page}: parse failed: {exc}")
                records = []

            if not records:
                print(f"[{_SITE_ID}] page {page}: 0 records; stopping")
                break

            new_records = []
            for record in records:
                dedupe_url = record.get("url") or record.get("external_id")
                if dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                new_records.append(record)

            if not new_records:
                print(f"[{_SITE_ID}] page {page}: no new records; stopping")
                break

            has_next = self._has_next_page(raw)

            for item in new_records:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _MAX_WALL_SECONDS:
                    print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget mid-page; exiting cleanly")
                    break

                item_label = item.get("external_id") or item.get("url") or item.get("title", "?")
                try:
                    if self._delay:
                        time.sleep(self._delay)

                    anchor_id = None
                    cc_detail_url = None
                    try:
                        meta = json.loads(item.get("metadata") or "{}")
                        anchor_id = meta.get("anchor_id")
                        cc_detail_url = meta.get("cc_detail_url")
                    except Exception:
                        anchor_id = None

                    detail_raw = self._curl_get(cc_detail_url or item["url"])
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {item_label} failed: detail fetch returned empty")
                        continue

                    parsed = self._parse_detail_record(detail_raw, anchor_id or "", item)
                    abstract = _clean_text(parsed.get("abstract"))
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{_SITE_ID}] item {item_label} abstract too short "
                            f"({len(abstract)} chars); skipping"
                        )
                        continue

                    parsed["abstract"] = abstract
                    self._save_paper(parsed)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {parsed['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not has_next:
                print(f"[{_SITE_ID}] page {page}: no next page link; stopping")
                break

            page += 1

        print(f"[{_SITE_ID}] done. saved {saved}")
        return saved
