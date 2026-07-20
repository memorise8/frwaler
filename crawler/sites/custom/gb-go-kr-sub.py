# -*- coding: utf-8 -*-
"""Crawler for 경상북도 통계소식 (gb-go-kr-sub).

Board: gb_statboard / mnu_uid=7866 — 경북공공데이터&통계 > 통계소식
List:  https://gb.go.kr/Sub/open_contents/section/datastat/page.do
       ?mnu_uid=7866&BD_CODE=gb_statboard&cmd=1&Start={offset}
       &key=0&word=&p1=0&p2=0&period=1&B_START=2000-01-01&B_END=2030-12-31
Detail: href extracted from each list row
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class GbGoKrSubCrawler(BaseCrawler):
    site_id = "gb-go-kr-sub"
    site_name = "Custom: gb-go-kr-sub"
    base_url = "https://gb.go.kr"

    # period=1 + explicit wide date range → server preserves our range in pagination
    _LIST_URL = (
        "https://gb.go.kr/Sub/open_contents/section/datastat/page.do"
        "?mnu_uid=7866&BD_CODE=gb_statboard&cmd=1"
        "&key=0&word=&p1=0&p2=0&period=1&B_START=2000-01-01&B_END=2030-12-31"
    )
    _PUBLISHER = "경상북도"
    _DEPARTMENT = "빅데이터과"
    _CATEGORY = "통계소식"

    MAX_PAGES = 200
    _MAX_RUNTIME_S = 25 * 60
    _RUNTIME_GRACE_S = 30
    PAGE_SIZE = 10

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page_idx = 0          # Start offset = page_idx * PAGE_SIZE
        seen_urls: set[str] = set()
        limit_label = limit if limit is not None else "inf"
        t0 = time.monotonic()

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        while page_idx < self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if self._budget_exhausted(t0):
                print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                break

            start_offset = page_idx * self.PAGE_SIZE
            if page_idx % 10 == 0:
                print(f"[{self.site_id}] page {page_idx + 1}: saved {saved}/{limit_label}")

            list_url = f"{self._LIST_URL}&Start={start_offset}"
            raw = self._curl_get(list_url, context=f"list page {page_idx + 1}")
            if not raw:
                print(f"[{self.site_id}] Empty response for list page {page_idx + 1}; stopping")
                break

            items = self._parse_list_items(raw)
            if not items:
                print(f"[{self.site_id}] No items on list page {page_idx + 1}; stopping")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            for it in new_items:
                seen_urls.add(it["url"])

            if not new_items:
                print(f"[{self.site_id}] page {page_idx + 1}: all items already seen; stopping")
                break

            for it in new_items:
                if limit is not None and saved >= limit:
                    break
                if self._budget_exhausted(t0):
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                pnum = it.get("post_number", "?")
                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        it["url"],
                        context=f"detail V_NUM={pnum}",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(it, detail_raw)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] V_NUM={pnum} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item V_NUM={pnum} failed: {exc}")
                    continue

            page_idx += 1

        if page_idx >= self.MAX_PAGES:
            print(f"[{self.site_id}] Safety cap reached at {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_items(self, raw: str) -> list[dict]:
        soup = self._make_soup(raw, context="list page")
        items = []

        for row in soup.select("table.bbsList tbody tr"):
            number_td = row.select_one("td.b_number")
            subject_td = row.select_one("td.b_subject")
            file_td = row.select_one("td.b_file")
            date_td = row.select_one("td.b_date")

            if not subject_td:
                continue
            a_tag = subject_td.select_one("a[href]")
            if not a_tag:
                continue

            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            # Resolve relative URL
            if href.startswith("./"):
                href = href[2:]
            if not href.startswith("http"):
                href = (
                    f"{self.base_url}/Sub/open_contents/section/datastat/{href}"
                )

            post_number = None
            if number_td:
                txt = number_td.get_text(strip=True)
                if txt.isdigit():
                    post_number = txt

            listed_date = None
            if date_td:
                dt = date_td.get_text(strip=True)
                m = re.match(r"^(\d{2})-(\d{2})-(\d{2})$", dt)
                if m:
                    yy, mm, dd = m.groups()
                    listed_date = f"20{yy}-{mm}-{dd}"

            # File links from the list row
            pdf_url = None
            original_filename = None
            all_files: list[dict] = []
            if file_td:
                for file_a in file_td.select("a[href]"):
                    fhref = file_a.get("href", "")
                    if not fhref.startswith("http"):
                        fhref = f"{self.base_url}{fhref}"
                    img = file_a.select_one("img[alt]")
                    fname = img["alt"].strip() if img else file_a.get("title", "").strip()
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                    all_files.append({"url": fhref, "name": fname, "ext": ext})

                # Prefer PDF; fall back to first available file
                for f in all_files:
                    if f["ext"] == "pdf":
                        pdf_url = f["url"]
                        original_filename = f["name"]
                        break
                if not pdf_url and all_files:
                    pdf_url = all_files[0]["url"]
                    original_filename = all_files[0]["name"]

            items.append({
                "url": href,
                "title": title,
                "post_number": post_number,
                "listed_date": listed_date,
                "pdf_url": pdf_url,
                "original_filename": original_filename,
                "all_files": all_files,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page / paper assembly
    # ------------------------------------------------------------------

    def _build_paper(self, item: dict, detail_raw: str) -> dict[str, Any]:
        soup = self._make_soup(detail_raw, context=f"detail {item.get('post_number', '?')}")
        title = item["title"]

        # Exact published date from 등록일 field
        published_date = item.get("listed_date")
        for li in soup.select("li"):
            l_span = li.select_one("span.span_l")
            r_span = li.select_one("span.span_r")
            if l_span and r_span and "등록일" in l_span.get_text():
                m = re.match(r"(\d{4}-\d{2}-\d{2})", r_span.get_text(strip=True))
                if m:
                    published_date = m.group(1)
                break

        # Author from 작성자 field
        author = ""
        for li in soup.select("li"):
            l_span = li.select_one("span.span_l")
            r_span = li.select_one("span.span_r")
            if l_span and r_span and "작성자" in l_span.get_text():
                raw_auth = r_span.get_text(strip=True)
                author = raw_auth.split("[")[0].split("☎")[0].strip()
                break

        # Content text from dl.content
        content_text = ""
        content_dl = soup.find("dl", class_="content")
        if content_dl:
            dd = content_dl.find("dd")
            if dd:
                content_text = re.sub(r"\s+", " ", dd.get_text(" ", strip=True)).strip()

        # Files from detail page's dl.attfile (overrides list-level file links)
        pdf_url = item.get("pdf_url")
        original_filename = item.get("original_filename")
        file_names: list[str] = [f["name"] for f in item.get("all_files", []) if f["name"]]

        attfile_dl = soup.find("dl", class_="attfile")
        if attfile_dl:
            detail_file_names: list[str] = []
            first_href = None
            first_fname = None
            pdf_href = None
            pdf_fname = None

            for a in attfile_dl.select("dd a[href]"):
                fhref = a.get("href", "")
                if not fhref.startswith("http"):
                    fhref = f"{self.base_url}{fhref}"
                fname = a.get_text(strip=True)
                if not fname:
                    continue
                detail_file_names.append(fname)
                if first_href is None:
                    first_href = fhref
                    first_fname = fname
                if fname.lower().endswith(".pdf") and pdf_href is None:
                    pdf_href = fhref
                    pdf_fname = fname

            # Prefer PDF from detail page
            if pdf_href:
                pdf_url = pdf_href
                original_filename = pdf_fname
            elif first_href:
                pdf_url = first_href
                original_filename = first_fname

            if detail_file_names:
                file_names = detail_file_names

        # ------------------------------------------------------------------
        # Abstract: content text + always-appended metadata description.
        # This guarantees >=100 chars even when the body is just the title.
        # ------------------------------------------------------------------
        abstract_parts: list[str] = []
        if content_text and len(content_text) >= 10:
            abstract_parts.append(content_text)

        meta_parts: list[str] = [
            "경상북도 통계포털 '통계소식' 게시물.",
            f"제목: {title}.",
        ]
        if item.get("post_number"):
            meta_parts.append(f"게시번호: {item['post_number']}.")
        if published_date:
            meta_parts.append(f"게시일: {published_date}.")
        if author:
            meta_parts.append(f"작성자: {author}.")
        if file_names:
            meta_parts.append(f"첨부파일: {'; '.join(file_names)}.")
        meta_parts.append(
            "경상북도 빅데이터과에서 관리하는 공개 통계 자료로, "
            "공공누리 제3유형(출처표시+변경금지) 조건에 따라 이용 가능합니다."
        )
        abstract_parts.append(" ".join(meta_parts))

        abstract = re.sub(r"\s+", " ", " ".join(abstract_parts)).strip()

        # external_id: V_NUM (post_number); fall back to B_NUM from URL
        external_id = item.get("post_number")
        if not external_id:
            m = re.search(r"[&?]B_NUM=(\d+)", item["url"])
            external_id = m.group(1) if m else item["url"]

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "url": item["url"],
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": item.get("listed_date"),
            "authors": author or None,
            "publisher": self._PUBLISHER,
            "department": self._DEPARTMENT,
            "pdf_url": pdf_url or None,
            "original_filename": original_filename or None,
            "keywords": None,
            "category": self._CATEGORY,
            "doi": None,
            "metadata": json.dumps(
                {
                    "posted_date": item.get("listed_date"),
                    "post_number": item.get("post_number"),
                    "board_code": "gb_statboard",
                    "file_names": file_names,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url: str,
        *,
        context: str = "request",
        referer: str | None = None,
        timeout: int = 45,
    ) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_err = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 5, check=False
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                last_err = (
                    result.stderr.decode("utf-8", errors="replace").strip()
                    or f"curl exit {result.returncode}"
                )
            except subprocess.TimeoutExpired as exc:
                last_err = f"timeout after {exc.timeout}s"
            except OSError as exc:
                last_err = str(exc)

            if attempt < 2:
                w = waits[attempt]
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt + 1}/3): {last_err}; retrying in {w}s"
                )
                time.sleep(w)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str, *, context: str = "html") -> BeautifulSoup:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed "
                    f"for {context}: {exc}"
                )
        return BeautifulSoup("", "html.parser")

    def _budget_exhausted(self, t0: float) -> bool:
        return (time.monotonic() - t0) >= (self._MAX_RUNTIME_S - self._RUNTIME_GRACE_S)
