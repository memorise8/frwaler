# -*- coding: utf-8 -*-
"""Incheon Institute research report crawler.

Target:
https://www.ii.re.kr/base/board/list?boardManagementNo=14&menuLevel=2&menuNo=76

The site renders its list and detail records as HTML. PDF downloads are exposed
through /base/linked/report/download and the original filename is supplied in
the download response's Content-Disposition header.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from html import unescape
from typing import Any

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - runtime environment should provide bs4
    BeautifulSoup = None


_PARSERS = ("html5lib", "lxml", "html.parser")
_BOARD_MANAGEMENT_NO = "14"
_MENU_LEVEL = "2"
_MENU_NO = "76"
_PUBLISHER = "인천연구원"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 100


def _make_soup(raw: str, context: str = ""):
    """Parse HTML with a robust BeautifulSoup parser fallback chain."""
    if BeautifulSoup is None:
        print(f"[ii-re-kr-base] BeautifulSoup is not installed; cannot parse {context}")
        return None

    for parser in _PARSERS:
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[ii-re-kr-base] BeautifulSoup({parser}) failed for {context}: {exc}")
            continue
    return None


def _clean_text(value: Any) -> str:
    """Normalize site text without raising on unexpected value types."""
    if value is None:
        return ""
    text = str(value)
    text = unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_iso_date(raw: str | None) -> str | None:
    """Convert common Korean-site date shapes to YYYY-MM-DD where possible."""
    if not raw:
        return None
    value = _clean_text(raw)
    m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = re.search(r"(\d{4})(\d{2})(\d{2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"\d{4}", value)
    if m:
        return f"{value}-01-01"
    return None


def _date_from_storage_path(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/storage/report/(\d{4})/(\d{2})/(\d{2})/", url)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _query_value(url: str, key: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
        values = urllib.parse.parse_qs(parsed.query).get(key)
        return values[0] if values else None
    except Exception:
        return None


def _split_people(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"\s*(?:;|,|·|ㆍ|/|\n)\s*", raw)
    return [p.strip() for p in parts if p.strip()]


def _looks_like_institution(value: str) -> bool:
    return any(token in value for token in ("연구원", "대학교", "대학", "센터", "재단", "기관"))


class IiReKrBaseCrawler(BaseCrawler):
    """Crawler for 인천연구원 연구보고서."""

    site_id = "ii-re-kr-base"
    site_name = "Custom: ii-re-kr-base"
    base_url = "https://www.ii.re.kr"

    _LIST_URL = f"{base_url}/base/board/list"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, params: dict[str, Any] | None = None,
              head: bool = False, referer: str | None = None) -> str | None:
        """Fetch via curl with Korean gov-site TLS settings and retries."""
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{sep}{query}"

        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H", f"Referer: {referer or self._LIST_URL}",
        ]
        if head:
            cmd.append("-I")
        cmd.append(url)

        delays = (1, 3, 9)
        for attempt, delay in enumerate(delays, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=40)
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                print(f"[{self.site_id}] empty curl response (attempt {attempt}/3): {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt}/3): {exc}")

            if attempt < 3:
                time.sleep(delay)

        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    def _absolute_url(self, href: str | None) -> str | None:
        if not href:
            return None
        return urllib.parse.urljoin(self.base_url, unescape(href))

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _list_params(self, page: int) -> dict[str, str]:
        return {
            "boardManagementNo": _BOARD_MANAGEMENT_NO,
            "page": str(page),
            "searchCategory": "",
            "searchType": "",
            "searchWord": "",
            "searchReportYear": "",
            "searchReportCategory": "",
            "menuLevel": _MENU_LEVEL,
            "menuNo": _MENU_NO,
        }

    def _fetch_list_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        raw = self._curl(self._LIST_URL, params=self._list_params(page))
        if not raw:
            return [], False

        soup = _make_soup(raw, f"list page {page}")
        if soup is None:
            return [], False

        items: list[dict[str, Any]] = []
        for li in soup.select("div.board-img__list > ul > li"):
            item = self._parse_list_item(li, page)
            if item:
                items.append(item)

        has_next = False
        if soup.select_one("a.next[href]"):
            has_next = True
        else:
            next_page = str(page + 1)
            for a_tag in soup.select("a[href*='/base/board/list'], a[href*='board/list']"):
                href = unescape(a_tag.get("href", ""))
                if _query_value(href, "page") == next_page:
                    has_next = True
                    break

        return items, has_next

    def _parse_list_item(self, li, page: int) -> dict[str, Any] | None:
        read_a = li.select_one("h4.board-img__text-tit a[href*='/base/board/read']")
        if read_a is None:
            read_a = li.select_one("a[href*='/base/board/read']")
        if read_a is None:
            return None

        detail_url = self._absolute_url(read_a.get("href"))
        if not detail_url:
            return None

        board_no = _query_value(detail_url, "boardNo")
        if not board_no:
            return None

        category = ""
        category_el = read_a.select_one("span")
        if category_el is not None:
            category = _clean_text(category_el.get_text(" ", strip=True))

        title = _clean_text(read_a.get_text(" ", strip=True))
        if category and title.startswith(category):
            title = title[len(category):].strip()

        info: dict[str, str] = {}
        for info_li in li.select("ul.board-img__text-info li"):
            text = _clean_text(info_li.get_text(" ", strip=True))
            if ":" in text:
                key, value = text.split(":", 1)
            elif "：" in text:
                key, value = text.split("：", 1)
            else:
                continue
            info[_clean_text(key)] = _clean_text(value)

        excerpt_el = li.select_one("p.board-img__text-text")
        list_abstract = _clean_text(excerpt_el.get_text(" ", strip=True)) if excerpt_el else ""

        image_el = li.select_one("div.board-img__img img")
        image_url = self._absolute_url(image_el.get("src")) if image_el else None
        listed_date = _date_from_storage_path(image_url)

        preview_url = None
        pdf_url = None
        summary_pdf_url = None
        for link in li.select("div.board-img__link a[href]"):
            href = self._absolute_url(link.get("href"))
            text = _clean_text(link.get_text(" ", strip=True))
            if not href:
                continue
            if "/base/linked/report/preview" in href and preview_url is None:
                preview_url = href
            if "/base/linked/report/download" in href:
                if _query_value(href, "div") == "A3" or "요약" in text:
                    summary_pdf_url = href
                elif pdf_url is None:
                    pdf_url = href

        return {
            "board_no": board_no,
            "post_number": board_no,
            "title": title,
            "category": category,
            "research_period": info.get("연구기간"),
            "research_type": info.get("연구유형"),
            "authors_raw": info.get("연구자"),
            "list_abstract": list_abstract,
            "listed_date": listed_date,
            "posted_date_raw": listed_date,
            "image_url": image_url,
            "preview_url": preview_url,
            "pdf_url": pdf_url,
            "summary_pdf_url": summary_pdf_url,
            "url": detail_url,
            "list_page": page,
        }

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        detail_url = item.get("url")
        if not detail_url:
            raise RuntimeError("missing detail URL")

        raw = self._curl(detail_url, referer=self._LIST_URL)
        if not raw:
            raise RuntimeError("detail fetch failed")

        soup = _make_soup(raw, f"detail {item.get('board_no')}")
        if soup is None:
            raise RuntimeError("detail HTML parse failed")

        root = soup.select_one("div.board-view") or soup
        result: dict[str, Any] = {"raw_fields": {}}

        category_el = root.select_one(".board-view-top__tag")
        if category_el is not None:
            result["category"] = _clean_text(category_el.get_text(" ", strip=True))

        title_el = root.select_one(".board-view-top__text h4")
        if title_el is not None:
            result["title"] = _clean_text(title_el.get_text(" ", strip=True))

        image_el = root.select_one(".board-view-top__img img")
        if image_el is not None:
            result["image_url"] = self._absolute_url(image_el.get("src"))
            result["image_alt"] = _clean_text(image_el.get("alt"))

        fields: dict[str, str] = {}
        authors: list[str] = []
        institutions: list[str] = []
        for li in root.select(".board-view-top__text ul > li"):
            label_el = li.find("b")
            if label_el is None:
                continue
            label = _clean_text(label_el.get_text(" ", strip=True))
            value_node = li.find("div") or li.find("p")
            value = _clean_text(value_node.get_text(" ", strip=True) if value_node else li.get_text(" ", strip=True))
            if value.startswith(label):
                value = value[len(label):].strip()
            fields[label] = value

            if label == "연구자":
                anchors = [_clean_text(a.get_text(" ", strip=True)) for a in li.select("a")]
                candidates = [a for a in anchors if a]
                if not candidates:
                    candidates = _split_people(value)
                for candidate in candidates:
                    if _looks_like_institution(candidate):
                        institutions.append(candidate)
                    else:
                        authors.append(candidate)

        result["raw_fields"] = fields
        if authors:
            result["authors"] = authors
        if institutions:
            result["institutions"] = institutions
        if fields.get("발행년도"):
            result["publication_year_raw"] = fields["발행년도"]
            result["published_date"] = _parse_iso_date(fields["발행년도"])
        if fields.get("연구기간"):
            result["research_period"] = fields["연구기간"]
        if fields.get("연구유형"):
            result["research_type"] = fields["연구유형"]

        sections: dict[str, str] = {}
        for heading in root.select(".board-view-contents h5"):
            label = _clean_text(heading.get_text(" ", strip=True))
            content = heading.find_next_sibling("div", class_="board-view-contents__wrap")
            if label and content is not None:
                sections[label] = _clean_text(content.get_text(" ", strip=True))
        result["sections"] = sections
        if sections.get("연구개요"):
            result["abstract"] = sections["연구개요"]
        if sections.get("연구목차"):
            result["table_of_contents"] = sections["연구목차"]

        keywords = []
        for tag in root.select(".board-view-aside__tag a, .board-view-aside__tag li"):
            keyword = _clean_text(tag.get_text(" ", strip=True)).lstrip("#")
            if keyword and keyword not in keywords:
                keywords.append(keyword)
        if keywords:
            result["keywords"] = keywords

        download_links: list[dict[str, str | None]] = []
        for link in root.select("a[href*='/base/linked/report/download']"):
            href = self._absolute_url(link.get("href"))
            if not href:
                continue
            download_links.append({
                "url": href,
                "text": _clean_text(link.get_text(" ", strip=True)),
                "div": _query_value(href, "div"),
                "projcd": _query_value(href, "projcd"),
                "seq": _query_value(href, "seq"),
                "atchSeq": _query_value(href, "atchSeq"),
            })
        result["download_links"] = download_links

        pdf_url = None
        summary_pdf_url = None
        for link in download_links:
            text = link.get("text") or ""
            if link.get("div") == "A3" or "요약" in text:
                if summary_pdf_url is None:
                    summary_pdf_url = link.get("url")
                continue
            if pdf_url is None:
                pdf_url = link.get("url")
        if pdf_url:
            result["pdf_url"] = pdf_url
        if summary_pdf_url:
            result["summary_pdf_url"] = summary_pdf_url

        return result

    def _original_filename(self, pdf_url: str | None) -> str | None:
        if not pdf_url:
            return None

        headers = self._curl(pdf_url, head=True, referer=self._LIST_URL)
        if not headers:
            return self._filename_from_url(pdf_url)

        filename = None
        m = re.search(r"filename\*=UTF-8''([^;\r\n]+)", headers, re.IGNORECASE)
        if m:
            filename = m.group(1).strip().strip('"')
        else:
            m = re.search(r"filename=\"?([^\";\r\n]+)\"?", headers, re.IGNORECASE)
            if m:
                filename = m.group(1).strip().strip('"').strip("'")

        if filename:
            try:
                filename = urllib.parse.unquote(filename)
            except Exception:
                pass
            return filename

        return self._filename_from_url(pdf_url)

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urllib.parse.urlparse(url)
        tail = urllib.parse.unquote(parsed.path.rstrip("/").split("/")[-1])
        if tail and "." in tail and len(tail) <= 200:
            return tail
        return None

    # ------------------------------------------------------------------
    # Save mapping
    # ------------------------------------------------------------------

    def _paper_from_item(self, item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any] | None:
        board_no = item.get("board_no")
        if not board_no:
            return None

        title = detail.get("title") or item.get("title") or ""
        abstract = detail.get("abstract") or item.get("list_abstract") or ""
        if len(abstract) < len(item.get("list_abstract") or ""):
            abstract = item.get("list_abstract") or abstract
        abstract = _clean_text(abstract)

        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(
                f"[{self.site_id}] item {board_no} abstract too short "
                f"({len(abstract)} chars < {_ABSTRACT_MIN_CHARS}), skipping"
            )
            return None

        authors = detail.get("authors")
        if not authors:
            authors = _split_people(item.get("authors_raw") or "")
        authors_str = "; ".join(dict.fromkeys(a for a in authors if a))

        publisher_parts = [_PUBLISHER]
        for institution in detail.get("institutions") or []:
            if institution and institution not in publisher_parts:
                publisher_parts.append(institution)
        publisher = "; ".join(publisher_parts)

        listed_date = (
            item.get("listed_date")
            or _date_from_storage_path(detail.get("image_url"))
            or detail.get("published_date")
        )
        posted_date_raw = item.get("posted_date_raw") or listed_date
        published_date = detail.get("published_date") or listed_date

        pdf_url = detail.get("pdf_url") or item.get("pdf_url")
        summary_pdf_url = detail.get("summary_pdf_url") or item.get("summary_pdf_url")
        original_filename = self._original_filename(pdf_url)

        keywords_list = detail.get("keywords") or []
        keywords = ", ".join(dict.fromkeys(k for k in keywords_list if k))

        selected_download = {}
        for link in detail.get("download_links") or []:
            if link.get("url") == pdf_url:
                selected_download = link
                break

        metadata = {
            "posted_date": posted_date_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "boardNo": board_no,
            "boardManagementNo": _BOARD_MANAGEMENT_NO,
            "menuLevel": _MENU_LEVEL,
            "menuNo": _MENU_NO,
            "post_number": item.get("post_number"),
            "list_page": item.get("list_page"),
            "category": detail.get("category") or item.get("category"),
            "publication_year_raw": detail.get("publication_year_raw"),
            "research_period": detail.get("research_period") or item.get("research_period"),
            "research_type": detail.get("research_type") or item.get("research_type"),
            "authors_raw": item.get("authors_raw"),
            "publisher_raw": detail.get("institutions") or [_PUBLISHER],
            "image_url": detail.get("image_url") or item.get("image_url"),
            "image_alt": detail.get("image_alt"),
            "preview_url": item.get("preview_url"),
            "summary_pdf_url": summary_pdf_url,
            "download_links": detail.get("download_links") or [],
            "selected_download": selected_download,
            "projcd": selected_download.get("projcd"),
            "seq": selected_download.get("seq"),
            "div": selected_download.get("div"),
            "atchSeq": selected_download.get("atchSeq"),
            "table_of_contents": detail.get("table_of_contents"),
            "detail_fields": detail.get("raw_fields") or {},
            "list_abstract": item.get("list_abstract"),
        }

        return {
            "id": f"{self.site_id}:{board_no}",
            "site_id": self.site_id,
            "external_id": str(board_no),
            "post_number": str(item.get("post_number") or board_no),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors_str,
            "publisher": publisher,
            "department": None,
            "journal": None,
            "url": item.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": detail.get("category") or item.get("category"),
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed >= _MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly")
                break

            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            try:
                items, has_next = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} failed: {exc}")
                page += 1
                continue

            if not items:
                print(f"[{self.site_id}] page {page}: no records returned. Done.")
                break

            new_items = [item for item in items if item.get("url") and item["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page}: all item URLs already seen. Done.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= _MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly")
                    return saved

                item_id = item.get("board_no") or item.get("url") or "unknown"
                seen_urls.add(item["url"])

                try:
                    time.sleep(max(0.0, float(getattr(self, "_delay", 1.0))))
                    detail = self._fetch_detail(item)
                    paper = self._paper_from_item(item, detail)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if not has_next and page >= _MAX_PAGES:
                print(f"[{self.site_id}] page {page}: no next-page link and cap reached. Done.")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page}: no next-page link. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
