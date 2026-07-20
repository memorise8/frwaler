# -*- coding: utf-8 -*-
"""Crawler for 대구광역시 뉴스룸 보도자료 (info.daegu.go.kr/newshome, mkey=26)."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "info-daegu-go-kr-newshome"
_BASE_URL = "https://info.daegu.go.kr"
_LIST_PATH = "/newshome/mtnmain.php"
_DETAIL_PATH = "/newshome/mtnmain.php"
_FILE_DOWNLOAD_PATH = "/enewspaper/file_download.php"
_MKEY = "26"
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 50
_WALL_CLOCK_BUDGET_SECONDS = 25 * 60


class InfoDaeguGoKrNewshomeCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: info-daegu-go-kr-newshome"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_list_page(self, page_no):
        params = {
            "mtnkey": "scatelist",
            "mkey": _MKEY,
            "mkey2": "",
            "stext": "",
            "stext2": "",
            "sel_year": "",
            "bpage": page_no,
        }
        url = f"{_BASE_URL}{_LIST_PATH}?{urlencode(params)}"
        return self._curl(url, referer=f"{_BASE_URL}{_LIST_PATH}?mtnkey=scatelist&mkey={_MKEY}")

    def _fetch_detail(self, aid, mkey2, bpage):
        params = {
            "mtnkey": "articleview",
            "mkey": "scatelist",
            "mkey2": mkey2 or "1",
            "aid": aid,
            "bpage": bpage or "1",
            "stext": "",
        }
        url = f"{_BASE_URL}{_DETAIL_PATH}?{urlencode(params)}"
        return self._curl(url, referer=f"{_BASE_URL}{_LIST_PATH}?mtnkey=scatelist&mkey={_MKEY}")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _one_line(value) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _to_iso_date(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        m = re.match(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$", raw)
        if m:
            y, mo, d = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            return []

        table = soup.select_one("#table_article table")
        if table is None:
            return []

        items = []
        for tr in table.find_all("tr"):
            try:
                if tr.find("th") is not None:
                    continue
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue

                post_number = self._one_line(tds[0].get_text(strip=True)) or None

                subject_td = tds[1]
                a = subject_td.find("a", href=True)
                if a is None:
                    continue
                href = a["href"]

                m_aid = re.search(r"[?&]aid=(\d+)", href)
                if not m_aid:
                    continue
                aid = m_aid.group(1)
                m_mkey2 = re.search(r"[?&]mkey2=(\d+)", href)
                mkey2 = m_mkey2.group(1) if m_mkey2 else "1"
                m_bpage = re.search(r"[?&]bpage=(\d+)", href)
                bpage = m_bpage.group(1) if m_bpage else "1"

                title = self._one_line(a.get_text(" ", strip=True))
                if not title:
                    continue

                file_td = tds[2]
                file_link = file_td.find("a", href=re.compile(r"^javascript:filedownload"))
                attachment = None
                if file_link is not None:
                    href_js = file_link.get("href", "")
                    m_file = re.search(
                        r"filedownload\(\s*'([^']*)'\s*,\s*'([^']*)'", href_js
                    )
                    if m_file:
                        fname, faid = m_file.groups()
                        attachment = {
                            "filename": fname,
                            "url": (
                                f"{_BASE_URL}{_FILE_DOWNLOAD_PATH}?filename="
                                f"{quote(fname)}&faid={faid}&in_cnt=1"
                            ),
                        }

                listed_raw = self._one_line(tds[3].get_text(strip=True))

                items.append({
                    "aid": aid,
                    "mkey2": mkey2,
                    "bpage": bpage,
                    "title": title,
                    "post_number": post_number,
                    "listed_date_raw": listed_raw,
                    "attachment": attachment,
                    "url": urljoin(
                        _BASE_URL,
                        f"{_DETAIL_PATH}?mtnkey=articleview&mkey=scatelist&mkey2={mkey2}"
                        f"&aid={aid}&bpage={bpage}&stext=",
                    ),
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list row parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed on detail page")

        article = soup.select_one("#article")
        if article is None:
            raise ValueError("detail page missing #article")

        title_div = article.select_one(".title")
        title = self._one_line(title_div.get_text(" ", strip=True)) if title_div else ""

        stitle_div = article.select_one(".stitle")
        subtitle = self._one_line(stitle_div.get_text(" ", strip=True)) if stitle_div else ""

        content_div = article.select_one(".article_view_content")
        abstract = self._one_line(content_div.get_text(" ", strip=True)) if content_div else ""
        if subtitle:
            abstract = f"{subtitle} {abstract}".strip()

        category = None
        published_raw = None
        info_div = article.select_one(".info_wrap .info")
        if info_div is not None:
            section_div = info_div.select_one(".section")
            if section_div is not None:
                category = self._one_line(section_div.get_text(strip=True)) or None
            date_div = info_div.select_one(".date")
            if date_div is not None:
                published_raw = self._one_line(date_div.get_text(strip=True))

        department = None
        contact_person = None
        dept_dl = article.select_one(".info_wrap .dept dl")
        if dept_dl is not None:
            dts = dept_dl.find_all("dt")
            dds = dept_dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                label = self._one_line(dt.get_text(strip=True))
                value = self._one_line(dd.get_text(strip=True))
                if label == "담당부서":
                    department = value or None
                elif label == "담당자":
                    contact_person = value or None

        attachments = []
        files_div = article.select_one("#files")
        if files_div is not None:
            for link in files_div.select("a[href*='filedownload']"):
                href_js = link.get("href", "")
                m_file = re.search(r"filedownload\(\s*'([^']*)'\s*,\s*'([^']*)'", href_js)
                if not m_file:
                    continue
                fname, faid = m_file.groups()
                url = (
                    f"{_BASE_URL}{_FILE_DOWNLOAD_PATH}?filename="
                    f"{quote(fname)}&faid={faid}&in_cnt=1"
                )
                if not any(a["url"] == url for a in attachments):
                    attachments.append({"filename": fname, "url": url})

        return {
            "title": title,
            "subtitle": subtitle,
            "abstract": abstract,
            "category": category,
            "published_raw": published_raw,
            "department": department,
            "contact_person": contact_person,
            "attachments": attachments,
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_label = str(limit) if limit is not None else "inf"
        crawl_start = time.time()

        page = 1
        while True:
            elapsed = time.time() - crawl_start
            if elapsed > _WALL_CLOCK_BUDGET_SECONDS:
                print(f"[{_SITE_ID}] {_WALL_CLOCK_BUDGET_SECONDS}s wall-clock budget reached; stopping at page {page}")
                break

            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            try:
                raw_list = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] failed to fetch list page {page}: {exc}")
                break

            if not raw_list:
                print(f"[{_SITE_ID}] empty list response at page {page}; stopping")
                break

            items = self._parse_list_page(raw_list)
            if not items:
                print(f"[{_SITE_ID}] no rows on page {page}; stopping")
                break

            new_urls = [it["url"] for it in items if it["url"] not in seen_urls]
            if not new_urls:
                print(f"[{_SITE_ID}] all URLs on page {page} already seen; stopping (paginator looped)")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                aid = item["aid"]

                try:
                    time.sleep(self._detail_delay)
                    raw_detail = self._fetch_detail(aid, item["mkey2"], item["bpage"])
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {aid} failed: empty detail response")
                        continue

                    detail = self._parse_detail(raw_detail)
                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{_SITE_ID}] item {aid} skipped: abstract too short ({len(abstract)} chars)")
                        continue

                    title = detail.get("title") or item["title"]
                    department = detail.get("department")
                    category = detail.get("category")

                    listed_iso = self._to_iso_date(item.get("listed_date_raw"))
                    published_iso = self._to_iso_date(detail.get("published_raw")) or listed_iso

                    attachments = detail.get("attachments") or []
                    if not attachments and item.get("attachment"):
                        attachments = [item["attachment"]]
                    pdf_url = attachments[0]["url"] if attachments else None
                    original_filename = attachments[0]["filename"] if attachments else None

                    metadata = {
                        "posted_date": item.get("listed_date_raw"),
                        "originalFilename": original_filename,
                        "aid": aid,
                        "mkey": _MKEY,
                        "mkey2": item.get("mkey2"),
                        "subtitle": detail.get("subtitle"),
                        "department": department,
                        "contact_person": detail.get("contact_person"),
                        "attachments": attachments,
                    }

                    paper = {
                        "id": str(uuid.uuid4()),
                        "site_id": _SITE_ID,
                        "external_id": aid,
                        "post_number": item.get("post_number"),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_iso,
                        "posted_date": listed_iso,
                        "authors": None,
                        "publisher": "대구광역시",
                        "department": department,
                        "journal": None,
                        "url": url,
                        "pdf_url": pdf_url,
                        "keywords": None,
                        "category": category,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {aid} failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
