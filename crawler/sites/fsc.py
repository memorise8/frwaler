# -*- coding: utf-8 -*-
"""금융위원회 법령해석 crawler (curl-based due to SSL issues)."""

import json
import subprocess
import time

from bs4 import BeautifulSoup
from ..base_crawler import BaseCrawler


class FSCCrawler(BaseCrawler):
    """Crawler for 금융위원회 법령해석포털."""

    site_id = "fsc"
    site_name = "금융위 법령해석"
    base_url = "https://better.fsc.go.kr"

    _LIST_URL = "https://better.fsc.go.kr/fsc_new/replyCase/selectReplyCaseLawreqList.do"
    _DETAIL_URL = "https://better.fsc.go.kr/fsc_new/replyCase/LawreqDetail.do"
    _REFERER = "https://better.fsc.go.kr/fsc_new/replyCase/LawreqList.do?stNo=11&muNo=85&muGpNo=75"
    _PAGE_SIZE = 25

    def _curl_request(self, url, method="GET", data=None):
        """Make HTTP request via curl (bypasses Python SSL issues)."""
        cmd = ["curl", "-sk", "--tlsv1.2", "--max-time", "30"]
        if method == "POST":
            cmd.extend(["-X", "POST"])
        cmd.extend([
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "-H", f"Referer: {self._REFERER}",
        ])
        if data:
            cmd.extend(["-d", data])
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.stdout:
                    return result.stdout
                if attempt < 2:
                    import time as _time
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] Empty response, retrying in {wait}s...")
                    _time.sleep(wait)
            except Exception as e:
                if attempt < 2:
                    import time as _time
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] curl error: {e}, retrying in {wait}s...")
                    _time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {e}")
        return None

    def crawl(self, limit=None):
        """Crawl 금융위 법령해석 via JSON API."""
        offset = 0
        total = None
        saved = 0

        while True:
            if limit is not None and saved >= limit:
                break

            time.sleep(self._delay)
            body = f"start={offset}&length={self._PAGE_SIZE}&stNo=11&muNo=85&muGpNo=75"
            response = self._curl_request(self._LIST_URL, method="POST", data=body)
            if not response:
                print(f"[{self.site_id}] Failed to fetch at offset {offset}. Stopping.")
                break

            try:
                data = json.loads(response)
            except json.JSONDecodeError:
                print(f"[{self.site_id}] Invalid JSON response. Stopping.")
                break

            if total is None:
                total = data.get("recordsTotal", 0)
                print(f"[{self.site_id}] Total items available: {total}")

            items = data.get("data", [])
            if not items:
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                lawreq_idx = item.get("lawreqIdx")
                title = item.get("title", "")
                category = item.get("category", "")
                lawreq_number = item.get("lawreqNumber", "")
                status = item.get("status", "")

                # Fetch detail page for abstract and attachments
                time.sleep(self._delay)
                abstract = ""
                pdf_url = ""
                published_date = ""
                detail_body = f"stNo=11&muNo=85&lawreqIdx={lawreq_idx}&actCd=R"
                detail_html = self._curl_request(self._DETAIL_URL, method="POST", data=detail_body)
                if detail_html:
                    detail_data = self._parse_detail(detail_html)
                    abstract = detail_data.get("abstract", "")
                    pdf_url = detail_data.get("pdf_url", "")
                    published_date = detail_data.get("published_date", "")

                view_url = f"https://better.fsc.go.kr/fsc_new/replyCase/LawreqDetail.do?stNo=11&muNo=85&lawreqIdx={lawreq_idx}&actCd=R"

                paper = {
                    "id": None,
                    "site_id": self.site_id,
                    "external_id": str(lawreq_idx),
                    "title": title,
                    "authors": json.dumps([], ensure_ascii=False),
                    "abstract": abstract,
                    "category": category,
                    "keywords": json.dumps([], ensure_ascii=False),
                    "published_date": published_date,
                    "url": view_url,
                    "pdf_url": pdf_url,
                    "doi": "",
                    "department": "",
                    "metadata": json.dumps({
                        "lawreqNumber": lawreq_number,
                        "status": status,
                    }, ensure_ascii=False),
                }
                self._save_paper(paper)
                saved += 1
                effective = f"{saved}/{limit}" if limit else str(saved)
                print(f"[{self.site_id}] Saved {effective} items...")

            offset += self._PAGE_SIZE
            if offset >= total:
                break

        print(f"[{self.site_id}] Done. Saved {saved} items.")
        return saved

    @staticmethod
    def _parse_detail(html):
        """Extract 질의요지, 회답, 첨부파일, 회신일 from detail page HTML."""
        import re
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return {"abstract": "", "pdf_url": "", "published_date": ""}
        text = soup.get_text(separator="\n", strip=True)

        # Extract text sections
        parts = []
        for label in ["질의요지", "회답", "이유"]:
            start = text.find(label)
            if start == -1:
                continue
            end = len(text)
            for next_label in ["회답", "이유", "첨부파일", "목록", "URL 복사"]:
                if next_label == label:
                    continue
                pos = text.find(next_label, start + len(label))
                if pos != -1 and pos < end:
                    end = pos
            section = text[start:end].strip()
            if section:
                parts.append(section)

        abstract = "\n\n".join(parts) if parts else ""

        # Extract file download link (hwp/hwpx/pdf)
        pdf_url = ""
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "displayFile.do" in href or "fileDown" in href.lower():
                if not href.startswith("http"):
                    href = "https://better.fsc.go.kr" + href
                pdf_url = href
                break

        # Extract 회신일
        published_date = ""
        date_match = re.search(r"회신일\s*(\d{4}[-./]\d{2}[-./]\d{2})", text)
        if date_match:
            published_date = date_match.group(1)

        return {"abstract": abstract, "pdf_url": pdf_url, "published_date": published_date}
