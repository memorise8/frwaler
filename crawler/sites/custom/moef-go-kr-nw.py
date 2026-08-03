# -*- coding: utf-8 -*-
"""Crawler for MOEF news press/reference materials.

Start page:
https://www.moef.go.kr/nw/nes/nesdta.do?bbsId=MOSFBBS_000000000028&menuNo=4010100

The public site currently serves the board as HTML form endpoints:
- list:   /nw/nes/nesdta.do
- detail: /nw/nes/detailNesDtaView.do
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_SITE_ID = "moef-go-kr-nw"
_BASE_URL = "https://www.moef.go.kr"
_LIST_PATH = "/nw/nes/nesdta.do"
_DETAIL_PATH = "/nw/nes/detailNesDtaView.do"
_BBS_ID = "MOSFBBS_000000000028"
_MENU_NO = "4010100"
_CATEGORY = "보도·참고자료"
_PUBLISHER = "재정경제부"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_CURL_RETRY_DELAYS = (1, 3, 9)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(html.unescape(str(value)))
    value = value.replace("\xa0", " ")
    value = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _strip_tags(markup: str | None) -> str:
    if not markup:
        return ""
    decoded = html.unescape(html.unescape(markup))
    try:
        soup = _make_soup(decoded, site_id=_SITE_ID, context="embedded body")
        return _clean_text(soup.get_text(" ", strip=True))
    except Exception:
        return _clean_text(re.sub(r"<[^>]+>", " ", decoded))


def _make_soup(raw: str | bytes | None, *, site_id: str, context: str) -> BeautifulSoup:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw or ""

    last_exc: Exception | None = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception as exc:
            last_exc = exc
            print(f"[{site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")

    print(f"[{site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
    return BeautifulSoup("", "html.parser")


def _clean_url(url: str | None) -> str | None:
    if not url:
        return None
    full = urljoin(_BASE_URL, html.unescape(url))
    parts = urlsplit(full)
    path = re.sub(r";jsessionid=[^/?#]+", "", parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _parse_date(raw: str | None) -> str | None:
    text = _clean_text(raw).rstrip(".")
    if not text:
        return None
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return text


def _post_number_from_ntt_id(ntt_id: str | None) -> str | None:
    if not ntt_id:
        return None
    m = re.search(r"(\d+)$", ntt_id)
    return m.group(1) if m else ntt_id


class MoefGoKrNwCrawler(BaseCrawler):
    site_id = "moef-go-kr-nw"
    site_name = "Custom: moef-go-kr-nw"
    base_url = "https://www.moef.go.kr"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        while True:
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break
            if self._near_budget(start_time):
                print(f"[{self.site_id}] 25-minute budget reached; stopping cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw_list = self._curl_get(list_url, context=f"list page {page}")
            if not raw_list:
                print(f"[{self.site_id}] list page {page}: empty response; stopping")
                break

            items = self._parse_list(raw_list)
            if not items:
                print(f"[{self.site_id}] list page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if self._near_budget(start_time):
                    print(f"[{self.site_id}] 25-minute budget reached mid-page; stopping cleanly")
                    return saved

                detail_url = item["url"]
                item_label = item.get("external_id") or detail_url
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail {item_label}",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(item, detail_raw)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def _list_url(self, page: int) -> str:
        return (
            f"{_BASE_URL}{_LIST_PATH}?bbsId={_BBS_ID}"
            f"&menuNo={_MENU_NO}&pageIndex={page}"
        )

    def _detail_url(self, ntt_id: str) -> str:
        return (
            f"{_BASE_URL}{_DETAIL_PATH}?searchBbsId1={_BBS_ID}"
            f"&searchNttId1={ntt_id}&menuNo={_MENU_NO}"
        )

    def _near_budget(self, start_time: float) -> bool:
        return time.monotonic() - start_time >= _CRAWL_BUDGET_SECONDS - 10

    def _curl_get(self, url: str, *, context: str, referer: str | None = None) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            "45",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt, wait in enumerate(_CURL_RETRY_DELAYS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=55,
                    check=False,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                last_error = (
                    result.stderr.decode("utf-8", errors="replace").strip()
                    or f"curl exit {result.returncode}"
                )
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except OSError as exc:
                last_error = str(exc)

            if attempt < len(_CURL_RETRY_DELAYS):
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _parse_list(self, raw: str) -> list[dict[str, Any]]:
        soup = _make_soup(raw, site_id=self.site_id, context="list page")
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for li in soup.select("ul.boardType3.explnList > li"):
            item = self._parse_list_li(li)
            if not item:
                continue
            if item["external_id"] in seen_ids:
                continue
            seen_ids.add(item["external_id"])
            items.append(item)

        if items:
            return items

        # Fallback for malformed pages where BS4 could not preserve the list.
        pattern = re.compile(
            r"<li\b.*?<h3>\s*<a[^>]+fn_egov_select\(['\"]([^'\"]+)['\"]\)[^>]*>"
            r"(.*?)</a>.*?<span\s+class=['\"]date['\"]>(.*?)</span>"
            r".*?<span\s+class=['\"]depart['\"]>(.*?)</span>.*?</li>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(raw):
            ntt_id = _clean_text(match.group(1))
            title = _strip_tags(match.group(2))
            listed_raw = _clean_text(match.group(3))
            department = _clean_text(match.group(4))
            if not ntt_id or ntt_id in seen_ids:
                continue
            seen_ids.add(ntt_id)
            items.append(self._list_item(ntt_id, title, listed_raw, department, []))

        return items

    def _parse_list_li(self, li) -> dict[str, Any] | None:
        link = li.find("a", href=re.compile(r"fn_egov_select"))
        if not link:
            return None

        href = link.get("href", "")
        match = re.search(r"fn_egov_select\(['\"]([^'\"]+)['\"]\)", href)
        if not match:
            return None

        ntt_id = _clean_text(match.group(1))
        title = _clean_text(link.get_text(" ", strip=True))
        listed_raw = _clean_text(li.select_one(".boardInfo .date").get_text(" ", strip=True)) if li.select_one(".boardInfo .date") else ""
        department = _clean_text(li.select_one(".boardInfo .depart").get_text(" ", strip=True)) if li.select_one(".boardInfo .depart") else ""
        attachment_ids = []
        for a in li.select('a[href*="atchFileId="]'):
            href = html.unescape(a.get("href", ""))
            params = parse_qs(urlsplit(_clean_url(href) or "").query)
            atch_file_id = (params.get("atchFileId") or [None])[0]
            if atch_file_id and atch_file_id not in attachment_ids:
                attachment_ids.append(atch_file_id)

        return self._list_item(ntt_id, title, listed_raw, department, attachment_ids)

    def _list_item(
        self,
        ntt_id: str,
        title: str,
        listed_raw: str,
        department: str,
        attachment_ids: list[str],
    ) -> dict[str, Any]:
        return {
            "external_id": ntt_id,
            "post_number": _post_number_from_ntt_id(ntt_id),
            "title": title,
            "listed_date_raw": listed_raw,
            "listed_date": _parse_date(listed_raw),
            "department": department,
            "attachment_ids": attachment_ids,
            "url": self._detail_url(ntt_id),
        }

    def _build_paper(self, item: dict[str, Any], detail_raw: str) -> dict[str, Any]:
        ntt_id = item["external_id"]
        soup = _make_soup(detail_raw, site_id=self.site_id, context=f"detail {ntt_id}")

        title = self._detail_title(soup) or item.get("title") or ntt_id
        raw_detail_date = self._detail_date_raw(soup)
        published_date = _parse_date(raw_detail_date) or item.get("listed_date")
        listed_date = item.get("listed_date") or published_date
        department_info = self._department_info(soup)
        department = department_info.get("담당부서") or item.get("department") or ""
        attachments = self._attachments(soup)
        pdf_attachment = self._pick_pdf_attachment(attachments)
        pdf_url = pdf_attachment.get("url") if pdf_attachment else None
        original_filename = pdf_attachment.get("filename") if pdf_attachment else None
        body_text = self._body_text(soup)
        abstract = self._abstract(
            title=title,
            body_text=body_text,
            published_date=published_date,
            department=department,
            attachments=attachments,
            ntt_id=ntt_id,
        )
        views, downloads = self._counts(soup)

        metadata = {
            "posted_date": item.get("listed_date_raw") or raw_detail_date or listed_date,
            "originalFilename": original_filename,
            "nttId": ntt_id,
            "searchNttId1": ntt_id,
            "bbsId": _BBS_ID,
            "searchBbsId1": _BBS_ID,
            "menuNo": _MENU_NO,
            "post_number": item.get("post_number"),
            "category": _CATEGORY,
            "list_endpoint": _LIST_PATH,
            "detail_endpoint": _DETAIL_PATH,
            "listed_date": listed_date,
            "listed_date_raw": item.get("listed_date_raw"),
            "published_date_raw": raw_detail_date,
            "department": department,
            "department_info": department_info,
            "views": views,
            "downloads": downloads,
            "list_attachment_ids": item.get("attachment_ids") or [],
            "attachments": attachments,
        }

        first_attachment_id = None
        if attachments:
            first_attachment_id = attachments[0].get("atchFileId")
        if first_attachment_id:
            metadata["atchFileId"] = first_attachment_id

        return {
            "id": f"{self.site_id}:{ntt_id}",
            "site_id": self.site_id,
            "external_id": ntt_id,
            "post_number": item.get("post_number"),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": department_info.get("담당자") or None,
            "publisher": _PUBLISHER,
            "department": department or None,
            "journal": None,
            "url": item["url"],
            "pdf_url": pdf_url,
            "keywords": None,
            "category": _CATEGORY,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _detail_title(self, soup: BeautifulSoup) -> str:
        title_el = soup.select_one(".detailBoard > h3")
        if title_el:
            return _clean_text(title_el.get_text(" ", strip=True))
        meta = soup.find("meta", property="og:description")
        return _clean_text(meta.get("content")) if meta else ""

    def _detail_date_raw(self, soup: BeautifulSoup) -> str:
        date_el = soup.select_one(".detailBoard .boardInfo .date")
        return _clean_text(date_el.get_text(" ", strip=True)) if date_el else ""

    def _body_text(self, soup: BeautifulSoup) -> str:
        hidden = soup.find("input", id="nttCn")
        if hidden and hidden.get("value"):
            return _strip_tags(hidden.get("value"))

        content = soup.select_one(".detailBoard .editorCont")
        if content:
            for script in content.find_all("script"):
                script.decompose()
            return _clean_text(content.get_text(" ", strip=True))

        meta = soup.find("meta", property="og:description")
        return _clean_text(meta.get("content")) if meta else ""

    def _department_info(self, soup: BeautifulSoup) -> dict[str, str]:
        info: dict[str, str] = {}
        for li in soup.select("ul.departInfo > li"):
            label_el = li.find("span")
            if not label_el:
                continue
            label = _clean_text(label_el.get_text(" ", strip=True))
            value = _clean_text(li.get_text(" ", strip=True))
            if value.startswith(label):
                value = _clean_text(value[len(label):])
            if label and value:
                info[label] = value
        return info

    def _attachments(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        by_key: OrderedDict[tuple[str | None, str | None], dict[str, Any]] = OrderedDict()

        for a in soup.select('a[href*="/com/cmm/fms/FileDown.do"]'):
            href = _clean_url(a.get("href"))
            if not href:
                continue
            parsed = urlsplit(href)
            params = parse_qs(parsed.query)
            atch_file_id = (params.get("atchFileId") or [None])[0]
            file_sn = (params.get("fileSn") or [None])[0]
            key = (atch_file_id, file_sn)

            text = _clean_text(a.get_text(" ", strip=True))
            filename = text if "." in text and text != "다운로드" else None

            current = by_key.get(key)
            if not current:
                current = {
                    "atchFileId": atch_file_id,
                    "fileSn": file_sn,
                    "url": href,
                    "filename": filename,
                }
                by_key[key] = current
            elif filename and not current.get("filename"):
                current["filename"] = filename

        return list(by_key.values())

    def _pick_pdf_attachment(self, attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
        for attachment in attachments:
            filename = (attachment.get("filename") or "").lower()
            if filename.endswith(".pdf"):
                return attachment
        return None

    def _counts(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        view_el = soup.select_one(".detailBoard .boardInfo .view")
        down_el = soup.select_one(".detailBoard .boardInfo .down")
        views = self._first_digits(view_el.get_text(" ", strip=True)) if view_el else None
        downloads = self._first_digits(down_el.get_text(" ", strip=True)) if down_el else None
        return views, downloads

    def _first_digits(self, text: str) -> str | None:
        match = re.search(r"\d[\d,]*", text or "")
        return match.group(0).replace(",", "") if match else None

    def _abstract(
        self,
        *,
        title: str,
        body_text: str,
        published_date: str | None,
        department: str,
        attachments: list[dict[str, Any]],
        ntt_id: str,
    ) -> str:
        body = _clean_text(body_text)
        if len(body) >= 100:
            return body

        names = [
            attachment.get("filename")
            for attachment in attachments
            if attachment.get("filename")
        ]
        attachment_text = "; ".join(names[:5])
        pieces = [
            f"{title}.",
            f"{published_date or ''} {_PUBLISHER} {_CATEGORY} 게시 자료입니다.",
        ]
        if department:
            pieces.append(f"담당부서: {department}.")
        if body:
            pieces.append(f"본문: {body}")
        if attachment_text:
            pieces.append(f"첨부파일: {attachment_text}.")
        pieces.append(f"사이트 native ID: {ntt_id}.")

        return _clean_text(" ".join(pieces))
