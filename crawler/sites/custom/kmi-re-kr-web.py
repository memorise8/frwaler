# -*- coding: utf-8 -*-
"""Crawler for KMI press releases.

Target:
    https://www.kmi.re.kr/web/board/list.do?rbsIdx=164

The site exposes regular HTML endpoints:
    list:   /web/board/list.do?rbsIdx=164&page=N
    detail: /web/board/view.do?rbsIdx=164&idx=NNNN
    file:   /web/board/download.do?rbsIdx=164&idx=NNNN&fidx=N
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_SITE_ID = "kmi-re-kr-web"
_BASE_URL = "https://www.kmi.re.kr"
_RBS_IDX = "164"
_LIST_URL = f"{_BASE_URL}/web/board/list.do"
_VIEW_URL = f"{_BASE_URL}/web/board/view.do"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 50
_PUBLISHER = "KMI 한국해양수산개발원"


def _clean(value) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"[\r\t\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _parse_date(raw: str) -> str:
    raw = _clean(raw)
    if not raw:
        return ""
    match = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", raw)
    if match:
        return (
            f"{match.group(1)}-"
            f"{match.group(2).zfill(2)}-"
            f"{match.group(3).zfill(2)}"
        )
    match = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return raw


def _make_soup(raw: str):
    """BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    html = raw or ""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
    return None


def _canonical_detail_url(idx: str) -> str:
    return f"{_VIEW_URL}?{urlencode({'rbsIdx': _RBS_IDX, 'idx': idx})}"


def _query_value(url: str, name: str) -> str:
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get(name) or []
    return values[0] if values else ""


class KmiReKrWebCrawler(BaseCrawler):
    site_id = "kmi-re-kr-web"
    site_name = "Custom: kmi-re-kr-web"
    base_url = "https://www.kmi.re.kr"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, referer: str | None = None, timeout: int = 30) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

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
            except Exception as exc:
                last_error = str(exc)
            else:
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_list_page(self, page: int) -> str | None:
        params = {"rbsIdx": _RBS_IDX}
        if page > 1:
            params["page"] = str(page)
        return self._curl_get(f"{_LIST_URL}?{urlencode(params)}", referer=_BASE_URL + "/")

    def _fetch_detail(self, url: str) -> str | None:
        return self._curl_get(url, referer=f"{_LIST_URL}?rbsIdx={_RBS_IDX}")

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str, page: int) -> tuple[list[dict], bool]:
        soup = _make_soup(raw)
        if soup is None:
            return [], False

        table = soup.select_one("table.bbsListA.wviewT")
        if table is None:
            tables = [t for t in soup.select("table.bbsListA") if "mviewT" not in t.get("class", [])]
            table = tables[0] if tables else None
        if table is None:
            return [], self._has_next_page(soup, page)

        items: list[dict] = []
        for row in table.select("tbody.alignC tr"):
            try:
                cells = row.find_all("td", recursive=False)
                if len(cells) < 5:
                    continue

                link = cells[1].select_one("a[href*='view.do']")
                if link is None:
                    continue

                href = unescape(link.get("href", "")).strip()
                idx = _query_value(href, "idx")
                if not idx:
                    match = re.search(r"[?&]idx=(\d+)", href)
                    idx = match.group(1) if match else ""
                if not idx:
                    continue

                board_number = _clean(cells[0].get_text(" ", strip=True))
                title = _clean(link.get("title") or link.get_text(" ", strip=True))
                listed_date_raw = _clean(cells[2].get_text(" ", strip=True))
                department = _clean(cells[3].get_text(" ", strip=True))
                view_count = _clean(cells[4].get_text(" ", strip=True))
                detail_url = _canonical_detail_url(idx)

                items.append(
                    {
                        "idx": idx,
                        "external_id": idx,
                        "post_number": board_number if board_number else idx,
                        "title": title,
                        "listed_date_raw": listed_date_raw,
                        "listed_date": _parse_date(listed_date_raw),
                        "department": department,
                        "view_count": view_count,
                        "url": detail_url,
                        "source_href": href,
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] list row parse failed on page {page}: {exc}")
                continue

        return items, self._has_next_page(soup, page)

    @staticmethod
    def _has_next_page(soup, page: int) -> bool:
        for link in soup.select("div.paginate a[href]"):
            href = unescape(link.get("href", ""))
            match = re.search(r"[?&]page=(\d+)", href)
            if match and int(match.group(1)) > page:
                return True
        return False

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw: str, item: dict) -> dict:
        soup = _make_soup(raw)
        if soup is None:
            raise ValueError("all BeautifulSoup parsers failed")

        table = soup.select_one("table.bbsViewA.wviewT") or soup.select_one("table.bbsViewA")
        if table is None:
            raise ValueError("detail table not found")

        title_node = table.select_one("th.viewTlt")
        title = _clean(title_node.get_text(" ", strip=True) if title_node else "") or item["title"]

        fields = self._extract_detail_fields(table)
        department = fields.get("담당부서") or item.get("department") or ""
        published_raw = fields.get("보도일") or item.get("listed_date_raw") or ""
        published_date = _parse_date(published_raw)

        attachments = self._extract_attachments(table, item["url"])
        pdf_attachment = self._pick_pdf_attachment(attachments)
        pdf_url = pdf_attachment.get("url") if pdf_attachment else None
        original_filename = pdf_attachment.get("filename") if pdf_attachment else None

        abstract = self._extract_abstract(table)
        if not abstract:
            abstract = self._extract_hwp_json_text(raw)

        return {
            "title": title,
            "department": department,
            "published_date": published_date,
            "published_date_raw": published_raw,
            "abstract": abstract,
            "attachments": attachments,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    @staticmethod
    def _extract_detail_fields(table) -> dict:
        fields: dict[str, str] = {}
        for row in table.select("thead tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            for pos, cell in enumerate(cells[:-1]):
                if cell.name != "th":
                    continue
                label = _clean(cell.get_text(" ", strip=True))
                next_cell = cells[pos + 1]
                if label and next_cell.name == "td":
                    fields[label] = _clean(next_cell.get_text(" ", strip=True))
        return fields

    @staticmethod
    def _extract_attachments(table, detail_url: str) -> list[dict]:
        attachments: list[dict] = []
        for link in table.select("ul.multi_file a[href*='download.do']"):
            href = unescape(link.get("href", "")).strip()
            if not href:
                continue
            url = urljoin(detail_url, href)
            filename = _clean(link.select_one("span").get_text(" ", strip=True) if link.select_one("span") else "")
            if not filename:
                title = _clean(link.get("title") or "")
                filename = re.sub(r"\s*다운로드\s*하기\s*$", "", title).strip()
            fidx = _query_value(url, "fidx")
            attachments.append(
                {
                    "url": url,
                    "filename": filename or None,
                    "fidx": fidx or None,
                    "extension": filename.rsplit(".", 1)[-1].lower() if "." in filename else None,
                }
            )
        return attachments

    @staticmethod
    def _pick_pdf_attachment(attachments: list[dict]) -> dict | None:
        for attachment in attachments:
            filename = (attachment.get("filename") or "").lower()
            if filename.endswith(".pdf"):
                return attachment
        for attachment in attachments:
            url = (attachment.get("url") or "").lower()
            if ".pdf" in url:
                return attachment
        return None

    @staticmethod
    def _extract_abstract(table) -> str:
        content = table.select_one("td._board_cont") or table.select_one("._board_cont")
        if content is None:
            return ""

        for node in content.select("script, style, div.hwp_editor_board_content"):
            node.decompose()
        text = _clean(content.get_text(" ", strip=True))
        return text

    @staticmethod
    def _extract_hwp_json_text(raw: str) -> str:
        parts = re.findall(r'"t"\s*:\s*"((?:\\.|[^"\\])*)"', raw or "")
        decoded: list[str] = []
        for part in parts:
            try:
                text = json.loads(f'"{part}"')
            except Exception:
                text = part
            text = _clean(text)
            if text:
                decoded.append(text)
        return _clean(" ".join(decoded))

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        start_ts = time.time()
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")
                break
            if time.time() - start_ts >= _MAX_WALL_SECONDS - 10:
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            raw = self._fetch_list_page(page)
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            try:
                items, has_next = self._parse_list_page(raw, page)
            except Exception as exc:
                print(f"[{self.site_id}] list page {page} parse failed: {exc}")
                break

            if not items:
                print(f"[{self.site_id}] page {page}: no records")
                break

            new_items = []
            for item in items:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: no new records")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_ts >= _MAX_WALL_SECONDS - 10:
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; stopping")
                    return saved

                try:
                    time.sleep(self._detail_delay)
                    detail_raw = self._fetch_detail(item["url"])
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item.get('idx')} failed: detail fetch returned empty")
                        continue

                    detail = self._parse_detail(detail_raw, item)
                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{self.site_id}] item {item.get('idx')} abstract too short "
                            f"({len(abstract)} chars); skipping"
                        )
                        continue

                    published_date = detail.get("published_date") or item.get("listed_date") or ""
                    original_filename = detail.get("original_filename")
                    metadata = {
                        "posted_date": item.get("listed_date_raw") or item.get("listed_date"),
                        "posted_date_iso": item.get("listed_date"),
                        "originalFilename": original_filename,
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "rbsIdx": _RBS_IDX,
                        "idx": item.get("idx"),
                        "node_id": item.get("idx"),
                        "post_number": item.get("post_number"),
                        "board_number": item.get("post_number"),
                        "view_count": item.get("view_count"),
                        "listed_department": item.get("department"),
                        "detail_department": detail.get("department"),
                        "published_date_raw": detail.get("published_date_raw"),
                        "source_href": item.get("source_href"),
                        "attachments": detail.get("attachments") or [],
                    }

                    paper = {
                        "id": f"{self.site_id}:{item['idx']}",
                        "site_id": self.site_id,
                        "external_id": item["external_id"],
                        "post_number": item.get("post_number"),
                        "title": detail.get("title") or item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": item.get("listed_date"),
                        "posted_date": item.get("listed_date"),
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": detail.get("department") or item.get("department") or "",
                        "journal": "",
                        "url": item["url"],
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": "KMI, 한국해양수산개발원, 보도자료",
                        "category": "보도자료",
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('idx')} failed: {exc}")
                    continue

            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent")
                break
            page += 1

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
