# -*- coding: utf-8 -*-
"""Crawler for english.www.gov.cn State Council Gazette archive.

Starting URL:
    https://english.www.gov.cn/archive/statecouncilgazette/

The archive is served as static HTML. The real list endpoints are the
starting page and /archive/statecouncilgazette/page_N.html; detail records are
the linked content_WS*.html pages.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_SITE_ID = "english-www-gov-cn-archive"
_BASE_URL = "https://english.www.gov.cn"
_LIST_URL = f"{_BASE_URL}/archive/statecouncilgazette/"
_PUBLISHER = "The State Council of the People's Republic of China; english.www.gov.cn"
_CATEGORY = "State Council Gazette"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_MIN_ABSTRACT_CHARS = 50

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    lines = []
    for line in value.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _parse_date(value: str | None) -> str:
    """Parse common gov.cn date strings to YYYY-MM-DD."""
    if not value:
        return ""
    text = re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()

    m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if m:
        year, month, day = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return ""

    m = re.search(r"\b([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})\b", text)
    if m:
        month_name = m.group(1).lower().rstrip(".")
        month = _MONTHS.get(month_name)
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(2))).strftime("%Y-%m-%d")
            except ValueError:
                return ""

    return ""


def _title_parenthetical_date(title: str | None) -> str:
    if not title:
        return ""
    m = re.search(r"\(([^()]*(?:\d{4})[^()]*)\)\s*$", title)
    return m.group(1).strip() if m else ""


def _extract_issue(title: str | None) -> str | None:
    if not title:
        return None
    m = re.search(r"Issue\s+No\.\s*(\d+)", title, re.IGNORECASE)
    return m.group(1) if m else None


def _extract_serial(title: str | None) -> str | None:
    if not title:
        return None
    m = re.search(r"Serial\s+No\.\s*(\d+)", title, re.IGNORECASE)
    return m.group(1) if m else None


def _content_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/content_([^/?#]+)\.html?$", url)
    return m.group(1) if m else None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    name = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if "." in name and len(name) <= 200:
        return name
    return None


class EnglishWwwGovCnArchiveCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: english-www-gov-cn-archive"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network and HTML helpers
    # ------------------------------------------------------------------

    def _curl_text(self, url: str, *, timeout: int = 45) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "20",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            url,
        ]
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
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr!r}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _make_soup(raw: str | bytes | None) -> BeautifulSoup | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = raw
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _page_url(page: int) -> str:
        if page <= 1:
            return _LIST_URL
        return f"{_LIST_URL}page_{page}.html"

    # ------------------------------------------------------------------
    # List/detail parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str, page: int) -> tuple[list[dict], bool]:
        soup = self._make_soup(raw)
        if soup is None:
            return [], False

        container = soup.select_one(".Gazette_List") or soup
        items: list[dict] = []
        for a_tag in container.select("a[href]"):
            try:
                href = (a_tag.get("href") or "").strip()
                title = _clean_text(a_tag.get_text(" ", strip=True))
                if not href or not title:
                    continue

                url = urljoin(_LIST_URL, href)
                if "/archive/statecouncilgazette/" not in url or "content_" not in url:
                    continue

                serial = _extract_serial(title)
                issue = _extract_issue(title)
                date_raw = _title_parenthetical_date(title)
                listed_date = _parse_date(date_raw)
                content_slug = _content_slug_from_url(url)

                items.append(
                    {
                        "title": title,
                        "url": url,
                        "href": href,
                        "listed_date": listed_date,
                        "listed_date_raw": date_raw,
                        "post_number": serial or content_slug,
                        "serial": serial,
                        "issue": issue,
                        "content_slug": content_slug,
                        "list_page": page,
                    }
                )
            except Exception as exc:
                print(f"[{_SITE_ID}] list item parse error on page {page}: {exc}")
                continue

        has_next = soup.select_one(".Page_Control_Next a[href]") is not None
        return items, has_next

    def _parse_detail(self, raw: str, detail_url: str, list_item: dict) -> dict:
        soup = self._make_soup(raw)
        if soup is None:
            return {}

        metas = {
            (tag.get("name") or tag.get("property") or "").strip(): (tag.get("content") or "").strip()
            for tag in soup.find_all("meta")
            if (tag.get("name") or tag.get("property"))
        }

        title = ""
        for selector in (".Artical_Title", ".conter-conter h3", "title"):
            node = soup.select_one(selector)
            if node:
                title = _clean_text(node.get_text(" ", strip=True))
                if title:
                    break
        if not title:
            title = list_item.get("title", "")

        content_node = soup.select_one(".Artical_Content")
        if content_node is None:
            content_node = soup.select_one("#sp")
        if content_node is None:
            content_node = soup.select_one("content")
        abstract = _clean_text(content_node.get_text("\n", strip=True)) if content_node else ""

        info_node = soup.select_one(".Artical_Info_Div")
        if info_node is None:
            legacy_info_nodes = soup.select(".adio")
            info_text = _clean_text("\n".join(n.get_text(" ", strip=True) for n in legacy_info_nodes))
        else:
            info_text = _clean_text(info_node.get_text(" ", strip=True))

        updated_raw = ""
        m = re.search(
            r"Updated:\s*(.*?)(?:\s+english(?:\.www)?\.gov\.cn|$)",
            info_text,
            re.IGNORECASE,
        )
        if m:
            updated_raw = m.group(1).strip()

        published_date = (
            _parse_date(metas.get("publishdate"))
            or _parse_date(updated_raw)
            or list_item.get("listed_date")
            or _parse_date(_title_parenthetical_date(title))
        )

        pdf_url = None
        original_filename = None
        for a_tag in soup.find_all("a", href=True):
            href = (a_tag.get("href") or "").strip()
            if ".pdf" not in href.lower():
                continue
            pdf_url = urljoin(detail_url, href)
            original_filename = _filename_from_url(pdf_url)
            break

        content_slug = _content_slug_from_url(detail_url)
        native_content_id = metas.get("contentid") or content_slug
        external_id = content_slug or native_content_id or metas.get("eomportal-uuid")

        keywords_raw = metas.get("keywords", "")
        keywords = ""
        if keywords_raw and keywords_raw != title:
            keywords = ", ".join(
                part.strip()
                for part in re.split(r"[,;]", keywords_raw)
                if part.strip()
            )

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "updated_raw": updated_raw,
            "info_text": info_text,
            "external_id": external_id,
            "native_content_id": native_content_id,
            "content_slug": content_slug,
            "authors": metas.get("author") or "",
            "source": metas.get("source") or ("english.gov.cn" if "english.gov.cn" in info_text else ""),
            "keywords": keywords,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "metas": metas,
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        page = 1
        seen_urls: set[str] = set()
        started_at = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping.")
                break
            if time.time() - started_at > _MAX_WALL_SECONDS - 30:
                print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; stopping cleanly.")
                break
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._page_url(page)
            if page > 1:
                time.sleep(self._delay)
            raw_list = self._curl_text(list_url)
            if not raw_list:
                print(f"[{_SITE_ID}] list page {page} failed; stopping.")
                break

            items, has_next = self._parse_list_page(raw_list, page)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no records; done.")
                break

            new_records_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item.get("url") or ""
                item_label = item.get("post_number") or item.get("content_slug") or item_url or "unknown"
                try:
                    if not item_url:
                        continue
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_records_on_page += 1

                    time.sleep(self._detail_delay)
                    raw_detail = self._curl_text(item_url)
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {item_label} failed: detail fetch failed after retries")
                        continue

                    detail = self._parse_detail(raw_detail, item_url, item)
                    title = detail.get("title") or item.get("title") or ""
                    abstract = detail.get("abstract") or ""
                    if 0 < len(abstract) < 100:
                        abstract = (
                            f"{abstract}\n\n"
                            f"Archive record for {title}, published by english.www.gov.cn "
                            f"as part of the State Council Gazette."
                        )
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{_SITE_ID}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    published_date = detail.get("published_date") or item.get("listed_date") or ""
                    listed_date = item.get("listed_date") or published_date
                    posted_date_raw = item.get("listed_date_raw") or listed_date
                    external_id = (
                        detail.get("external_id")
                        or item.get("content_slug")
                        or item.get("post_number")
                    )
                    post_number = item.get("post_number") or item.get("content_slug")
                    original_filename = detail.get("original_filename")

                    metadata = {
                        "posted_date": posted_date_raw,
                        "listed_date": listed_date,
                        "originalFilename": original_filename,
                        "journal_raw": None,
                        "series": _CATEGORY,
                        "volume": item.get("serial"),
                        "issue": item.get("issue"),
                        "node_id": detail.get("native_content_id"),
                        "contentid": (detail.get("metas") or {}).get("contentid"),
                        "content_slug": detail.get("content_slug") or item.get("content_slug"),
                        "post_number": post_number,
                        "serial": item.get("serial"),
                        "issue_number": item.get("issue"),
                        "list_page": item.get("list_page"),
                        "list_href": item.get("href"),
                        "list_title": item.get("title"),
                        "list_endpoint": list_url,
                        "detail_endpoint": item_url,
                        "published_raw": (detail.get("metas") or {}).get("publishdate"),
                        "updated_raw": detail.get("updated_raw"),
                        "source": detail.get("source"),
                        "author_raw": detail.get("authors"),
                        "meta": detail.get("metas") or {},
                        "raw_info_text": detail.get("info_text"),
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": detail.get("authors") or "",
                        "publisher": _PUBLISHER,
                        "department": _CATEGORY,
                        "journal": "",
                        "url": item_url,
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords") or "",
                        "category": _CATEGORY,
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_or_inf}: {title[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_label} failed: {exc}")
                    continue

            if new_records_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: 0 new records; stopping.")
                break
            if not has_next:
                print(f"[{_SITE_ID}] page {page}: next page link absent; done.")
                break

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
