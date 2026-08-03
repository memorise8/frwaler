# -*- coding: utf-8 -*-
"""Ministry of Justice Korea press-release crawler.

Target: https://www.moj.go.kr/moj/221/subview.do

The board is a K2Web Wizard BBS:

* list:   /moj/221/subview.do?page=N
* detail: /bbs/moj/182/{bbsArtclSeq}/artclView.do
* file:   /bbs/moj/182/{atchmnflSeq}/download.do
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


def _make_soup(raw):
    """Build BeautifulSoup with a tolerant parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[moj-go-kr-moj] BeautifulSoup parser {parser} failed: {exc}")
            continue
    try:
        return BeautifulSoup("", "html.parser")
    except Exception:
        return None


def _text(node, sep=" "):
    if node is None:
        return ""
    text = node.get_text(separator=sep, strip=True)
    text = unquote(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(value):
    if not value:
        return None
    match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", value)
    if not match:
        return value.strip()
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


class MojGoKrMojCrawler(BaseCrawler):
    """Crawler for 법무부 보도자료."""

    site_id = "moj-go-kr-moj"
    site_name = "Custom: moj-go-kr-moj"
    base_url = "https://www.moj.go.kr"

    _START_URL = "https://www.moj.go.kr/moj/221/subview.do"
    _SITE_ID_NATIVE = "moj"
    _FNCT_NO = "182"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _WALL_GRACE_SECONDS = 30
    _RETRY_WAITS = (1, 3, 9)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, referer=None, max_time=45):
        """Fetch a URL with curl, retrying transient network/site failures."""
        headers = [
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        ]
        if referer:
            headers.extend(["-e", referer])

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-sk",
            "-L",
            "--compressed",
            "--max-time",
            str(max_time),
            *headers,
            url,
        ]
        for attempt, wait in enumerate(self._RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=max_time + 5)
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl empty/error for {url} "
                    f"(attempt {attempt}/3): {err or 'empty response'}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl failed for {url} (attempt {attempt}/3): {exc}")

            if attempt < 3:
                time.sleep(wait)
        return None

    def _curl_head_filename(self, url):
        """Best-effort filename extraction from Content-Disposition."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-sk",
            "-I",
            "-L",
            "--max-time",
            "20",
            "-A",
            self.USER_AGENT,
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=25)
        except Exception:
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        headers = result.stdout.decode("utf-8", errors="replace")
        match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", headers, re.I)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        match = re.search(r'filename="?([^";\r\n]+)"?', headers, re.I)
        if match:
            return unquote(match.group(1).strip())
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 1:
            return self._START_URL
        return f"{self._START_URL}?page={page}"

    def _parse_list_page(self, raw, page, list_url):
        soup = _make_soup(raw)
        if soup is None:
            return [], False

        items = []
        table = soup.select_one("table.artclTable") or soup.find("table")
        if not table:
            return [], False

        for row in table.select("tbody tr"):
            link = row.select_one("a.artclLinkView[href]") or row.find(
                "a", href=lambda href: href and "/artclView" in href
            )
            if not link:
                continue

            href = link.get("href") or ""
            detail_url = urljoin(self.base_url, href)
            seq_match = re.search(r"/bbs/([^/]+)/([^/]+)/(\d+)/artclView", href)
            if not seq_match:
                seq_match = re.search(r"jf_viewArtcl\('([^']+)'\s*,\s*'([^']+)'\s*,\s*'(\d+)'", link.get("onclick", ""))
            if seq_match:
                native_site_id, fnct_no, bbs_artcl_seq = seq_match.groups()
            else:
                native_site_id, fnct_no, bbs_artcl_seq = self._SITE_ID_NATIVE, self._FNCT_NO, None

            title_node = link.find("strong") or link
            title = _text(title_node)
            if not title:
                continue

            num_text = _text(row.select_one("td._artclTdNum"))
            num_match = re.search(r"\d+", num_text)
            post_number = num_match.group(0) if num_match else (bbs_artcl_seq or None)

            date_raw = _text(row.select_one("td._artclTdRdate"))
            department = _text(row.select_one("td.progress"))
            access_count = _text(row.select_one("td._artclTdAccess"))
            attachment_label = _text(row.select_one("td._artclTdAtchFile"))

            items.append(
                {
                    "page": page,
                    "source_list_url": list_url,
                    "siteId": native_site_id,
                    "fnctNo": fnct_no,
                    "bbsArtclSeq": bbs_artcl_seq,
                    "external_id": bbs_artcl_seq or post_number,
                    "post_number": post_number,
                    "title": title,
                    "department": department,
                    "listed_date_raw": date_raw,
                    "listed_date": _iso_date(date_raw),
                    "access_count": access_count,
                    "attachment_label": attachment_label,
                    "url": detail_url,
                }
            )

        has_next = False
        for anchor in soup.select("div._paging a[href*='page_link']"):
            href = anchor.get("href", "")
            if re.search(rf"page_link\('{page + 1}'\)", href):
                has_next = True
                break
        if not has_next:
            state = soup.select_one("._pageState")
            if state:
                cur = soup.select_one("._pageState ._curPage")
                total = soup.select_one("._pageState ._totPage")
                try:
                    has_next = int(_text(cur)) < int(_text(total))
                except (TypeError, ValueError):
                    has_next = False

        return items, has_next

    def _parse_detail(self, raw, item):
        soup = _make_soup(raw)
        if soup is None:
            return None

        title = _text(soup.select_one(".artclViewTitle")) or item["title"]

        detail_fields = {}
        for dl in soup.select(".dataInfo .infor dl"):
            label = _text(dl.find("dt"))
            value = _text(dl.find("dd"))
            if label:
                detail_fields[label] = value

        body_node = soup.select_one(".artclView")
        if body_node:
            for removable in body_node.find_all(["iframe", "script", "style"]):
                removable.decompose()
        abstract = _text(body_node, sep="\n")

        attachments = self._parse_attachments(soup)
        attachment_names = [a["name"] for a in attachments if a.get("name")]
        if len(abstract) < 100 and attachment_names:
            attachment_text = "첨부파일: " + "; ".join(attachment_names)
            abstract = "\n".join(part for part in (abstract, attachment_text) if part).strip()

        pdf = next((a for a in attachments if a.get("is_pdf")), None)
        if pdf is None:
            pdf = next((a for a in attachments if ".pdf" in (a.get("url") or "").lower()), None)

        pdf_url = pdf.get("url") if pdf else None
        original_filename = pdf.get("name") if pdf else None
        if pdf_url and not original_filename:
            original_filename = self._curl_head_filename(pdf_url)
        if not original_filename and pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            original_filename = tail if "." in tail else None

        date_raw = detail_fields.get("작성일") or item.get("listed_date_raw")
        listed_date = _iso_date(item.get("listed_date_raw"))
        published_date = _iso_date(date_raw) or listed_date
        department = detail_fields.get("담당부서") or item.get("department") or None
        authors = detail_fields.get("담당자") or None

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date or published_date,
            "posted_date_raw": item.get("listed_date_raw"),
            "detail_date_raw": date_raw,
            "department": department,
            "authors": authors,
            "phone": detail_fields.get("전화번호"),
            "kogl_type": detail_fields.get("공공누리"),
            "access_count": detail_fields.get("조회수") or item.get("access_count"),
            "attachments": attachments,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "detail_fields": detail_fields,
        }

    def _parse_attachments(self, soup):
        attachments = []
        seen = set()
        for link in soup.select("dd.fileList a[href*='/download.do'], a[href*='/download.do']"):
            href = link.get("href") or ""
            url = urljoin(self.base_url, href)
            if url in seen:
                continue
            seen.add(url)

            name = _text(link)
            if not name or name == "다운로드":
                name = link.get("aria-label") or link.get("title") or ""
                name = _text(BeautifulSoup(name, "html.parser")) if name else ""

            file_id_match = re.search(r"/(\d+)/download\.do", href)
            file_id = file_id_match.group(1) if file_id_match else None
            lower_name = name.lower()
            attachments.append(
                {
                    "name": name or None,
                    "url": url,
                    "file_id": file_id,
                    "is_pdf": lower_name.endswith(".pdf") or ".pdf" in lower_name,
                }
            )
        return attachments

    def _build_paper(self, item, detail):
        metadata = {
            "posted_date": detail.get("posted_date_raw"),
            "listed_date": detail.get("listed_date"),
            "listed_date_raw": item.get("listed_date_raw"),
            "published_date_raw": detail.get("detail_date_raw"),
            "originalFilename": detail.get("original_filename"),
            "siteId": item.get("siteId"),
            "fnctNo": item.get("fnctNo"),
            "bbsArtclSeq": item.get("bbsArtclSeq"),
            "post_number": item.get("post_number"),
            "node_id": "221",
            "source_list_url": item.get("source_list_url"),
            "page": item.get("page"),
            "department": detail.get("department"),
            "contact_person": detail.get("authors"),
            "phone": detail.get("phone"),
            "access_count": detail.get("access_count"),
            "kogl_type": detail.get("kogl_type"),
            "attachment_label": item.get("attachment_label"),
            "attachments": detail.get("attachments", []),
            "detail_fields": detail.get("detail_fields", {}),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

        return {
            "id": f"{self.site_id}-{item.get('external_id')}",
            "site_id": self.site_id,
            "external_id": item.get("external_id"),
            "post_number": item.get("post_number"),
            "title": detail.get("title") or item.get("title"),
            "abstract": detail.get("abstract"),
            "published_date": detail.get("published_date"),
            "listed_date": detail.get("listed_date"),
            "posted_date": detail.get("listed_date"),
            "authors": detail.get("authors"),
            "publisher": "법무부",
            "department": detail.get("department"),
            "journal": None,
            "url": item.get("url"),
            "pdf_url": detail.get("pdf_url"),
            "keywords": "법무부, 보도자료",
            "category": "보도자료",
            "doi": None,
            "original_filename": detail.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MOJ press-release pages and save documents."""
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        page = 1
        seen_urls = set()
        started = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= self._WALL_SECONDS - self._WALL_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, referer=self._START_URL)
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            items, has_next = self._parse_list_page(raw, page, list_url)
            if not items:
                print(f"[{self.site_id}] page {page}: no records; stopping")
                break

            new_items = []
            for item in items:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - started >= self._WALL_SECONDS - self._WALL_GRACE_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    return saved

                item_label = item.get("external_id") or item.get("post_number") or item.get("url")
                try:
                    if self._delay:
                        time.sleep(self._delay)
                    detail_raw = self._curl_get(item["url"], referer=list_url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_label} failed: could not fetch detail")
                        continue

                    detail = self._parse_detail(detail_raw, item)
                    if not detail:
                        print(f"[{self.site_id}] item {item_label} failed: could not parse detail")
                        continue

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_label}: abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue

                    self._save_paper(self._build_paper(item, detail))
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {detail.get('title', '')[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
