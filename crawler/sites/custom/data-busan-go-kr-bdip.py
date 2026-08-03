# -*- coding: utf-8 -*-
"""부산광역시 빅데이터웨이브 공공데이터 포털 crawler.

List API  : POST /bdip/srh/getPublicDataListSearch.do  (JSON)
Detail URL: /bdip/opendata/detail.do?publicdatapk={pk}
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402  (path set above)


class BusanBdipCrawler(BaseCrawler):
    """공공데이터 포털 — 부산광역시 빅데이터웨이브 (data.busan.go.kr/bdip)."""

    site_id = "data-busan-go-kr-bdip"
    site_name = "Custom: data-busan-go-kr-bdip"
    base_url = "https://data.busan.go.kr"

    _LIST_URL = "https://data.busan.go.kr/bdip/srh/getPublicDataListSearch.do"
    _REFERER = "https://data.busan.go.kr/bdip/opendata/dataSet.do"
    _PAGE_SIZE = 50

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_post_json(self, url: str, body: dict) -> dict | None:
        """POST JSON body via curl; returns parsed JSON or None on any failure."""
        body_str = json.dumps(body, ensure_ascii=False)
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {self._REFERER}",
            "-d", body_str,
            url,
        ]
        delays = [1, 3, 9]
        for attempt, delay in enumerate(delays):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw and raw.strip():
                    try:
                        return json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError as exc:
                        print(f"[{self.site_id}] JSON decode error attempt {attempt+1}: {exc}")
                else:
                    print(f"[{self.site_id}] Empty response attempt {attempt+1}/{len(delays)}")
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout attempt {attempt+1}/{len(delays)}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt+1}/{len(delays)}: {exc}")

            if attempt < len(delays) - 1:
                print(f"[{self.site_id}] Retrying in {delay}s...")
                time.sleep(delay)

        print(f"[{self.site_id}] All retries exhausted for {url}")
        return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Convert 'YYYYMMDD[...]' → 'YYYY-MM-DD'; return '' on bad input."""
        if not raw:
            return ""
        digits = re.sub(r"[^\d]", "", raw)[:8]
        if len(digits) < 8:
            return ""
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags, decode common entities, collapse whitespace."""
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl list pages; persist each dataset entry as a document."""
        saved = 0
        page = 0
        seen_pks: set = set()
        max_pages = 200
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Hard wall-clock budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached. Stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= max_pages:
                print(f"[{self.site_id}] Safety cap of {max_pages} pages reached. Stopping.")
                break

            offset = page * self._PAGE_SIZE
            body = {
                "searchSort": "REG_DT",
                "dataTy": "",
                "brmnCd": "",
                "insttCd": "",
                "offset": offset,
                "pagelength": self._PAGE_SIZE,
                "listOrderCd": "",
            }

            data = self._curl_post_json(self._LIST_URL, body)
            if not data:
                print(f"[{self.site_id}] Failed to fetch list at page {page}. Stopping.")
                break

            try:
                rows = data["result"]["rows"]
                total = data["result"]["total_count"]
            except (KeyError, TypeError) as exc:
                print(f"[{self.site_id}] Unexpected response at page {page}: {exc}. Stopping.")
                break

            if not rows:
                print(f"[{self.site_id}] No more items at page {page}. Done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str} (server total: {total})")

            new_on_page = 0
            for row in rows:
                if limit is not None and saved >= limit:
                    break

                pk = None
                try:
                    fields = row.get("fields") or {}
                    pk = str(fields.get("PUBLICDATAPK") or "").strip()
                    if not pk:
                        continue

                    # URL-based deduplication
                    if pk in seen_pks:
                        continue
                    seen_pks.add(pk)
                    new_on_page += 1

                    title = (fields.get("PUBLICDATASJ") or "").strip()
                    if not title:
                        title = f"공공데이터_{pk}"

                    abstract = self._strip_html(fields.get("PUBLICDATADC") or "")
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] item {pk} abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue

                    category = (fields.get("BRMNNM") or "").strip()
                    publisher = (fields.get("INSTTNM") or "").strip()

                    # List keywords are space-separated words; normalise to comma-separated
                    kw_raw = (fields.get("KEYWORD") or "").strip()
                    keywords = ", ".join(k for k in kw_raw.split() if k) if kw_raw else ""

                    registdt = (fields.get("REGISTDT") or "").strip()
                    updtdt = (fields.get("UPDTDT") or "").strip()
                    dataty = (fields.get("DATATY") or "").strip()
                    brmncd = (fields.get("BRMNCD") or "").strip()
                    insttcode = (fields.get("INSTTCODE") or "").strip()

                    published_date = self._parse_date(registdt)
                    detail_url = f"{self.base_url}/bdip/opendata/detail.do?publicdatapk={pk}"

                    paper = {
                        "site_id": self.site_id,
                        "external_id": pk,
                        "post_number": pk,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": published_date,   # → listed_date in libertree path
                        "authors": None,
                        "publisher": publisher,
                        "department": None,
                        "journal": None,
                        "category": category,
                        "keywords": keywords,
                        "url": detail_url,
                        "pdf_url": None,
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": registdt,
                                "updtdt": updtdt,
                                "dataty": dataty,
                                "publicdatapk": pk,
                                "brmncd": brmncd,
                                "insttcode": insttcode,
                                "category": category,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pk or '?'} failed: {exc}")
                    continue

            # If every record on this page was already seen, pagination has looped back
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page} (all duplicates). Done.")
                break

            page += 1
            time.sleep(1.0)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
