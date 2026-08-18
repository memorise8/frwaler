# -*- coding: utf-8 -*-
"""Crawler for e-Stat stat-search dataset records.

The public page is Drupal-rendered, but its own JavaScript uses the JSON
endpoint below and returns rendered result HTML. We crawl the same endpoint
with ``layout=dataset`` so each record has a native ``stat_infid``, dates, and
file download links, then fetch the dataset detail page for richer metadata.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EStatGoJpStatSearchCrawler(BaseCrawler):
    site_id = "e-stat-go-jp-stat-search"
    site_name = "Custom: e-stat-go-jp-stat-search"
    base_url = "https://www.e-stat.go.jp"
    DELIVERY_ORDER = "arbitrary"

    _START_URL = "https://www.e-stat.go.jp/stat-search?page=1"
    _LIST_API = "https://www.e-stat.go.jp/retrieve/api_stat"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _ABSTRACT_MIN = 100

    # ------------------------------------------------------------------
    # Network and parser helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, accept: str = "*/*", head: bool = False) -> str | None:
        """Fetch a URL with curl, TLS 1.3 cap, retries, and lossy UTF-8 decode."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            "45",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
        ]
        if head:
            cmd.append("-I")
        cmd.append(url)

        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl empty/error "
                    f"(attempt {attempt + 1}/3) for {url}: {err or result.returncode}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl failed (attempt {attempt + 1}/3) for {url}: {exc}")

            if attempt < 2:
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] giving up after 3 network attempts: {url}")
        return None

    @staticmethod
    def _soup(raw: str | None):
        """Build BeautifulSoup with a robust parser fallback chain."""
        last_exc: Exception | None = None
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                continue
        print(f"[e-stat-go-jp-stat-search] BeautifulSoup parser fallback failed: {last_exc}")
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def _clean(text: Any) -> str:
        if text is None:
            return ""
        return re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()

    @staticmethod
    def _iso_date(text: str | None) -> str | None:
        if not text:
            return None
        match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        return None

    @staticmethod
    def _survey_month_date(text: str | None) -> str | None:
        if not text:
            return None
        match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-01"
        return None

    @staticmethod
    def _join_unique(values: list[str | None], sep: str = ", ") -> str | None:
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            clean = EStatGoJpStatSearchCrawler._clean(value)
            if clean and clean not in seen:
                seen.add(clean)
                out.append(clean)
        return sep.join(out) if out else None

    def _list_api_url(self, page: int) -> str:
        query = urllib.parse.urlencode(
            {
                "page": str(page),
                "layout": "dataset",
                "metadata": "1",
                "data": "1",
            }
        )
        return f"{self._LIST_API}?{query}"

    def _fetch_list_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        api_url = self._list_api_url(page)
        raw = self._curl(api_url, accept="application/json")
        if not raw:
            return [], False

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON parse failed on page {page}: {exc}")
            return [], False

        items_html = payload.get("items") or ""
        if not items_html:
            return [], False

        soup = self._soup(items_html)
        rows = [self._parse_list_article(article, page, api_url) for article in soup.select("article.stat-resource_list-item")]
        rows = [row for row in rows if row and row.get("url")]

        has_next = soup.select_one(".stat-paginate-next:not(.__current)") is not None
        return rows, has_next

    def _parse_list_article(self, article, page: int, api_url: str) -> dict[str, Any] | None:
        link = article.select_one("a.stat-item2_parent, a.js-data")
        if link is None:
            return None

        href = link.get("href") or ""
        detail_url = urljoin(self.base_url, href)
        stat_infid = self._clean(link.get("data-value")) or self._stat_infid_from_url(detail_url)
        title_text = self._clean(link.get_text(" ", strip=True))

        li_texts = [
            self._clean(li.get_text(" ", strip=True))
            for li in article.select("li.stat-resource_list-detail-item")
        ]
        li_texts = [text for text in li_texts if text]

        survey_name = li_texts[0] if li_texts else ""
        series = li_texts[1] if len(li_texts) > 1 else ""
        survey_month_raw = self._after_label(self._find_text(li_texts, "調査年月"), "調査年月")
        listed_raw = self._after_label(self._find_text(li_texts, "公開（更新）日"), "公開（更新）日")

        downloads = self._extract_downloads(article)
        file_types = [d.get("file_type") for d in downloads]

        return {
            "url": detail_url,
            "stat_infid": stat_infid,
            "post_number": stat_infid,
            "title": title_text or survey_name or stat_infid,
            "survey_name": survey_name,
            "series": series,
            "survey_month_raw": survey_month_raw,
            "survey_month_date": self._survey_month_date(survey_month_raw),
            "listed_date_raw": listed_raw,
            "listed_date": self._iso_date(listed_raw),
            "downloads": downloads,
            "file_types": file_types,
            "list_page": page,
            "list_api_url": api_url,
            "raw_list_text": li_texts,
        }

    @staticmethod
    def _find_text(values: list[str], needle: str) -> str:
        for value in values:
            if needle in value:
                return value
        return ""

    @staticmethod
    def _after_label(text: str, label: str) -> str:
        if not text:
            return ""
        return re.sub(rf"^\s*{re.escape(label)}\s*", "", text).strip()

    @staticmethod
    def _stat_infid_from_url(url: str) -> str | None:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        value = params.get("stat_infid") or params.get("statInfId")
        return value[0] if value else None

    def _extract_downloads(self, root) -> list[dict[str, Any]]:
        downloads: list[dict[str, Any]] = []
        for anchor in root.select("a.js-dl[href]"):
            href = anchor.get("href") or ""
            url = urljoin(self.base_url, href)
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            file_type = self._clean(anchor.get("data-file_type") or anchor.get_text(" ", strip=True))
            downloads.append(
                {
                    "url": url,
                    "file_type": file_type,
                    "file_id": self._clean(anchor.get("data-file_id")),
                    "release_count": self._clean(anchor.get("data-release_count")),
                    "statInfId": (params.get("statInfId") or [None])[0],
                    "fileKind": (params.get("fileKind") or [None])[0],
                }
            )
        return downloads

    def _parse_detail(self, detail_url: str, raw: str | None) -> dict[str, Any]:
        soup = self._soup(raw)
        fields: dict[str, str] = {}
        for row in soup.select("table.stat-resource_table tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            key = self._clean(th.get_text(" ", strip=True))
            value = self._clean(td.get_text(" ", strip=True))
            if key:
                fields[key] = value

        og_title = ""
        og = soup.select_one('meta[property="og:title"]')
        if og and og.get("content"):
            og_title = self._clean(og.get("content"))

        og_description = ""
        ogd = soup.select_one('meta[property="og:description"]')
        if ogd and ogd.get("content"):
            og_description = self._clean(ogd.get("content"))

        return {
            "url": detail_url,
            "fields": fields,
            "og_title": og_title,
            "og_description": og_description,
            "downloads": self._extract_downloads(soup),
        }

    def _pdf_info(self, downloads: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        pdf = None
        for download in downloads:
            if "PDF" in (download.get("file_type") or "").upper():
                pdf = download
                break
        if not pdf:
            return None, None

        pdf_url = pdf.get("url")
        filename = None
        if pdf_url:
            headers = self._curl(pdf_url, accept="application/pdf,*/*", head=True)
            filename = self._filename_from_headers(headers)

        if not filename and pdf_url:
            filename = self._filename_from_url(pdf_url)
        if not filename and pdf.get("statInfId"):
            filename = f"{pdf.get('statInfId')}.pdf"
        return pdf_url, filename

    @staticmethod
    def _filename_from_headers(headers: str | None) -> str | None:
        if not headers:
            return None
        match = re.search(r"content-disposition:\s*([^\r\n]+)", headers, re.IGNORECASE)
        if not match:
            return None
        value = match.group(1)
        star = re.search(r"filename\*\s*=\s*(?:[^']*)''([^;\r\n]+)", value, re.IGNORECASE)
        if star:
            return urllib.parse.unquote(star.group(1)).strip().strip('"') or None
        plain = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE)
        if plain:
            return plain.group(1).strip() or None
        plain = re.search(r"filename\s*=\s*([^;\r\n]+)", value, re.IGNORECASE)
        if plain:
            return plain.group(1).strip().strip('"') or None
        return None

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urllib.parse.urlparse(url)
        tail = parsed.path.rstrip("/").split("/")[-1]
        if "." in tail and len(tail) <= 200:
            return urllib.parse.unquote(tail)
        return None

    def _build_record(self, list_item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any] | None:
        fields = detail.get("fields") or {}
        stat_infid = list_item.get("stat_infid") or self._stat_infid_from_url(list_item.get("url", ""))
        if not stat_infid:
            return None

        downloads = detail.get("downloads") or list_item.get("downloads") or []
        pdf_url, original_filename = self._pdf_info(downloads)

        survey_name = fields.get("政府統計名") or list_item.get("survey_name")
        toukei_code = fields.get("政府統計コード")
        overview = fields.get("調査の概要")
        provided_name = fields.get("提供統計名")
        table_name = fields.get("統計表名")
        dataset_overview = fields.get("データセットの概要")
        category_l = fields.get("統計分野（大分類）")
        category_s = fields.get("統計分野（小分類）")
        publisher = fields.get("担当機関")
        department = fields.get("担当課室")
        survey_type = fields.get("統計の種類")
        survey_month_raw = fields.get("調査年月") or list_item.get("survey_month_raw")
        public_raw = fields.get("公開年月日時分") or list_item.get("listed_date_raw")
        cycle = fields.get("提供周期")
        area = fields.get("集計地域区分")

        listed_date = self._iso_date(public_raw) or list_item.get("listed_date")
        published_date = listed_date or self._survey_month_date(survey_month_raw)
        category = self._join_unique([category_l, category_s], " / ")
        file_types = self._join_unique([d.get("file_type") for d in downloads])

        title_parts = [survey_name, provided_name, table_name]
        title = self._join_unique(title_parts, " - ") or list_item.get("title") or stat_infid

        abstract = self._build_abstract(
            {
                "overview": overview,
                "dataset_overview": dataset_overview,
                "survey_name": survey_name,
                "provided_name": provided_name,
                "table_name": table_name or list_item.get("title"),
                "category": category,
                "publisher": publisher,
                "department": department,
                "survey_month_raw": survey_month_raw,
                "public_raw": public_raw,
                "cycle": cycle,
                "area": area,
                "survey_type": survey_type,
                "file_types": file_types,
                "og_description": detail.get("og_description"),
            }
        )

        if len(abstract) < self._ABSTRACT_MIN:
            print(f"[{self.site_id}] Skipping {stat_infid}: abstract too short ({len(abstract)} chars)")
            return None

        keywords = self._join_unique(
            [
                survey_name,
                provided_name,
                table_name,
                category_l,
                category_s,
                publisher,
                survey_type,
                cycle,
                area,
                file_types,
            ],
            ", ",
        )

        metadata = {
            "posted_date": public_raw or listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": provided_name or list_item.get("series"),
            "volume": None,
            "issue": None,
            "stat_infid": stat_infid,
            "statInfId": stat_infid,
            "toukei_code": toukei_code,
            "post_number": stat_infid,
            "listed_date": listed_date,
            "published_date": published_date,
            "survey_month_raw": survey_month_raw,
            "survey_month_date": self._survey_month_date(survey_month_raw),
            "public_datetime_raw": public_raw,
            "survey_name": survey_name,
            "provided_stat_name": provided_name,
            "table_name": table_name,
            "dataset_overview": dataset_overview,
            "category_large": category_l,
            "category_small": category_s,
            "publisher": publisher,
            "department": department,
            "survey_type": survey_type,
            "cycle": cycle,
            "collect_area": area,
            "file_types": file_types,
            "file_downloads": downloads,
            "api_endpoint": self._LIST_API,
            "list_api_url": list_item.get("list_api_url"),
            "detail_endpoint": list_item.get("url"),
            "list_page": list_item.get("list_page"),
            "list_record": list_item,
            "detail_fields": fields,
            "og_title": detail.get("og_title"),
        }

        return {
            "id": f"{self.site_id}:{stat_infid}",
            "site_id": self.site_id,
            "external_id": stat_infid,
            "post_number": stat_infid,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": list_item.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _build_abstract(self, values: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("overview", "dataset_overview"):
            value = self._clean(values.get(key))
            if value and value not in parts:
                parts.append(value)

        labels = [
            ("政府統計名", values.get("survey_name")),
            ("提供統計名", values.get("provided_name")),
            ("統計表名", values.get("table_name")),
            ("統計分野", values.get("category")),
            ("担当機関", values.get("publisher")),
            ("担当課室", values.get("department")),
            ("調査年月", values.get("survey_month_raw")),
            ("公開年月日時分", values.get("public_raw")),
            ("提供周期", values.get("cycle")),
            ("集計地域区分", values.get("area")),
            ("統計の種類", values.get("survey_type")),
            ("ファイル形式", values.get("file_types")),
        ]
        for label, value in labels:
            clean = self._clean(value)
            if clean:
                line = f"{label}: {clean}"
                if line not in parts:
                    parts.append(line)

        if not parts and values.get("og_description"):
            parts.append(self._clean(values.get("og_description")))
        return "\n".join(parts).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        page = (self.delivery_cursor or {}).get("page", 1)
        page_start = page
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "inf"

        print(f"[{self.site_id}] start: {self._START_URL}")

        while True:
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed >= self._WALL_BUDGET_SECS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break

            # _MAX_PAGES is a per-run chunk size (not an absolute ceiling) so a resume
            # from a large cursor still walks a full budget of pages this run.
            if page >= page_start + self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached this run")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            try:
                list_items, has_next = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} failed: {exc}")
                break

            if not list_items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for item in list_items:
                if limit is not None and saved >= limit:
                    break

                detail_url = item.get("url")
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    if self._delay:
                        time.sleep(self._delay)
                    detail_raw = self._curl(detail_url, accept="text/html,application/xhtml+xml,*/*")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item.get('stat_infid')} failed: empty detail")
                        continue
                    detail = self._parse_detail(detail_url, detail_raw)
                    paper = self._build_record(item, detail)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_label}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('stat_infid') or detail_url} failed: {exc}")
                    continue

            self._advance_cursor({"page": page + 1}, items_done=len(list_items))

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all URLs already seen; stopping")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page}: next page absent; stopping")
                break

            page += 1

        return saved
