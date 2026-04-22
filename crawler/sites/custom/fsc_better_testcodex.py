# -*- coding: utf-8 -*-
"""Crawler for FSC Better Regulation past reply cases."""

import html
import json
import re
import subprocess
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class FSCBetterTestCodexCrawler(BaseCrawler):
    """Crawl past FSC Better Regulation reply cases."""

    site_id = "fsc-better-testcodex"
    site_name = "금융위 Better Regulation (테스트)"
    base_url = "https://better.fsc.go.kr"

    _LIST_URL = f"{base_url}/fsc_new/replyCase/selectReplyCasePastReplyList.do"
    _LIST_REFERER = (
        f"{base_url}/fsc_new/replyCase/PastReplyList.do?stNo=11&muNo=171&muGpNo=75"
    )
    _PAGE_SIZE = 10

    def _curl(self, url, *, data=None, referer=None):
        """Fetch via curl to tolerate the site's TLS quirks."""
        cmd = [
            "curl",
            "-skL",
            "--tls-max",
            "1.3",
            "--max-time",
            "40",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if data is not None:
            cmd.extend(
                [
                    "-X",
                    "POST",
                    "-H",
                    "X-Requested-With: XMLHttpRequest",
                    "-H",
                    "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                    "--data",
                    data,
                ]
            )
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
            except Exception as exc:
                if attempt == 2:
                    print(f"[{self.site_id}] curl failed for {url}: {exc}")
                    return None
                wait = (attempt + 1) * 3
                print(f"[{self.site_id}] curl error, retrying in {wait}s: {exc}")
                time.sleep(wait)
                continue

            body = result.stdout.strip()
            if result.returncode == 0 and body:
                return body

            if attempt == 2:
                stderr = (result.stderr or "").strip()
                print(
                    f"[{self.site_id}] curl returned no usable body for {url} "
                    f"(code={result.returncode} stderr={stderr})"
                )
                return None

            wait = (attempt + 1) * 3
            print(f"[{self.site_id}] empty response, retrying in {wait}s for {url}")
            time.sleep(wait)

        return None

    def _fetch_list_page(self, start):
        payload = (
            f"draw=1&start={start}&length={self._PAGE_SIZE}"
            "&searchKeyword=&searchCondition=&searchReplyRegDateStart="
            "&searchReplyRegDateEnd=&searchType=&searchCategory=&searchLawType="
        )
        raw = self._curl(self._LIST_URL, data=payload, referer=self._LIST_REFERER)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] list JSON decode failed at start={start}: {exc}")
            return None

    @staticmethod
    def _normalize_text(value):
        text = html.unescape(value or "")
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _parse_date(raw):
        raw = (raw or "").strip()
        if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", raw):
            return raw.replace(".", "-")
        return raw

    @staticmethod
    def _detail_url(item):
        idx = item["idx"]
        gubun = (item.get("gubun") or "").strip()
        if gubun == "법령해석":
            return (
                f"https://better.fsc.go.kr/fsc_new/replyCase/LawreqDetail.do"
                f"?stNo=11&muNo=171&muGpNo=75&lawreqIdx={idx}&actCd=R"
            )
        return (
            f"https://better.fsc.go.kr/fsc_new/replyCase/OpinionDetail.do"
            f"?stNo=11&muNo=171&muGpNo=75&opinionIdx={idx}&actCd=R"
        )

    def _extract_detail(self, item):
        url = self._detail_url(item)
        html_text = self._curl(url, referer=self._LIST_REFERER)
        if not html_text:
            return None

        soup = BeautifulSoup(html_text, "html.parser")
        title_node = soup.select_one(".tbl-view.two td.subject") or soup.select_one(
            ".tbl-write td.subject"
        )
        title = self._normalize_text(title_node.get_text(" ", strip=True) if title_node else "")
        if not title:
            title = self._normalize_text(item.get("title"))

        sections = []
        for row in soup.select(".res-wrap table.tbl-write tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            label = self._normalize_text(th.get_text(" ", strip=True))
            if label not in {"질의요지", "회답", "이유"}:
                continue
            body = self._normalize_text(td.get_text("\n", strip=True))
            if body:
                sections.append(f"{label}: {body}")

        abstract = "\n\n".join(sections)
        if len(abstract) < 100:
            fallback = self._normalize_text(
                soup.select_one(".res-wrap").get_text("\n", strip=True)
                if soup.select_one(".res-wrap")
                else ""
            )
            if len(fallback) > len(abstract):
                abstract = fallback

        if len(abstract) < 100:
            print(
                f"[{self.site_id}] extracted abstract too short for idx={item.get('idx')}: "
                f"{len(abstract)} chars"
            )
            return None

        return {
            "title": title,
            "abstract": abstract,
            "url": url,
        }

    def crawl(self, limit=None):
        saved = 0
        start = 0

        while True:
            if limit is not None and saved >= limit:
                break

            page = self._fetch_list_page(start)
            if not page:
                break

            items = page.get("data") or []
            if not items:
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                detail = self._extract_detail(item)
                if not detail:
                    continue

                external_id = f"{item.get('gubun', '').strip()}-{item.get('idx')}"
                metadata = {
                    "number": item.get("number"),
                    "gubun": item.get("gubun"),
                    "category": item.get("category"),
                    "regDate": item.get("regDate"),
                    "idx": item.get("idx"),
                }

                self._save_paper(
                    {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": detail["title"],
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": detail["abstract"],
                        "category": self._normalize_text(item.get("category")),
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": self._parse_date(item.get("regDate")),
                        "url": detail["url"],
                        "pdf_url": "",
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }
                )
                saved += 1
                print(
                    f"[{self.site_id}] saved {saved}: {detail['title']} "
                    f"({len(detail['abstract'])} chars)"
                )

            total = page.get("recordsFiltered") or page.get("recordsTotal") or 0
            start += self._PAGE_SIZE
            if start >= total:
                break

        return saved
