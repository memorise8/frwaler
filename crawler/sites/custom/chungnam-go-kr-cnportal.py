# -*- coding: utf-8 -*-
"""충청남도 보도자료 crawler.

Discovered with curl:
  - List HTML:   GET /cnportal/cnapcPressList/cnapcPress/list.do
                 ?menuNo=500498&pageIndex=N&pageUnit=30
  - Detail HTML: GET /cnportal/cnapcPressList/cnapcPress/view.do
                 ?nttId={nttId}&menuNo=500498&pageIndex=N

The list page also exposes a legacy citynet source link containing
``matOfYmd`` and ``matSno``. The detail page is the reliable source for
subtitle, body text, contact, category, department, and attachments.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


class ChungnamGoKrCnportalCrawler(BaseCrawler):
    site_id = "chungnam-go-kr-cnportal"
    site_name = "Custom: chungnam-go-kr-cnportal"
    base_url = "https://www.chungnam.go.kr"

    _LIST_PATH = "/cnportal/cnapcPressList/cnapcPress/list.do"
    _DETAIL_PATH = "/cnportal/cnapcPressList/cnapcPress/view.do"
    _MENU_NO = "500498"
    _PAGE_UNIT = 30
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _STOP_SOON_SECONDS = _WALL_CLOCK_SECONDS - 30
    _MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
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

                    detail_url = item.get("url")
                    if not detail_url:
                        continue

                    dedupe_url = self._dedupe_url(detail_url)
                    if dedupe_url in seen_urls:
                        continue
                    seen_urls.add(dedupe_url)
                    new_on_page += 1

                    item_label = item.get("external_id") or item.get("post_number") or detail_url
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

    def _curl_content_disposition_filename(
        self,
        url: str,
        *,
        referer: str | None = None,
        timeout: int = 20,
    ) -> str | None:
        """Best-effort original filename extraction from response headers."""
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
    # List/detail parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        query = urlencode({
            "menuNo": self._MENU_NO,
            "pageIndex": str(page),
            "pageUnit": str(self._PAGE_UNIT),
        })
        return f"{self.base_url}{self._LIST_PATH}?{query}"

    def _parse_list(self, raw: bytes | str, page: int) -> list[dict[str, Any]]:
        soup = self._make_soup(raw)
        if soup is None:
            return []

        items: list[dict[str, Any]] = []
        rows = soup.select(".txt-board tbody tr")
        for row in rows:
            try:
                title_link = row.select_one("td.td-subj a.tit[href]")
                if title_link is None:
                    continue

                href = title_link.get("href") or ""
                detail_url = urljoin(self.base_url, href)
                external_id = self._query_param(detail_url, "nttId")
                title = self._clean_text(title_link.get_text(" ", strip=True))
                if not title or not external_id:
                    continue

                tds = row.find_all("td")
                post_number = self._clean_text(tds[0].get_text(" ", strip=True)) if tds else None
                if post_number and not re.fullmatch(r"\d+", post_number):
                    post_number = None

                citynet_link = row.select_one("a.linkBtn[href]")
                citynet_url = urljoin(self.base_url, citynet_link.get("href")) if citynet_link else None
                citynet_fields = self._citynet_fields(citynet_url)

                department = None
                listed_date_raw = None
                view_count = None
                if len(tds) >= 6:
                    department = self._clean_text(tds[3].get_text(" ", strip=True))
                    listed_date_raw = self._clean_text(tds[4].get_text(" ", strip=True))
                    view_count = self._clean_text(tds[5].get_text(" ", strip=True))
                elif len(tds) >= 5:
                    department = self._clean_text(tds[2].get_text(" ", strip=True))
                    listed_date_raw = self._clean_text(tds[3].get_text(" ", strip=True))
                    view_count = self._clean_text(tds[4].get_text(" ", strip=True))

                items.append({
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "url": detail_url,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": self._normalize_date(listed_date_raw),
                    "department": department,
                    "view_count": view_count,
                    "citynet_url": citynet_url,
                    "citynet_fields": citynet_fields,
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

        title = self._clean_text(self._text_of(soup.select_one(".board-view-title"))) or item.get("title")
        fields = self._parse_detail_fields(soup)
        content = self._clean_text(self._text_of(soup.select_one(".board-view .content")))
        subtitle = fields.get("부제목")
        abstract = self._build_abstract(title, subtitle, content, fields, item)
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            return {
                "site_id": self.site_id,
                "external_id": item.get("external_id"),
                "title": title,
                "abstract": abstract,
            }

        listed_date_raw = item.get("listed_date_raw")
        listed_date = item.get("listed_date")
        published_raw = fields.get("제공일자") or listed_date_raw
        published_date = self._normalize_date(published_raw) or listed_date
        department = fields.get("제공부서") or item.get("department")
        author = fields.get("담당자")
        category = fields.get("구분") or "보도자료"
        phone = fields.get("전화번호")

        attachments = self._parse_attachments(soup, detail_url)
        pdf_attachment = self._first_pdf_attachment(attachments)
        pdf_url = pdf_attachment.get("url") if pdf_attachment else None
        original_filename = pdf_attachment.get("filename") if pdf_attachment else None
        if pdf_url and not original_filename:
            original_filename = (
                self._curl_content_disposition_filename(pdf_url, referer=detail_url)
                or self._filename_from_url(pdf_url)
            )

        original_filename_meta = original_filename
        if not original_filename_meta and attachments:
            original_filename_meta = attachments[0].get("filename")

        citynet_fields = item.get("citynet_fields") or {}
        metadata = {
            "posted_date": listed_date_raw,
            "posted_date_iso": listed_date,
            "originalFilename": original_filename_meta,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "nttId": item.get("external_id"),
            "post_number": item.get("post_number"),
            "menuNo": self._MENU_NO,
            "pageIndex": item.get("page"),
            "pageUnit": self._PAGE_UNIT,
            "department": department,
            "provider_department": department,
            "contact_person": author,
            "phone": phone,
            "category_raw": category,
            "view_count": item.get("view_count"),
            "citynet_url": item.get("citynet_url"),
            "matOfYmd": citynet_fields.get("matOfYmd"),
            "matSno": citynet_fields.get("matSno"),
            "citynet_command": citynet_fields.get("command"),
            "citynet_sido": citynet_fields.get("sido"),
            "citynet_flag": citynet_fields.get("flag"),
            "detail_fields": fields,
            "attachments": attachments,
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
            "publisher": "충청남도",
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
        for wrapper in soup.select(".board-view-li .board-view-inner"):
            try:
                key_node = wrapper.select_one(".k")
                value_node = wrapper.select_one(".v")
                if key_node is None or value_node is None:
                    continue
                key = self._clean_text(key_node.get_text(" ", strip=True))
                if not key or key in {"첨부파일"}:
                    continue
                value = self._clean_text(value_node.get_text(" ", strip=True))
                if value:
                    fields[key] = value
            except Exception:
                continue
        return fields

    def _parse_attachments(self, soup, detail_url: str) -> list[dict[str, str | None]]:
        attachments: list[dict[str, str | None]] = []
        for li in soup.select("ul.view-file-list li"):
            try:
                link = li.select_one("a.ico_file[href]") or li.select_one("a[href*='download']")
                if link is None:
                    continue
                file_url = urljoin(self.base_url, link.get("href") or "")
                filename = (
                    self._filename_from_attachment_node(li, link)
                    or self._filename_from_preview_onclick(li)
                    or self._filename_from_url(file_url)
                )
                extension = None
                if filename and "." in filename:
                    extension = filename.rsplit(".", 1)[-1].lower()
                elif file_url and "." in urlparse(file_url).path:
                    extension = urlparse(file_url).path.rsplit(".", 1)[-1].lower()

                attachments.append({
                    "filename": filename,
                    "url": file_url,
                    "extension": extension,
                })
            except Exception as exc:
                print(f"[{self.site_id}] attachment parse failed: {exc}")
                continue
        return attachments

    def _build_abstract(
        self,
        title: str | None,
        subtitle: str | None,
        content: str | None,
        fields: dict[str, str],
        item: dict[str, Any],
    ) -> str:
        parts = []
        if subtitle:
            parts.append(subtitle)
        if content:
            parts.append(content)
        if len(" ".join(parts)) < 100:
            department = fields.get("제공부서") or item.get("department")
            provided_date = fields.get("제공일자") or item.get("listed_date_raw")
            context_bits = []
            if title:
                context_bits.append(f"제목: {title}.")
            if department:
                context_bits.append(f"제공부서: {department}.")
            if provided_date:
                context_bits.append(f"제공일자: {provided_date}.")
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
        return text or None

    @staticmethod
    def _query_param(url: str, name: str) -> str | None:
        values = parse_qs(urlparse(url).query).get(name)
        return values[0] if values else None

    @staticmethod
    def _citynet_fields(url: str | None) -> dict[str, str]:
        if not url:
            return {}
        parsed = parse_qs(urlparse(url).query)
        return {key: values[0] for key, values in parsed.items() if values}

    @staticmethod
    def _dedupe_url(url: str) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        ntt_id = query.get("nttId", [None])[0]
        if ntt_id:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?nttId={ntt_id}"
        return url

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = urlparse(url).path.rstrip("/").split("/")[-1]
        tail = unquote(tail)
        return tail if "." in tail and len(tail) <= 240 else None

    def _filename_from_attachment_node(self, li, link) -> str | None:
        parts = []
        for child in li.contents:
            if child is link:
                break
            if isinstance(child, str):
                parts.append(child)
            elif hasattr(child, "get_text"):
                parts.append(child.get_text(" ", strip=True))
        filename = self._clean_text(" ".join(parts))
        filename = re.sub(r"(내려받기|미리보기|미리듣기)$", "", filename).strip()
        return filename or None

    def _filename_from_preview_onclick(self, li) -> str | None:
        for anchor in li.select("a[onclick]"):
            onclick = anchor.get("onclick") or ""
            match = re.search(
                r",\s*'([^']+\.(?:pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|png|jpe?g))'",
                onclick,
                re.I,
            )
            if match:
                return self._clean_text(match.group(1))
        return None

    @staticmethod
    def _first_pdf_attachment(attachments: list[dict[str, str | None]]) -> dict[str, str | None] | None:
        for attachment in attachments:
            filename = (attachment.get("filename") or "").lower()
            extension = (attachment.get("extension") or "").lower()
            if extension == "pdf" or filename.endswith(".pdf"):
                return attachment
        return None

    def _keywords(self, category: str | None, department: str | None) -> str:
        values = ["충청남도", "보도자료"]
        for value in (category, department):
            value = self._clean_text(value)
            if value and value not in values:
                values.append(value)
        return ",".join(values)
