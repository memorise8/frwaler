# -*- coding: utf-8 -*-
"""Busan Metropolitan City press release crawler.

Discovered endpoints:
  - List HTML:   GET /nbtnewsBU?curPage=N
  - Detail HTML: GET /nbtnewsBU/{nttNo}
  - Attachment:  GET /comm/getFile?srvcId=BBSTY3&upperNo={nttNo}&fileTy=ATTACH&fileNo=N
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

from crawler.base_crawler import BaseCrawler


class BusanGoKrNbtnewsbuCrawler(BaseCrawler):
    site_id = "busan-go-kr-nbtnewsbu"
    site_name = "Custom: busan-go-kr-nbtnewsbu"
    base_url = "https://www.busan.go.kr"

    _LIST_PATH = "/nbtnewsBU"
    _MAX_PAGES = 200
    _WALL_CLOCK_SECONDS = 25 * 60
    _STOP_SOON_SECONDS = _WALL_CLOCK_SECONDS - 30
    _MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        saved = 0
        page = 1
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

                    dedupe_url = self._dedupe_url(detail_url)
                    if dedupe_url in seen_urls:
                        continue
                    seen_urls.add(dedupe_url)
                    new_on_page += 1

                    item_label = item.get("nttNo") or detail_url
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
        for anchor in soup.select(".boardTextGallery a.item[href]"):
            href = anchor.get("href") or ""
            ntt_no = self._extract_ntt_no(href)
            if not ntt_no:
                continue

            title_tag = anchor.select_one(".bTitle")
            title = self._clean_text(title_tag.get_text(" ", strip=True) if title_tag else "")

            writer_tag = anchor.select_one(".writer")
            writer_raw = self._clean_text(writer_tag.get_text(" ", strip=True) if writer_tag else "")
            writer_parts = [self._clean_text(part) for part in writer_raw.split("|")]
            writer_parts = [part for part in writer_parts if part]

            department = writer_parts[0] if len(writer_parts) >= 1 else None
            author = writer_parts[1] if len(writer_parts) >= 2 else None
            phone = writer_parts[2] if len(writer_parts) >= 3 else None
            listed_date_raw = writer_parts[3] if len(writer_parts) >= 4 else None
            listed_date = self._normalize_date(listed_date_raw)

            abstract_parts = []
            for text_tag in anchor.select(".tgTxt .txtL"):
                classes = text_tag.get("class") or []
                if "writer" in classes:
                    continue
                text = self._clean_text(text_tag.get_text(" ", strip=True))
                if text:
                    abstract_parts.append(text)
            list_abstract = self._clean_text(" ".join(abstract_parts))

            image_tag = anchor.select_one("img")
            thumbnail_url = None
            thumbnail_alt = None
            if image_tag:
                thumbnail_url = urljoin(self.base_url, image_tag.get("src") or "")
                thumbnail_alt = self._clean_text(image_tag.get("alt") or "")

            detail_url = self._detail_url(ntt_no)
            items.append(
                {
                    "nttNo": ntt_no,
                    "post_number": ntt_no,
                    "title": title,
                    "department": department,
                    "author": author,
                    "phone": phone,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": listed_date,
                    "list_abstract": list_abstract,
                    "url": detail_url,
                    "list_href": urljoin(self.base_url, href),
                    "list_page": page,
                    "thumbnail_url": thumbnail_url,
                    "thumbnail_alt": thumbnail_alt,
                }
            )

        return items

    def _fetch_parse_detail(self, item: dict[str, Any]) -> dict[str, Any] | None:
        detail_url = item["url"]
        raw = self._curl_get(detail_url, referer=self._list_url(item.get("list_page") or 1))
        if not raw:
            print(f"[{self.site_id}] detail fetch failed for {item.get('nttNo')}, skipping.")
            return None

        soup = self._make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] detail parse failed for {item.get('nttNo')}, skipping.")
            return None

        ntt_no = item.get("nttNo") or self._extract_ntt_no(detail_url)
        if not ntt_no:
            print(f"[{self.site_id}] detail has no native id for {detail_url}, skipping.")
            return None

        labels = self._extract_label_map(soup)
        title = (
            self._extract_detail_title(soup)
            or item.get("title")
            or f"Busan press release {ntt_no}"
        )

        department = labels.get("부서명") or item.get("department")
        author = labels.get("작성자") or item.get("author")
        phone = labels.get("전화번호") or item.get("phone")
        published_date_raw = labels.get("작성일") or item.get("listed_date_raw")
        published_date = self._normalize_date(published_date_raw) or item.get("listed_date")
        listed_date_raw = item.get("listed_date_raw") or published_date_raw
        listed_date = item.get("listed_date") or published_date
        view_count = labels.get("조회수")
        subtitle = labels.get("부제목")
        content_text = self._extract_content_text(soup)
        abstract = self._choose_abstract(subtitle, content_text, item.get("list_abstract"))

        attachments = self._extract_attachments(soup, detail_url)
        pdf = self._choose_pdf_attachment(attachments)
        pdf_url = pdf.get("url") if pdf else None
        original_filename = pdf.get("filename") if pdf else None
        if pdf_url and not original_filename:
            original_filename = (
                self._filename_from_url(pdf_url)
                or self._curl_content_disposition_filename(pdf_url, referer=detail_url)
            )

        bbs_no = self._input_value(soup, "bbsNo")
        ccl = labels.get("공공누리")

        metadata = {
            "posted_date": listed_date_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "nttNo": ntt_no,
            "bbsNo": bbs_no,
            "post_number": ntt_no,
            "list_page": item.get("list_page"),
            "list_href": item.get("list_href"),
            "list_title": item.get("title"),
            "list_abstract": item.get("list_abstract"),
            "listed_date": listed_date,
            "listed_date_raw": listed_date_raw,
            "published_date_raw": published_date_raw,
            "department": department,
            "author": author,
            "phone": phone,
            "view_count": view_count,
            "subtitle": subtitle,
            "ccl": ccl,
            "thumbnail_url": item.get("thumbnail_url"),
            "thumbnail_alt": item.get("thumbnail_alt"),
            "attachments": attachments,
            "source_endpoints": {
                "list": f"{self.base_url}{self._LIST_PATH}?curPage={item.get('list_page') or 1}",
                "detail": detail_url,
                "attachment": f"{self.base_url}/comm/getFile",
            },
        }

        if pdf:
            metadata.update(
                {
                    "srvcId": pdf.get("srvcId"),
                    "upperNo": pdf.get("upperNo"),
                    "fileTy": pdf.get("fileTy"),
                    "fileNo": pdf.get("fileNo"),
                }
            )

        return {
            "id": f"{self.site_id}-{ntt_no}",
            "site_id": self.site_id,
            "external_id": ntt_no,
            "post_number": ntt_no,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": author,
            "publisher": "부산광역시",
            "department": department,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": "보도자료",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_label_map(self, soup) -> dict[str, str]:
        labels: dict[str, str] = {}
        for dl in soup.select(".boardView dl"):
            children = [
                child
                for child in dl.children
                if getattr(child, "name", None) in {"dt", "dd"}
            ]
            i = 0
            while i < len(children):
                child = children[i]
                if child.name != "dt":
                    i += 1
                    continue
                key = self._clean_text(child.get_text(" ", strip=True))
                value = ""
                if i + 1 < len(children) and children[i + 1].name == "dd":
                    value = self._clean_text(children[i + 1].get_text(" ", strip=True))
                    i += 2
                else:
                    i += 1
                if key and value:
                    labels[key] = value
        return labels

    def _extract_detail_title(self, soup) -> str | None:
        tag = soup.select_one(".form-data-subject strong")
        if tag:
            title = self._clean_text(tag.get_text(" ", strip=True))
            if title:
                return title

        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            title = self._clean_text(meta.get("content"))
            title = re.split(r"\s*:\s*부산소식", title)[0].strip()
            if title:
                return title
        return None

    def _extract_content_text(self, soup) -> str | None:
        content = soup.select_one(".inner-content")
        if content is None:
            return None
        for removable in content.select("iframe, script, style"):
            removable.extract()
        text = self._clean_text(content.get_text(" ", strip=True))
        return text or None

    def _extract_attachments(self, soup, detail_url: str) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for anchor in soup.select("ul.attfiles a[href], a[href*='/comm/getFile']"):
            href = anchor.get("href") or ""
            if "/comm/getFile" not in href:
                continue
            filename = self._clean_text(anchor.get_text(" ", strip=True))
            url = urljoin(self.base_url, href)
            parsed = urlparse(url)
            query = parse_qs(parsed.query)

            attachment = {
                "url": url,
                "filename": filename or self._filename_from_url(url),
                "srvcId": self._first_query_value(query, "srvcId"),
                "upperNo": self._first_query_value(query, "upperNo"),
                "fileTy": self._first_query_value(query, "fileTy"),
                "fileNo": self._first_query_value(query, "fileNo"),
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
                return candidate
        return cleaned[0] if cleaned else ""

    @staticmethod
    def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
        values = query.get(key)
        if not values:
            return None
        return values[0]

    @staticmethod
    def _input_value(soup, name: str) -> str | None:
        tag = soup.find("input", attrs={"name": name})
        if not tag:
            return None
        value = tag.get("value")
        return str(value).strip() if value is not None else None

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return f"{self.base_url}{self._LIST_PATH}"
        return f"{self.base_url}{self._LIST_PATH}?curPage={page}"

    def _detail_url(self, ntt_no: str) -> str:
        return f"{self.base_url}{self._LIST_PATH}/{ntt_no}"

    @staticmethod
    def _extract_ntt_no(value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"/nbtnewsBU/(\d+)", value)
        if match:
            return match.group(1)
        match = re.search(r"(?:nttNo|upperNo)=([0-9]+)", value)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _dedupe_url(url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    @staticmethod
    def _decode(raw: bytes | str | None) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        for encoding in ("utf-8", "cp949", "euc-kr"):
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
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        text = html.unescape(str(value)).replace("\xa0", " ")
        text = text.replace("\u200b", " ").replace("\ufeff", " ")
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
        return value.strip() or None

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
