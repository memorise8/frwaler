# -*- coding: utf-8 -*-
"""Crawler for MAFRA press releases.

Starting URL: https://www.mafra.go.kr/home/5109/subview.do
List action:  /bbs/home/792/artclList.do
Detail URL:   /bbs/home/792/{bbsArtclSeq}/artclView.do
"""

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urlencode, urljoin

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - crawler runtime normally has bs4
    BeautifulSoup = None


_PARSERS = ("html5lib", "lxml", "html.parser")


def _make_soup(raw):
    if BeautifulSoup is None or raw is None:
        return None
    for parser in _PARSERS:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(value):
    if not value:
        return ""
    text = unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u2002", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_lines(value):
    if not value:
        return []
    text = unescape(str(value)).replace("\xa0", " ").replace("\u2002", " ")
    return [_clean_text(line) for line in text.splitlines() if _clean_text(line)]


def _parse_date(value):
    if not value:
        return ""
    match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", value)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"


def _filename_from_attachment_label(value):
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\(\s*파일\s*용량\s*:\s*[^)]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"(.+\.(?:pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip))\b", text, re.I)
    if match:
        return match.group(1).strip()
    return text


def _filename_from_content_disposition(headers):
    if not headers:
        return ""
    match = re.search(r"filename\*=[^=]+=\s*([^;\r\n]+)", headers, re.I)
    if match:
        return unquote(match.group(1).strip().strip("\"'"))
    match = re.search(r"filename=([^;\r\n]+)", headers, re.I)
    if match:
        return unquote(match.group(1).strip().strip("\"'"))
    return ""


class MafraGoKrHomeCrawler(BaseCrawler):
    site_id = "mafra-go-kr-home"
    site_name = "Custom: mafra-go-kr-home"
    base_url = "https://www.mafra.go.kr"

    _START_URL = "https://www.mafra.go.kr/home/5109/subview.do"
    _LIST_URL = "https://www.mafra.go.kr/bbs/home/792/artclList.do"
    _SITE_ID_NATIVE = "home"
    _FNCT_NO = "792"
    _CATEGORY = "보도자료"
    _PUBLISHER = "농림축산식품부"
    _DEFAULT_LAYOUT = "gSMQlq%2FRnioZNDL9kDA24%2FlabU8u4tWWDbOYcFOF2Xw%3D"
    _PAGE_SIZE = 10
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _CURL_USER_AGENT = "Mozilla/5.0"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    def _curl(self, url, data=None, referer=None, headers_only=False):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-sk",
            "-L",
            "--max-time",
            "30",
            "-H",
            f"User-Agent: {self._CURL_USER_AGENT}",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if headers_only:
            cmd.extend(["-D", "-", "-o", "/dev/null"])
        if data is not None:
            cmd.extend([
                "-H",
                "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                "--data",
                urlencode(data),
            ])
        cmd.append(url)

        waits = (1, 3, 9)
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=35,
                    check=False,
                )
            except Exception as exc:
                if attempt == len(waits):
                    print(f"[{self.site_id}] curl failed for {url}: {exc}")
                    return None
                print(f"[{self.site_id}] curl error {attempt}/3 for {url}: {exc}")
                time.sleep(wait)
                continue

            raw = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and (headers_only or raw.strip()):
                return raw

            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            if attempt == len(waits):
                print(
                    f"[{self.site_id}] curl failed for {url}: "
                    f"exit={result.returncode} stderr={stderr}"
                )
                return None
            print(
                f"[{self.site_id}] empty/failed response {attempt}/3 for {url}: "
                f"exit={result.returncode} stderr={stderr}"
            )
            time.sleep(wait)
        return None

    def _page_payload(self, page, layout):
        return {
            "layout": unquote(layout or self._DEFAULT_LAYOUT),
            "page": str(page),
            "row": str(self._PAGE_SIZE),
            "srchColumn": "",
            "srchWrd": "",
            "bbsClSeq": "",
            "bbsOpenWrdSeq": "",
            "rgsBgndeStr": "",
            "rgsEnddeStr": "",
            "isViewMine": "false",
        }

    def _fetch_list_page(self, page, layout=None):
        if page == 1:
            return self._curl(self._START_URL, referer=self.base_url)
        return self._curl(
            self._LIST_URL,
            data=self._page_payload(page, layout),
            referer=self._START_URL,
        )

    def _parse_list_page(self, raw, page):
        soup = _make_soup(raw)
        if soup is None:
            raise ValueError("failed to parse list HTML with all parsers")

        layout_el = soup.select_one("form[name='pageForm'] input[name='layout']")
        layout = layout_el.get("value", "") if layout_el else ""

        items = []
        table = soup.select_one("._articleTable .list table")
        if table is None:
            return {"items": items, "layout": layout, "has_next": False}

        for row in table.select("tbody tr"):
            if row.select_one("td._noData"):
                continue
            link = row.select_one(f"a[href*='/bbs/{self._SITE_ID_NATIVE}/{self._FNCT_NO}/'][href*='/artclView.do']")
            if link is None:
                continue
            href = link.get("href", "")
            match = re.search(r"/bbs/([^/]+)/(\d+)/(\d+)/artclView\.do", href)
            if not match:
                continue

            title = _clean_text(link.get_text(" ", strip=True))
            title = re.sub(r"\s*새글\s*$", "", title).strip()
            cells = row.find_all("td")
            post_number = _clean_text(cells[0].get_text(" ", strip=True)) if cells else ""
            if post_number and not post_number.isdigit():
                num_match = re.search(r"\d+", post_number)
                post_number = num_match.group(0) if num_match else post_number

            date_raw = ""
            date_el = row.select_one("dd.date")
            if date_el is not None:
                date_raw = _clean_text(date_el.get_text(" ", strip=True))
            listed_date = _parse_date(date_raw)
            detail_url = urljoin(self.base_url, href)
            bbs_artcl_seq = match.group(3)

            items.append({
                "siteId": match.group(1),
                "fnctNo": match.group(2),
                "bbsArtclSeq": bbs_artcl_seq,
                "post_number": post_number or None,
                "title": title,
                "listed_date_raw": date_raw,
                "listed_date": listed_date,
                "url": detail_url,
                "page": page,
            })

        has_next = bool(re.search(rf"page_link\(['\"]{page + 1}['\"]\)", raw or ""))
        if not has_next:
            next_link = soup.select_one("._paging a._next[href*='page_link']")
            has_next = next_link is not None
        return {"items": items, "layout": layout, "has_next": has_next}

    def _parse_attachments(self, soup, detail_url):
        attachments = []
        for link in soup.select(".file_list li > a[href*='/download.do']"):
            href = link.get("href", "")
            if not href:
                continue
            url = urljoin(self.base_url, href)
            filename = _filename_from_attachment_label(link.get_text(" ", strip=True))
            seq_match = re.search(r"/(\d+)/download\.do", href)
            attachments.append({
                "url": url,
                "filename": filename or None,
                "fileSeq": seq_match.group(1) if seq_match else None,
            })

        pdf_url = None
        original_filename = None
        for attachment in attachments:
            filename = attachment.get("filename") or ""
            if filename.lower().endswith(".pdf"):
                pdf_url = attachment["url"]
                original_filename = filename
                break

        if pdf_url and not original_filename:
            headers = self._curl(pdf_url, referer=detail_url, headers_only=True)
            original_filename = _filename_from_content_disposition(headers) or None

        return attachments, pdf_url, original_filename

    def _parse_detail_page(self, raw, item):
        soup = _make_soup(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        title_el = soup.select_one(".view_top dt")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else item["title"]

        date_raw = ""
        department = ""
        date_el = soup.select_one(".view_top dd.date")
        if date_el is not None:
            lines = _clean_lines(date_el.get_text("\n", strip=True))
            date_raw = lines[0] if lines else ""
            dept_lines = [line for line in lines[1:] if not re.match(r"^\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}", line)]
            department = "; ".join(dept_lines)

        published_date = _parse_date(date_raw) or item.get("listed_date", "")

        content = soup.select_one(".view_contents")
        abstract = ""
        if content is not None:
            for hidden in content.select("#hwpEditorBoardContent, script, style"):
                hidden.decompose()
            abstract = _clean_text(content.get_text(" ", strip=True))

        attachments, pdf_url, original_filename = self._parse_attachments(soup, item["url"])

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "date_raw": date_raw,
            "department": department,
            "attachments": attachments,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    def crawl(self, limit=None):
        saved = 0
        page = 1
        layout = self._DEFAULT_LAYOUT
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                    break
                if time.time() - start_time >= self._MAX_WALL - 30:
                    print(f"[{self.site_id}] approaching 25-minute crawl budget; stopping cleanly")
                    break

                raw = self._fetch_list_page(page, layout)
                if not raw:
                    print(f"[{self.site_id}] page {page}: empty list response; stopping")
                    break

                try:
                    parsed = self._parse_list_page(raw, page)
                except Exception as exc:
                    print(f"[{self.site_id}] page {page} failed: {exc}")
                    break

                if parsed.get("layout"):
                    layout = parsed["layout"]

                items = parsed["items"]
                if not items:
                    print(f"[{self.site_id}] page {page}: 0 records; stopping")
                    break

                new_items = []
                for item in items:
                    if item["url"] in seen_urls:
                        continue
                    seen_urls.add(item["url"])
                    new_items.append(item)

                if not new_items:
                    print(f"[{self.site_id}] page {page}: no new URLs; stopping")
                    break

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - start_time >= self._MAX_WALL - 30:
                        print(f"[{self.site_id}] approaching 25-minute crawl budget; stopping cleanly")
                        return saved

                    bbs_artcl_seq = item["bbsArtclSeq"]
                    try:
                        detail_raw = self._curl(item["url"], referer=self._START_URL)
                        if not detail_raw:
                            print(f"[{self.site_id}] item {bbs_artcl_seq} failed: empty detail response")
                            continue

                        detail = self._parse_detail_page(detail_raw, item)
                        title = detail["title"] or item["title"]
                        abstract = detail["abstract"]
                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] item {bbs_artcl_seq} abstract too short "
                                f"({len(abstract)} chars), skipping"
                            )
                            continue

                        listed_date = item.get("listed_date") or detail["published_date"]
                        original_filename = detail.get("original_filename")
                        metadata = {
                            "posted_date": item.get("listed_date_raw") or listed_date,
                            "posted_date_iso": listed_date,
                            "originalFilename": original_filename,
                            "journal_raw": None,
                            "series": None,
                            "volume": None,
                            "issue": None,
                            "siteId": item.get("siteId"),
                            "fnctNo": item.get("fnctNo"),
                            "bbsArtclSeq": bbs_artcl_seq,
                            "bbsSeq": self._FNCT_NO,
                            "post_number": item.get("post_number"),
                            "list_page": item.get("page"),
                            "listed_date": listed_date,
                            "listed_date_raw": item.get("listed_date_raw"),
                            "detail_date_raw": detail.get("date_raw"),
                            "department": detail.get("department"),
                            "attachments": detail.get("attachments"),
                        }

                        paper = {
                            "id": f"{self.site_id}:{bbs_artcl_seq}",
                            "site_id": self.site_id,
                            "external_id": bbs_artcl_seq,
                            "post_number": item.get("post_number"),
                            "title": title,
                            "abstract": abstract,
                            "published_date": detail["published_date"] or listed_date,
                            "listed_date": listed_date,
                            "posted_date": listed_date,
                            "authors": "",
                            "publisher": self._PUBLISHER,
                            "department": detail.get("department") or "",
                            "journal": "",
                            "url": item["url"],
                            "pdf_url": detail.get("pdf_url"),
                            "keywords": "",
                            "category": self._CATEGORY,
                            "doi": "",
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }
                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {title[:60]}")
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {bbs_artcl_seq} failed: {exc}")
                        continue
                    time.sleep(self.detail_delay)

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")
                if not parsed.get("has_next"):
                    print(f"[{self.site_id}] page {page}: next page absent; stopping")
                    break
                page += 1
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done. saved {saved}")
        return saved
