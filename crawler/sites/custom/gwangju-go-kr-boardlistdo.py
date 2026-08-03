# -*- coding: utf-8 -*-
"""Gwangju Metropolitan City press release board crawler.

Discovered endpoints:
  - List HTML:   GET /boardList.do?boardId=BD_0000000027&pageId=www789&movePage=N
  - Detail HTML: GET /boardView.do?pageId=www789&boardId=BD_0000000027&seq={seq}&movePage=1&recordCnt=15
  - Attachment:  GET /fileDownload.do?fileSe=BB&fileKey={boardId}%7C{seq}&fileSn=N&boardId={boardId}&seq={seq}

Note: this WAF blocks requests without a browser-like User-Agent (returns
HTTP 400 "Request Blocked"), so ``curl -A <browser UA>`` is required.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from typing import Any
from urllib.parse import unquote, urljoin

from crawler.base_crawler import BaseCrawler


class GwangjuGoKrBoardlistdoCrawler(BaseCrawler):
    site_id = "gwangju-go-kr-boardlistdo"
    site_name = "Custom: gwangju-go-kr-boardlistdo"
    base_url = "https://www.gwangju.go.kr"

    _BOARD_ID = "BD_0000000027"
    _PAGE_ID = "www789"
    _LIST_PATH = "/boardList.do"
    _DETAIL_PATH = "/boardView.do"
    _PUBLISHER = "광주광역시"

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _STOP_SOON_SECONDS = _WALL_CLOCK_SECONDS - 30
    _MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        stop_due_time = False

        try:
            for page in range(1, self._MAX_PAGES + 1):
                elapsed = time.time() - start_time
                if elapsed >= self._STOP_SOON_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly.")
                    stop_due_time = True
                    break

                if limit is not None and saved >= limit:
                    break

                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                list_url = self._list_url(page)
                raw = self._curl_get(list_url, referer=self.base_url)
                if not raw:
                    print(f"[{self.site_id}] list page {page} failed or empty, stopping.")
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

                    detail_url = item.get("url")
                    if not detail_url:
                        continue

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    item_label = item.get("seq") or detail_url
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

    def _curl_get(self, url: str, *, referer: str | None = None, timeout: int = 45) -> bytes | None:
        """Fetch a URL via curl with TLS cap and 1s/3s/9s retries."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--http1.1",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
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
                    f"[{self.site_id}] curl failed attempt {attempt}/3 for {url}: "
                    f"returncode={result.returncode} {stderr[:180]}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < 3:
                time.sleep(wait)

        print(f"[{self.site_id}] all retries exhausted for {url}")
        return None

    def _curl_content_disposition_filename(
        self, url: str, *, referer: str | None = None, timeout: int = 20
    ) -> str | None:
        """Best-effort filename extraction from Content-Disposition."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--http1.1",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-D",
            "-",
            "-o",
            "/dev/null",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        except Exception:
            return None

        if result.returncode != 0 or not result.stdout:
            return None

        headers = self._decode(result.stdout)
        match = re.search(r"filename\*=(?:UTF-8'')?([^;\r\n]+)", headers, re.I)
        if not match:
            match = re.search(r'filename="?([^";\r\n]+)"?', headers, re.I)
        if not match:
            return None
        return self._clean_text(unquote(match.group(1).strip().strip('"'))) or None

    # ------------------------------------------------------------------
    # Page parsers
    # ------------------------------------------------------------------

    def _parse_list(self, raw: bytes | str, page: int) -> list[dict[str, Any]]:
        soup = self._make_soup(raw)
        if soup is None:
            return []

        items: list[dict[str, Any]] = []
        for row in soup.select(".board_list_body .body_row"):
            anchor = row.select_one(".subject a[href]")
            if anchor is None:
                continue

            seq = anchor.get("data-seq") or self._extract_seq(anchor.get("href") or "")
            if not seq:
                continue

            title = self._clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))

            category_div = row.select_one(".category")
            category = self._last_stripped_string(category_div)

            date_div = row.select_one(".date")
            listed_date_raw = self._last_stripped_string(date_div)
            listed_date = self._normalize_date(listed_date_raw)

            detail_url = self._detail_url(seq)
            items.append(
                {
                    "seq": seq,
                    "post_number": seq,
                    "title": title,
                    "category": category,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": listed_date,
                    "url": detail_url,
                    "list_href": urljoin(self.base_url, anchor.get("href") or ""),
                    "list_page": page,
                }
            )

        return items

    def _fetch_parse_detail(self, item: dict[str, Any]) -> dict[str, Any] | None:
        detail_url = item["url"]
        raw = self._curl_get(detail_url, referer=self._list_url(item.get("list_page") or 1))
        if not raw:
            print(f"[{self.site_id}] detail fetch failed for {item.get('seq')}, skipping.")
            return None

        soup = self._make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] detail parse failed for {item.get('seq')}, skipping.")
            return None

        seq = item.get("seq") or self._extract_seq(detail_url)
        if not seq:
            print(f"[{self.site_id}] detail has no native id for {detail_url}, skipping.")
            return None

        title = self._extract_detail_title(soup) or item.get("title") or f"Gwangju press release {seq}"
        subtitle = self._extract_subtitle(soup)

        info_raw = self._extract_view_info(soup)
        published_date_raw = info_raw
        published_date = self._normalize_date(published_date_raw) or item.get("listed_date")

        listed_date_raw = item.get("listed_date_raw") or published_date_raw
        listed_date = item.get("listed_date") or published_date

        department = self._extract_department(soup)
        content_text = self._extract_content_text(soup)
        abstract = self._choose_abstract(subtitle, content_text)

        attachments = self._extract_attachments(soup, detail_url)
        pdf = self._choose_pdf_attachment(attachments)
        pdf_url = pdf.get("url") if pdf else None
        original_filename = pdf.get("filename") if pdf else None
        if pdf_url and not original_filename:
            original_filename = (
                self._filename_from_url(pdf_url)
                or self._curl_content_disposition_filename(pdf_url, referer=detail_url)
            )

        category = item.get("category")

        metadata = {
            "posted_date": listed_date_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "boardId": self._BOARD_ID,
            "pageId": self._PAGE_ID,
            "seq": seq,
            "post_number": seq,
            "list_page": item.get("list_page"),
            "list_href": item.get("list_href"),
            "list_title": item.get("title"),
            "listed_date": listed_date,
            "listed_date_raw": listed_date_raw,
            "published_date_raw": published_date_raw,
            "department": department,
            "category_raw": category,
            "subtitle": subtitle,
            "attachments": attachments,
            "source_endpoints": {
                "list": self._list_url(item.get("list_page") or 1),
                "detail": detail_url,
                "attachment": f"{self.base_url}/fileDownload.do",
            },
        }

        return {
            "id": f"{self.site_id}-{seq}",
            "site_id": self.site_id,
            "external_id": seq,
            "post_number": seq,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": department,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_detail_title(self, soup) -> str | None:
        tag = soup.select_one(".board_view_head h6")
        if tag:
            title = self._clean_text(tag.get_text(" ", strip=True))
            if title:
                return title

        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            title = self._clean_text(meta.get("content"))
            if title:
                return title
        return None

    def _extract_subtitle(self, soup) -> str | None:
        tag = soup.select_one(".board_view_head p.tc.mt10")
        if tag is None:
            tag = soup.select_one(".board_view_head p")
        if tag is None:
            return None
        text = self._clean_text(tag.get_text(" ", strip=True))
        return text or None

    def _extract_view_info(self, soup) -> str | None:
        container = soup.select_one(".board_view_info")
        if container is None:
            return None
        spans = container.select("span")
        text = self._clean_text((spans[0] if spans else container).get_text(" ", strip=True))
        match = re.search(r"작성일\s*:\s*([0-9.\-/년월일:\s]+)", text)
        if match:
            return match.group(1).strip()
        return text or None

    def _extract_department(self, soup) -> str | None:
        dd = soup.select_one(".charger dl dd")
        if dd is None:
            return None
        text = self._clean_text(dd.get_text(" ", strip=True))
        return text or None

    def _extract_content_text(self, soup) -> str | None:
        content = soup.select_one(".board_view_body")
        if content is None:
            return None
        content = self._safe_copy(content)
        if content is None:
            return None
        for removable in content.select(
            "script, style, .koglSeView, .add_file, .viewimg_comment"
        ):
            removable.decompose()
        text = self._clean_text(content.get_text(" ", strip=True))
        return text or None

    def _extract_attachments(self, soup, detail_url: str) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for li in soup.select(".add_file ul li"):
            anchor = li.select_one("a[href*='fileDownload.do']")
            if anchor is None:
                continue
            href = anchor.get("href") or ""
            if "action=zip" in href:
                continue
            url = urljoin(self.base_url, href)
            filename = self._clean_text(anchor.get_text(" ", strip=True))

            attachment = {
                "url": url,
                "filename": filename or self._filename_from_url(url),
                "extension": self._extension_from_filename(filename),
            }
            if not attachment["filename"]:
                attachment["filename"] = self._curl_content_disposition_filename(url, referer=detail_url)
            if not attachment["extension"]:
                attachment["extension"] = self._extension_from_filename(attachment.get("filename"))
            attachments.append(attachment)

        deduped = []
        seen = set()
        for attachment in attachments:
            url = attachment.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(attachment)
        return deduped

    @staticmethod
    def _choose_pdf_attachment(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
        for attachment in attachments:
            filename = (attachment.get("filename") or "").lower()
            extension = (attachment.get("extension") or "").lower()
            if extension == ".pdf" or filename.endswith(".pdf"):
                return attachment
        return None

    def _choose_abstract(self, *candidates: str | None) -> str:
        cleaned = [self._clean_text(candidate) for candidate in candidates if candidate]
        for candidate in cleaned:
            if len(candidate) >= self._MIN_ABSTRACT_CHARS:
                return candidate[:2000]
        joined = self._clean_text(" ".join(cleaned))
        return joined[:2000]

    @staticmethod
    def _last_stripped_string(tag) -> str | None:
        if tag is None:
            return None
        strings = list(tag.stripped_strings)
        return strings[-1] if strings else None

    def _list_url(self, page: int) -> str:
        return (
            f"{self.base_url}{self._LIST_PATH}?boardId={self._BOARD_ID}"
            f"&pageId={self._PAGE_ID}&movePage={page}"
        )

    def _detail_url(self, seq: str) -> str:
        return (
            f"{self.base_url}{self._DETAIL_PATH}?pageId={self._PAGE_ID}"
            f"&boardId={self._BOARD_ID}&seq={seq}&movePage=1&recordCnt=15"
        )

    @staticmethod
    def _extract_seq(value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"[?&]seq=([0-9]+)", value)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _decode(raw: bytes | str | None) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            for encoding in ("cp949", "euc-kr"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
        return raw.decode("utf-8", errors="replace")

    def _make_soup(self, raw: bytes | str | None):
        from bs4 import BeautifulSoup

        text = self._decode(raw)
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
        return None

    @staticmethod
    def _safe_copy(tag):
        try:
            import copy

            return copy.copy(tag)
        except Exception:
            return tag

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        text = html.unescape(str(value)).replace("\xa0", " ")
        text = text.replace("​", " ").replace("﻿", " ")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\s*\n\s*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _normalize_date(value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", value)
        if match:
            return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
        match = re.search(r"(\d{4})\s*(\d{2})\s*(\d{2})", value)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        return None

    @staticmethod
    def _extension_from_filename(filename: str | None) -> str | None:
        if not filename:
            return None
        match = re.search(r"(\.[A-Za-z0-9]{2,8})(?:$|\?)", filename.strip())
        return match.group(1).lower() if match else None

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        tail = unquote(tail)
        if "." in tail and len(tail) <= 200:
            return tail
        return None
