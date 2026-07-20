# -*- coding: utf-8 -*-
"""병무청 보도자료 게시판 crawler.

Board: https://www.mma.go.kr/board/boardList.do?gesipan_id=15&mc=mma0000392
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import urljoin

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

# BeautifulSoup parser fallback chain
try:
    from bs4 import BeautifulSoup as _BS

    _PARSERS: list[str] = []
    for _p in ("html5lib", "lxml", "html.parser"):
        try:
            _BS("<p>x</p>", _p)
            _PARSERS.append(_p)
        except Exception:
            pass
    _DEFAULT_PARSER = _PARSERS[0] if _PARSERS else "html.parser"
except ImportError:
    _BS = None  # type: ignore[assignment]
    _DEFAULT_PARSER = "html.parser"

_SITE_ID = "mma-go-kr-board"
_BASE_URL = "https://www.mma.go.kr"
_LIST_PATH = "/board/boardList.do"
_VIEW_PATH = "/board/boardView.do"
_DOWN_PATH = "/boardFileDown.do"
_GESIPAN_ID = "15"
_MC = "mma0000392"
_PAGE_SIZE = 10
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60  # 25 minutes


def _soup(html: str):
    """Parse HTML with parser fallback chain; never raises."""
    if _BS is None:
        return None
    for parser in _PARSERS or ["html.parser"]:
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_date(raw: str) -> str:
    """Return ISO date string from various formats; empty string on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = re.search(r"(\d{8})", raw)
    if m:
        d = m.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return ""


class MmaGoBoardCrawler(BaseCrawler):
    """병무청 보도자료 게시판 crawler."""

    site_id = _SITE_ID
    site_name = "Custom: mma-go-kr-board"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, timeout: int = 30) -> str | None:
        """GET via curl with TLS workaround for Korean gov sites. Retries 3×."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
            "-H", f"Referer: {_BASE_URL}/",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 5
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if raw.strip():
                    return raw
                if attempt < 2:
                    wait = (attempt + 1) ** 2
                    print(f"[{_SITE_ID}] Empty response, retry {attempt+1}/3 in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = (attempt + 1) ** 2
                    print(f"[{_SITE_ID}] curl error: {exc}, retry {attempt+1}/3 in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[{_SITE_ID}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page_index: int) -> list[dict]:
        """Return list of {gsgeul_no, title, date, department} dicts from one page."""
        url = (
            f"{_BASE_URL}{_LIST_PATH}"
            f"?gesipan_id={_GESIPAN_ID}&mc={_MC}&pageIndex={page_index}"
            f"&pageUnit={_PAGE_SIZE}"
        )
        raw = self._curl_get(url)
        if not raw:
            return []

        soup = _soup(raw)
        if soup is None:
            return []

        items = []
        table = soup.find("table", class_="board_notice")
        if not table:
            return []

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else []
        for row in rows:
            # Title cell
            td_title = row.find("td", class_="text_left")
            if not td_title:
                continue
            a_tag = td_title.find("a")
            if not a_tag:
                continue
            href = a_tag.get("href", "")
            m = re.search(r"gsgeul_no=(\d+)", href)
            if not m:
                continue
            gsgeul_no = m.group(1)
            title = " ".join(a_tag.get_text(" ", strip=True).split())

            # Date cell
            tds = row.find_all("td")
            date_text = ""
            for td in tds:
                text = td.get_text(strip=True)
                if re.match(r"\d{4}-\d{2}-\d{2}", text):
                    date_text = text
                    break

            # Department (skip_m td before date)
            dept = ""
            skip_tds = [td for td in tds if "skip_m" in td.get("class", [])]
            if skip_tds:
                dept = skip_tds[0].get_text(strip=True)

            items.append({
                "gsgeul_no": gsgeul_no,
                "title": title,
                "listed_date": _parse_date(date_text),
                "department": dept,
            })

        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, gsgeul_no: str) -> dict | None:
        """Fetch and parse a single detail page. Returns None on failure."""
        url = (
            f"{_BASE_URL}{_VIEW_PATH}"
            f"?gesipan_id={_GESIPAN_ID}&gsgeul_no={gsgeul_no}"
            f"&pageIndex=1&mc={_MC}"
        )
        raw = self._curl_get(url)
        if not raw:
            return None

        soup = _soup(raw)
        if soup is None:
            return None

        table = soup.find("table", class_="notice_view")
        if not table:
            return None

        result: dict = {"url": url, "gsgeul_no": gsgeul_no}

        # Title
        title_th = table.find("th", string=re.compile(r"제목"))
        if title_th:
            td = title_th.find_next_sibling("td")
            if td:
                result["title"] = td.get_text(" ", strip=True)

        # Author (작성자) / date (작성일) / 최종 수정일
        meta_td = table.find("td", attrs={"colspan": "2"})
        if meta_td:
            meta_text = meta_td.get_text(" ", strip=True)
            m_author = re.search(r"작성자\s*:\s*([^\s]+(?:\s[^\s]+)*?)(?=\s{2,}|\s작성일|\s조회|$)", meta_text)
            if m_author:
                result["department"] = m_author.group(1).strip()
            m_date = re.search(r"작성일\s*:\s*(\d{4}-\d{2}-\d{2})", meta_text)
            if m_date:
                result["published_date"] = m_date.group(1)
            m_mod = re.search(r"최종\s*수정일\s*:\s*(\d{4}-\d{2}-\d{2})", meta_text)
            if m_mod:
                result["modified_date"] = m_mod.group(1)

        # Content (abstract)
        con_td = table.find("td", class_="con_text")
        if con_td:
            result["abstract_html"] = str(con_td)
            result["abstract"] = _strip_tags(str(con_td))

        # 부제목1/2/3 — gather any additional th/td pairs
        extra_fields: dict = {}
        for tr in table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
                label = th.get_text(strip=True)
                if label in ("제목", "첨부파일") or td.get("colspan"):
                    continue
                val = td.get_text(" ", strip=True)
                if label and val:
                    extra_fields[label] = val
        if extra_fields:
            result["extra_fields"] = extra_fields

        # Files
        files = []
        for a in table.find_all("a", href=re.compile(r"/boardFileDown\.do")):
            href = a["href"]
            fn_match = re.search(r"ilryeon_no=(\d+)", href)
            ilryeon = fn_match.group(1) if fn_match else "1"
            # Filename from link text or title attribute
            fname = a.get("title", "")
            fname = re.sub(r"\s+파일\s+다운로드$", "", fname).strip()
            link_text = a.get_text(" ", strip=True)
            if not fname:
                fname = link_text
            down_url = urljoin(_BASE_URL, href.replace("&amp;", "&"))
            files.append({
                "url": down_url,
                "filename": fname,
                "ilryeon_no": ilryeon,
            })
        result["files"] = files

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 병무청 보도자료. Walks pages until limit reached or no more items."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget check
            elapsed = time.monotonic() - start_time
            if elapsed > _CRAWL_BUDGET_SECS:
                print(f"[{_SITE_ID}] Approaching 25-min budget at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            try:
                list_items = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] List page {page} error: {exc}")
                break

            if not list_items:
                print(f"[{_SITE_ID}] No items at page {page}. Done.")
                break

            # URL deduplication check — detect paginator looping back
            first_url = f"{_BASE_URL}{_VIEW_PATH}?gesipan_id={_GESIPAN_ID}&gsgeul_no={list_items[0]['gsgeul_no']}"
            if first_url in seen_urls:
                print(f"[{_SITE_ID}] Duplicate first URL on page {page}, stopping (loop detected).")
                break

            new_this_page = 0
            for item in list_items:
                if limit is not None and saved >= limit:
                    break

                gsgeul_no = item["gsgeul_no"]
                detail_url = (
                    f"{_BASE_URL}{_VIEW_PATH}"
                    f"?gesipan_id={_GESIPAN_ID}&gsgeul_no={gsgeul_no}"
                    f"&pageIndex={page}&mc={_MC}"
                )

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_this_page += 1

                time.sleep(self._delay)

                try:
                    detail = self._fetch_detail(gsgeul_no)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {gsgeul_no} failed: {exc}")
                    continue

                if not detail:
                    print(f"[{_SITE_ID}] item {gsgeul_no} failed: empty detail")
                    continue

                title = detail.get("title") or item.get("title") or ""
                abstract = detail.get("abstract") or ""
                published_date = detail.get("published_date") or item.get("listed_date") or ""
                listed_date = item.get("listed_date") or published_date
                department = detail.get("department") or item.get("department") or "병무청"

                # Short abstract guard
                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] item {gsgeul_no} skipped: abstract too short ({len(abstract)} chars)")
                    continue

                # PDF/HWP file: prefer PDF, else first file
                files = detail.get("files", [])
                pdf_url = None
                original_filename = None
                for f in files:
                    fn = f.get("filename", "").lower()
                    if fn.endswith(".pdf"):
                        pdf_url = f["url"]
                        original_filename = f["filename"]
                        break
                if not pdf_url and files:
                    pdf_url = files[0]["url"]
                    original_filename = files[0]["filename"]

                metadata = {
                    "posted_date": listed_date,
                    "gsgeul_no": gsgeul_no,
                    "gesipan_id": _GESIPAN_ID,
                    "files": files,
                }
                if detail.get("modified_date"):
                    metadata["modified_date"] = detail["modified_date"]
                if detail.get("extra_fields"):
                    metadata["extra_fields"] = detail["extra_fields"]

                paper = {
                    "site_id": self.site_id,
                    "external_id": gsgeul_no,
                    "post_number": gsgeul_no,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": listed_date,
                    "authors": "",
                    "publisher": "병무청",
                    "department": department,
                    "url": detail_url,
                    "pdf_url": pdf_url or "",
                    "original_filename": original_filename or "",
                    "category": "보도자료",
                    "keywords": "",
                    "doi": "",
                    "journal": "",
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                except Exception as exc:
                    print(f"[{_SITE_ID}] save failed for {gsgeul_no}: {exc}")
                    continue

                saved += 1
                print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {title[:60]}")

            if new_this_page == 0:
                print(f"[{_SITE_ID}] Page {page} had no new items (all seen). Stopping.")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
