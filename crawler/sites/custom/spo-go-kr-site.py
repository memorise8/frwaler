# -*- coding: utf-8 -*-
"""Supreme Prosecutors' Office (spo.go.kr) research bulletin board crawler.

Discovered endpoints:
  - List:   POST /site/spo/ex/board/List.do
            body: pageIndex, cbIdx=1303, bcIdx=0, actGubun=, searchCondition=subCont, searchKeyword=
            (cbIdx must NOT also appear in the URL query string together with a POST body,
            or the WebtoB/JEUS front-end WAF rejects the request with HTTP 400 "JBWEB000065".
            A plain GET to List.do?cbIdx=1303 works for the first page only -- the
            pageIndex query parameter is silently ignored by the server, so real
            pagination requires the POST above.)
  - Detail: GET  /site/spo/ex/board/View.do?cbIdx=1303&bcIdx={bcIdx}
  - File:   GET  /common/board/Download.do?bcIdx={bcIdx}&cbIdx={cbIdx}&streFileNm={storedName}

Every list page repeats a fixed set of pinned ("공지") postings (<li class="noti">)
ahead of that page's own items (<li> with no class). Both are real, distinct
postings -- we parse all of them and rely on ``seen_urls`` dedup so the pinned
set is only saved once.
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


class SpoGoKrSiteCrawler(BaseCrawler):
    site_id = "spo-go-kr-site"
    site_name = "Custom: spo-go-kr-site"
    base_url = "https://www.spo.go.kr"

    _LIST_PATH = "/site/spo/ex/board/List.do"
    _DETAIL_PATH = "/site/spo/ex/board/View.do"
    _CB_IDX = "1303"
    _PUBLISHER = "대검찰청"
    _CATEGORY = "검찰관련 연구자료"

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _STOP_SOON_SECONDS = _WALL_CLOCK_SECONDS - 30
    _MIN_ABSTRACT_CHARS = 50
    _MAX_ABSTRACT_CHARS = 2000

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

                raw = self._fetch_list_page(page)
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

                    item_label = item.get("bc_idx") or detail_url
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

    def _curl(self, cmd: list, timeout: int) -> bytes | None:
        """Run a prepared curl command with 1s/3s/9s retries."""
        for attempt, wait in enumerate((1, 3, 9), start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
                if result.returncode == 0 and result.stdout:
                    return result.stdout

                stderr = self._decode(result.stderr).strip()
                print(
                    f"[{self.site_id}] curl failed attempt {attempt}/3: "
                    f"returncode={result.returncode} {stderr[:180]}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3: {exc}")

            if attempt < 3:
                time.sleep(wait)

        print(f"[{self.site_id}] all retries exhausted")
        return None

    def _fetch_list_page(self, page: int, timeout: int = 45) -> bytes | None:
        """POST to List.do. ``cbIdx`` must live only in the body -- putting it
        in the URL query string too triggers the site's WAF (HTTP 400)."""
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
            "-X",
            "POST",
            "-H",
            "Content-Type: application/x-www-form-urlencoded",
            "-H",
            f"Referer: {self.base_url}{self._LIST_PATH}?cbIdx={self._CB_IDX}",
            "--data-urlencode",
            f"pageIndex={page}",
            "--data-urlencode",
            f"cbIdx={self._CB_IDX}",
            "--data-urlencode",
            "bcIdx=0",
            "--data-urlencode",
            "actGubun=",
            "--data-urlencode",
            "searchCondition=subCont",
            "--data-urlencode",
            "searchKeyword=",
            f"{self.base_url}{self._LIST_PATH}",
        ]
        return self._curl(cmd, timeout)

    def _curl_get(self, url: str, *, referer: str | None = None, timeout: int = 45) -> bytes | None:
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
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)
        return self._curl(cmd, timeout)

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
        for li in soup.select(".reportList ul li"):
            anchor = li.find("a", href=True) or li.find(
                "a", attrs={"href": re.compile(r"javascript:doBbsContentView")}
            )
            if anchor is None:
                continue

            href = anchor.get("href") or ""
            onclick = anchor.get("onclick") or ""
            bc_idx = self._extract_bc_idx(href) or self._extract_bc_idx(onclick)
            if not bc_idx:
                continue

            title_tag = li.select_one("span.top b.title")
            title = self._clean_text(title_tag.get_text(" ", strip=True)) if title_tag else None

            listed_date_raw = self._last_stripped_string(li.select_one("span.info span.date"))
            listed_date = self._normalize_date(listed_date_raw)

            detail_url = self._detail_url(bc_idx)
            items.append(
                {
                    "bc_idx": bc_idx,
                    "post_number": bc_idx,
                    "title": title,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": listed_date,
                    "url": detail_url,
                    "list_page": page,
                }
            )

        return items

    def _fetch_parse_detail(self, item: dict[str, Any]) -> dict[str, Any] | None:
        detail_url = item["url"]
        raw = self._curl_get(detail_url, referer=f"{self.base_url}{self._LIST_PATH}?cbIdx={self._CB_IDX}")
        if not raw:
            print(f"[{self.site_id}] detail fetch failed for {item.get('bc_idx')}, skipping.")
            return None

        soup = self._make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] detail parse failed for {item.get('bc_idx')}, skipping.")
            return None

        bc_idx = item.get("bc_idx") or self._extract_bc_idx(detail_url)
        if not bc_idx:
            print(f"[{self.site_id}] detail has no native id for {detail_url}, skipping.")
            return None

        view = soup.select_one("div.brdView dl.view")
        title_tag = view.select_one("dt span.title") if view else None
        title = self._clean_text(title_tag.get_text(" ", strip=True)) if title_tag else None
        title = title or item.get("title") or f"SPO research bulletin {bc_idx}"

        published_date_raw = self._last_stripped_string(view.select_one("dt div.etc span.date")) if view else None
        published_date = self._normalize_date(published_date_raw) or item.get("listed_date")

        listed_date_raw = item.get("listed_date_raw") or published_date_raw
        listed_date = item.get("listed_date") or published_date

        content_tag = view.select_one("dd div.cont") if view else None
        content_text = self._extract_content_text(content_tag)

        department = self._extract_department(title, content_text)
        journal_raw, issue = self._extract_journal_issue(title)

        attachments = self._extract_attachments(view, detail_url) if view else []
        pdf = self._choose_pdf_attachment(attachments)
        pdf_url = pdf.get("url") if pdf else None
        original_filename = pdf.get("filename") if pdf else None
        if pdf_url and not original_filename:
            original_filename = (
                self._filename_from_url(pdf_url)
                or self._curl_content_disposition_filename(pdf_url, referer=detail_url)
            )

        metadata = {
            "posted_date": listed_date_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": None,
            "volume": None,
            "issue": issue,
            "cbIdx": self._CB_IDX,
            "bcIdx": bc_idx,
            "list_page": item.get("list_page"),
            "list_title": item.get("title"),
            "listed_date": listed_date,
            "listed_date_raw": listed_date_raw,
            "published_date_raw": published_date_raw,
            "department": department,
            "attachments": attachments,
            "source_endpoints": {
                "list": f"{self.base_url}{self._LIST_PATH}",
                "detail": detail_url,
                "attachment": f"{self.base_url}/common/board/Download.do",
            },
        }

        return {
            "id": f"{self.site_id}-{bc_idx}",
            "site_id": self.site_id,
            "external_id": bc_idx,
            "post_number": bc_idx,
            "title": title,
            "abstract": content_text or "",
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": department,
            "journal": journal_raw,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": self._CATEGORY,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_content_text(self, content_tag) -> str | None:
        if content_tag is None:
            return None
        content = self._safe_copy(content_tag)
        if content is None:
            return None
        for removable in content.select("script, style"):
            removable.decompose()
        text = self._clean_text(content.get_text(" ", strip=True))
        if not text:
            return None
        return text[: self._MAX_ABSTRACT_CHARS]

    @staticmethod
    def _extract_department(title: str | None, content_text: str | None) -> str | None:
        if title:
            match = re.match(r"^\[([가-힣A-Za-z0-9·\s]{2,20})\]", title)
            if match:
                return match.group(1).strip()
        if content_text:
            match = re.search(r"대검찰청\s+([가-힣]{2,15}(?:담당관실|과|국|부|팀|실))(?:에서는|에서)", content_text)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _extract_journal_issue(title: str | None) -> tuple[str | None, str | None]:
        if not title:
            return None, None
        journal_match = re.search(r"[「\<]?([가-힣]+의\s*신동향)[」\>]?", title)
        journal = journal_match.group(1).strip() if journal_match else None
        issue_match = re.search(r"통권\s*제?\s*(\d+)\s*호", title)
        issue = issue_match.group(1) if issue_match else None
        return journal, issue

    def _extract_attachments(self, view, detail_url: str) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for anchor in view.select("dd div.file a[href]"):
            href = anchor.get("href") or ""
            if not href.startswith("/common/board/Download.do") and "Download.do" not in href:
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

    @staticmethod
    def _last_stripped_string(tag) -> str | None:
        if tag is None:
            return None
        strings = list(tag.stripped_strings)
        return strings[-1] if strings else None

    def _detail_url(self, bc_idx: str) -> str:
        return f"{self.base_url}{self._DETAIL_PATH}?cbIdx={self._CB_IDX}&bcIdx={bc_idx}"

    @staticmethod
    def _extract_bc_idx(value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"doBbsContentView\('(\d+)'\)", value)
        if match:
            return match.group(1)
        match = re.search(r"[?&]bcIdx=(\d+)", value)
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
