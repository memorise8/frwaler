# -*- coding: utf-8 -*-
"""대전광역시 보도자료 (daejeon.go.kr/drh) crawler.

Discovered with curl:
  - List HTML:   GET /drh/board/boardNormalList.do
                 ?boardId=normal_0189&menuSeq=6825&pageIndex=N&recordCountPerPage=10
  - Detail HTML: GET /drh/board/boardNormalView.do
                 ?boardId=normal_0189&menuSeq=6825&pageIndex=N&ntatcSeq={external_id}
  - Attachment download (found in /js/cmm/commonUtil.js's ``fileDownLoad``):
                 GET /cmm/Download.do?filePath={urlencoded relpath}&fileName={urlencoded name}

Note: despite the prior probe's note claiming a downloadable PDF was seen on
this board, a scan of 500+ items spread across the full page range (1 to the
last page, ~4439) found every attachment is Hancom Office format
(.hwp/.hwpx) — never .pdf. ``pdf_url`` is therefore only populated when an
attachment's extension is actually ``.pdf``; otherwise it stays ``None`` per
the field spec ("없으면 None"), and the real attachment (whatever its type)
is still captured in ``metadata.attachments`` / ``original_filename``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


class DaejeonGoKrDrhCrawler(BaseCrawler):
    site_id = "daejeon-go-kr-drh"
    site_name = "Custom: daejeon-go-kr-drh"
    base_url = "https://www.daejeon.go.kr"

    _LIST_PATH = "/drh/board/boardNormalList.do"
    _BOARD_ID = "normal_0189"
    _MENU_SEQ = "6825"
    _RECORD_COUNT = 10
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _STOP_SOON_SECONDS = _WALL_CLOCK_SECONDS - 30
    _MIN_ABSTRACT_CHARS = 100

    def crawl(self, limit=None):
        saved = 0
        seen_keys = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"
        stop_due_time = False

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        try:
            for page in range(1, self._MAX_PAGES + 1):
                if time.time() - start_time >= self._STOP_SOON_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly.")
                    stop_due_time = True
                    break

                if limit is not None and saved >= limit:
                    break

                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                list_url = self._list_url(page)
                raw = self._curl_get(list_url, context=f"list page {page}", referer=self.base_url)
                if not raw:
                    print(f"[{self.site_id}] page {page}: list response empty or failed, stopping.")
                    break

                items = self._parse_list(raw, page)
                if not items:
                    print(f"[{self.site_id}] page {page}: no records, stopping.")
                    break

                new_on_page = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    if time.time() - start_time >= self._STOP_SOON_SECONDS:
                        print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly.")
                        stop_due_time = True
                        break

                    dedupe_key = item.get("external_id") or item.get("url")
                    if not dedupe_key or dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    new_on_page += 1

                    item_label = item.get("external_id") or item.get("url")
                    try:
                        paper = self._fetch_parse_detail(item)
                        if paper is None:
                            continue

                        abstract = paper.get("abstract") or ""
                        if len(abstract) < self._MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_label} abstract too short "
                                f"({len(abstract)} chars), skipping"
                            )
                            continue

                        self._save_paper(paper)
                        saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue
                    finally:
                        if self._delay:
                            time.sleep(self._delay)

                if stop_due_time:
                    break

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page}: no new records, stopping.")
                    break
            else:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached, stopping.")
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url: str,
        *,
        context: str = "request",
        referer: str | None = None,
        timeout: int = 45,
    ) -> bytes | None:
        """Fetch URL with curl, TLS cap, and 1s/3s/9s retry schedule."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--http1.1",
            "--compressed",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        for attempt, wait in enumerate((1, 3, 9), start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
                if result.returncode == 0 and result.stdout:
                    return result.stdout

                stderr = self._decode(result.stderr).strip()
                print(
                    f"[{self.site_id}] {context} curl failed attempt {attempt}/3 "
                    f"for {url}: returncode={result.returncode} {stderr[:180]}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] {context} curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < 3:
                time.sleep(wait)

        print(f"[{self.site_id}] {context} skipped after 3 curl failures: {url}")
        return None

    # ------------------------------------------------------------------
    # List/detail parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        return (
            f"{self.base_url}{self._LIST_PATH}"
            f"?boardId={self._BOARD_ID}&menuSeq={self._MENU_SEQ}"
            f"&pageIndex={page}&recordCountPerPage={self._RECORD_COUNT}"
        )

    def _parse_list(self, raw: bytes | str, page: int) -> list[dict[str, Any]]:
        soup = self._make_soup(raw)
        if soup is None:
            return []

        items: list[dict[str, Any]] = []
        rows = soup.select("table.board_table_list tbody tr")
        for row in rows:
            try:
                title_link = row.select_one("td.subject a[href]")
                if title_link is None:
                    continue

                href = title_link.get("href") or ""
                detail_url = urljoin(self.base_url, href)
                external_id = self._query_param(detail_url, "ntatcSeq")
                title = self._clean_text(title_link.get_text(" ", strip=True))
                if not title or not external_id:
                    continue

                num_node = row.select_one("td.num")
                post_number_raw = self._clean_text(num_node.get_text(" ", strip=True)) if num_node else None
                post_number = post_number_raw if post_number_raw and re.fullmatch(r"\d+", post_number_raw) else None

                division_node = row.select_one("td.division")
                department = self._clean_text(division_node.get_text(" ", strip=True)) if division_node else None

                date_node = row.select_one("td.date")
                listed_date_raw = self._clean_text(date_node.get_text(" ", strip=True)) if date_node else None

                counter_node = row.select_one("td.counter")
                view_count = self._clean_text(counter_node.get_text(" ", strip=True)) if counter_node else None

                items.append({
                    "external_id": external_id,
                    "post_number": post_number,
                    "post_number_raw": post_number_raw,
                    "title": title,
                    "url": detail_url,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": self._normalize_date(listed_date_raw),
                    "department": department,
                    "view_count": view_count,
                    "page": page,
                })
            except Exception as exc:
                print(f"[{self.site_id}] row parse failed on page {page}: {exc}")
                continue

        return items

    def _fetch_parse_detail(self, item: dict[str, Any]) -> dict[str, Any] | None:
        detail_url = item["url"]
        raw = self._curl_get(detail_url, context=f"detail {item.get('external_id')}", referer=self._list_url(item["page"]))
        if not raw:
            print(f"[{self.site_id}] item {item.get('external_id')} skipped after detail fetch failure")
            return None

        soup = self._make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] item {item.get('external_id')} skipped after detail parser failure")
            return None

        fields = self._parse_detail_fields(soup)
        title = fields.get("제목") or item.get("title")
        content = self._clean_text(self._text_of(soup.select_one("div.board_txt")))
        abstract = self._build_abstract(title, content, fields, item)

        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            return {
                "site_id": self.site_id,
                "external_id": item.get("external_id"),
                "title": title,
                "abstract": abstract,
            }

        listed_date_raw = item.get("listed_date_raw")
        listed_date = item.get("listed_date")
        published_raw = fields.get("작성일") or listed_date_raw
        published_date = self._normalize_date(published_raw) or listed_date
        department = fields.get("담당부서") or item.get("department")
        author = fields.get("작성자")
        tel = fields.get("문의처")
        category = "보도자료"

        attachments = self._parse_attachments(soup)
        pdf_attachment = self._first_pdf_attachment(attachments)
        pdf_url = pdf_attachment.get("url") if pdf_attachment else None
        original_filename = pdf_attachment.get("filename") if pdf_attachment else None
        if not original_filename and attachments:
            original_filename = attachments[0].get("filename")

        metadata = {
            "posted_date": listed_date_raw,
            "posted_date_iso": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "ntatcSeq": item.get("external_id"),
            "boardId": self._BOARD_ID,
            "menuSeq": self._MENU_SEQ,
            "post_number_raw": item.get("post_number_raw"),
            "department": department,
            "tel": tel,
            "view_count": item.get("view_count") or fields.get("조회수"),
            "attachments": attachments,
            "page": item.get("page"),
        }

        keywords = self._keywords(category, department)

        return {
            "id": f"{self.site_id}:{item.get('external_id')}",
            "site_id": self.site_id,
            "external_id": item.get("external_id"),
            "post_number": item.get("post_number"),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": author,
            "publisher": "대전광역시",
            "department": department,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_detail_fields(self, soup) -> dict[str, str]:
        fields: dict[str, str] = {}
        for li in soup.select("div.board_view > ul > li"):
            try:
                key_node = li.select_one(".subject")
                value_node = li.select_one(".txt")
                if key_node is None or value_node is None:
                    continue
                key = self._clean_text(key_node.get_text(" ", strip=True))
                if not key or key == "첨부파일":
                    continue
                value = self._clean_text(value_node.get_text(" ", strip=True))
                if value:
                    fields[key] = value
            except Exception:
                continue
        return fields

    def _parse_attachments(self, soup) -> list[dict[str, str | None]]:
        attachments: list[dict[str, str | None]] = []
        seen_paths = set()
        for anchor in soup.select("div.board_view a[href*='fileDownLoad']"):
            onclick = anchor.get("href") or ""
            match = re.search(r"fileDownLoad\('([^']+)'\s*,\s*'([^']+)'\)", onclick)
            if not match:
                continue
            file_path, file_name = match.group(1), match.group(2)
            if file_path in seen_paths:
                continue
            seen_paths.add(file_path)

            file_name = self._clean_text(file_name)
            download_url = (
                f"{self.base_url}/cmm/Download.do"
                f"?filePath={quote(file_path, safe='')}&fileName={quote(file_name, safe='')}"
            )
            extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else None

            attachments.append({
                "filename": file_name or None,
                "url": download_url,
                "extension": extension,
                "server_path": file_path,
            })
        return attachments

    def _build_abstract(
        self,
        title: str | None,
        content: str | None,
        fields: dict[str, str],
        item: dict[str, Any],
    ) -> str:
        parts = []
        if content:
            parts.append(content)
        if len(" ".join(parts)) < 100:
            department = fields.get("담당부서") or item.get("department")
            written_date = fields.get("작성일") or item.get("listed_date_raw")
            context_bits = []
            if title:
                context_bits.append(f"제목: {title}.")
            if department:
                context_bits.append(f"담당부서: {department}.")
            if written_date:
                context_bits.append(f"작성일: {written_date}.")
            if context_bits:
                parts.append(" ".join(context_bits))
        return self._clean_text(" ".join(part for part in parts if part))

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw: bytes | str):
        text = self._decode(raw) if isinstance(raw, bytes) else raw
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(text or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
                continue
        return None

    @staticmethod
    def _decode(raw: bytes | str | None) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        for encoding in ("utf-8", "euc-kr", "cp949"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = "" if value is None else str(value)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _text_of(node) -> str:
        if node is None:
            return ""
        return node.get_text(" ", strip=True)

    @staticmethod
    def _normalize_date(raw: str | None) -> str | None:
        if not raw:
            return None
        text = str(raw).strip()
        match = re.search(r"(\d{4})[-.\/](\d{1,2})[-.\/](\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(\d{4})(\d{2})(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        return None

    @staticmethod
    def _query_param(url: str, name: str) -> str | None:
        values = parse_qs(urlparse(url).query).get(name)
        return values[0] if values else None

    @staticmethod
    def _first_pdf_attachment(attachments: list[dict[str, str | None]]) -> dict[str, str | None] | None:
        for attachment in attachments:
            filename = (attachment.get("filename") or "").lower()
            extension = (attachment.get("extension") or "").lower()
            if extension == "pdf" or filename.endswith(".pdf"):
                return attachment
        return None

    def _keywords(self, category: str | None, department: str | None) -> str:
        values = ["대전광역시", "보도자료"]
        for value in (category, department):
            value = self._clean_text(value) if value else ""
            if value and value not in values:
                values.append(value)
        return ",".join(values)
