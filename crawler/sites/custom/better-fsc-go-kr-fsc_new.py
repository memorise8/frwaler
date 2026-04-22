# -*- coding: utf-8 -*-
"""Crawler for FSC better portal recent reply cases."""

import hashlib
import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BetterFscGoKrFscNewCrawler(BaseCrawler):
    site_id = "better-fsc-go-kr-fsc_new"
    site_name = "Custom: better-fsc-go-kr-fsc_new"
    base_url = "https://better.fsc.go.kr"

    _LIST_URL = "https://better.fsc.go.kr/fsc_new/replyCase/selectReplyCasePastReplyList.do"
    _LAWREQ_DETAIL_URL = "https://better.fsc.go.kr/fsc_new/replyCase/LawreqDetail.do"
    _OPINION_DETAIL_URL = "https://better.fsc.go.kr/fsc_new/replyCase/OpinionDetail.do"
    _LIST_REFERER = (
        "https://better.fsc.go.kr/fsc_new/replyCase/"
        "PastReplyList.do?stNo=11&muNo=171&muGpNo=75"
    )
    _PAGE_SIZE = 50

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    def _curl(self, url, data=None, referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-sk",
            "-L",
            "--max-time",
            "30",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if data is not None:
            cmd.extend(
                [
                    "-X",
                    "POST",
                    "-H",
                    "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                    "--data",
                    urlencode(data),
                ]
            )
        cmd.append(url)

        wait_times = [1, 3, 9]
        for attempt, wait in enumerate(wait_times, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=35,
                    check=False,
                )
            except Exception as exc:
                if attempt == len(wait_times):
                    print(f"[{self.site_id}] curl failed for {url}: {exc}")
                    return None
                print(
                    f"[{self.site_id}] curl error {attempt}/{len(wait_times)} for {url}: {exc}"
                )
                time.sleep(wait)
                continue

            raw = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and raw.strip():
                return raw

            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            if attempt == len(wait_times):
                print(
                    f"[{self.site_id}] curl failed for {url}: "
                    f"exit={result.returncode} stderr={stderr}"
                )
                return None

            print(
                f"[{self.site_id}] empty/failed response {attempt}/{len(wait_times)} "
                f"for {url}: exit={result.returncode} stderr={stderr}"
            )
            time.sleep(wait)
        return None

    def _parse_html(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean_text(value):
        if not value:
            return ""
        text = unescape(value)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _fetch_list_page(self, start):
        raw = self._curl(
            self._LIST_URL,
            data={
                "draw": 1,
                "start": start,
                "length": self._PAGE_SIZE,
                "searchKeyword": "",
                "searchCondition": "",
                "searchReplyRegDateStart": "",
                "searchReplyRegDateEnd": "",
                "searchType": "",
                "searchCategory": "",
                "searchLawType": "",
            },
            referer=self._LIST_REFERER,
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] list JSON decode failed at start={start}: {exc}")
            return None

    def _fetch_detail_html(self, item):
        gubun = self._clean_text(item.get("gubun"))
        idx = item.get("idx")
        if not idx:
            return None, None, None

        if gubun == "법령해석":
            url = self._LAWREQ_DETAIL_URL
            data = {"muNo": 171, "stNo": 11, "lawreqIdx": idx, "actCd": "R"}
            public_url = (
                f"{self._LAWREQ_DETAIL_URL}?stNo=11&muNo=171&muGpNo=75&lawreqIdx={idx}"
            )
        else:
            url = self._OPINION_DETAIL_URL
            data = {"muNo": 171, "stNo": 11, "opinionIdx": idx, "actCd": "R"}
            public_url = (
                f"{self._OPINION_DETAIL_URL}?stNo=11&muNo=171&muGpNo=75&opinionIdx={idx}"
            )

        raw = self._curl(url, data=data, referer=self._LIST_REFERER)
        return raw, url, public_url

    def _extract_label_value(self, table, label):
        if table is None:
            return ""
        for row in table.find_all("tr"):
            header = row.find("th")
            if header is None:
                continue
            if self._clean_text(header.get_text(" ", strip=True)) != label:
                continue
            cell = row.find("td")
            if cell is None:
                return ""
            return self._clean_text(cell.get_text(" ", strip=True))
        return ""

    def _extract_section_html(self, table, label):
        if table is None:
            return ""
        for row in table.find_all("tr"):
            header = row.find("th")
            if header is None:
                continue
            if self._clean_text(header.get_text(" ", strip=True)) != label:
                continue
            cell = row.find("td")
            if cell is None:
                return ""
            return cell.decode_contents()
        return ""

    def _html_to_text(self, html_fragment):
        if not html_fragment:
            return ""
        soup = self._parse_html(f"<div>{html_fragment}</div>")
        if soup is None:
            return self._clean_text(re.sub(r"<[^>]+>", " ", html_fragment))
        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{2,}", "\n", text)
        return self._clean_text(text.replace("\n", " "))

    def _parse_detail(self, raw, item, page_url):
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        top_table = soup.select_one(".sub-con .board-view .tbl-view")
        detail_table = soup.select_one(".sub-con .res-wrap .tbl-write")
        if top_table is None or detail_table is None:
            raise ValueError("detail tables not found")

        title = self._clean_text(item.get("title"))
        detail_title_cell = detail_table.select_one("td.subject")
        detail_title = self._clean_text(
            detail_title_cell.get_text(" ", strip=True) if detail_title_cell else ""
        )
        if detail_title and len(detail_title) >= len(title):
            title = detail_title

        department = self._extract_label_value(top_table, "소관부서")
        published_date = self._extract_label_value(detail_table, "회신일")
        if not published_date:
            published_date = self._clean_text(item.get("regDate")).replace(".", "-")

        query_summary = self._html_to_text(self._extract_section_html(detail_table, "질의요지"))
        answer = self._html_to_text(self._extract_section_html(detail_table, "회답"))
        reason = self._html_to_text(self._extract_section_html(detail_table, "이유"))

        abstract_parts = []
        if query_summary:
            abstract_parts.append(f"질의요지: {query_summary}")
        if answer:
            abstract_parts.append(f"회답: {answer}")
        if reason:
            abstract_parts.append(f"이유: {reason}")
        abstract = "\n\n".join(abstract_parts).strip()

        attach_link = detail_table.select_one("a[href*='/file/displayFile.do']")
        pdf_url = ""
        if attach_link and attach_link.get("href"):
            href = attach_link["href"].strip()
            if href.startswith("http://") or href.startswith("https://"):
                pdf_url = href
            else:
                pdf_url = f"{self.base_url}{href}"

        metadata = {
            "gubun": self._clean_text(item.get("gubun")),
            "category": self._clean_text(item.get("category")),
            "number": self._clean_text(item.get("number")),
            "idx": item.get("idx"),
            "reply_title": detail_title,
            "query_summary": query_summary,
            "answer": answer,
            "reason": reason,
            "detail_url": page_url,
        }

        external_id = f"{item.get('gubun', '')}:{item.get('idx', '')}:{item.get('number', '')}"
        stable_id = hashlib.sha1(
            f"{self.site_id}:{external_id}".encode("utf-8", errors="replace")
        ).hexdigest()

        return {
            "id": stable_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": self._clean_text(item.get("category")) or self._clean_text(item.get("gubun")),
            "keywords": json.dumps(
                [
                    value
                    for value in [
                        self._clean_text(item.get("gubun")),
                        self._clean_text(item.get("category")),
                    ]
                    if value
                ],
                ensure_ascii=False,
            ),
            "published_date": published_date,
            "url": page_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        start = 0

        while True:
            if limit is not None and saved >= limit:
                break

            page = self._fetch_list_page(start)
            if not page:
                print(f"[{self.site_id}] skipping list page at start={start}")
                start += self._PAGE_SIZE
                continue

            items = page.get("data") or []
            if not items:
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    raw, _detail_post_url, public_url = self._fetch_detail_html(item)
                    if not raw:
                        print(
                            f"[{self.site_id}] item {item.get('idx')} failed: empty detail response"
                        )
                        continue

                    paper = self._parse_detail(raw, item, public_url)
                    if len(paper["abstract"]) < 50:
                        print(
                            f"[{self.site_id}] item {item.get('idx')} skipped: "
                            f"abstract too short ({len(paper['abstract'])})"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[better-fsc-go-kr-fsc_new] item {item.get('idx')} failed: {exc}")
                    continue

                time.sleep(self.detail_delay)

            if len(items) < self._PAGE_SIZE:
                break
            start += self._PAGE_SIZE

        return saved
