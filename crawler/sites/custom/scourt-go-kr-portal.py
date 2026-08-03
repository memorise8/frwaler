# -*- coding: utf-8 -*-
"""대법원 포털 보도자료/언론보도해명 crawler.

Target: https://www.scourt.go.kr/portal/news/NewsListAction.work?gubun=6
List:   GET ?gubun=6&pageIndex=N  (10 items/page, ~293 pages as of 2026-05)
Detail: GET /portal/news/NewsViewAction.work?seqnum=XXXX&gubun=6
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

_BASE_URL = "https://www.scourt.go.kr"
_LIST_URL = f"{_BASE_URL}/portal/news/NewsListAction.work"
_DETAIL_URL = f"{_BASE_URL}/portal/news/NewsViewAction.work"
_GUBUN = "6"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = 24 * 60  # 24 min (25-min budget)
_BACKOFFS = [1, 3, 9]


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if not _BS4_OK:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class ScourtPortalCrawler(BaseCrawler):
    """대법원 포털 보도자료/언론보도해명 crawler."""

    site_id = "scourt-go-kr-portal"
    site_name = "Custom: scourt-go-kr-portal"
    base_url = "https://www.scourt.go.kr"

    # ------------------------------------------------------------------
    # curl helper — handles EUC-KR decoding + exponential backoff
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
            url,
        ]
        for attempt, wait in enumerate(_BACKOFFS):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.returncode == 0 and result.stdout:
                    try:
                        return result.stdout.decode("euc-kr", errors="replace")
                    except Exception:
                        return result.stdout.decode("utf-8", errors="replace")
                if attempt < len(_BACKOFFS) - 1:
                    print(
                        f"[{self.site_id}] Empty/error response "
                        f"(attempt {attempt + 1}/3), retry in {wait}s"
                    )
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] Timeout on {url} (attempt {attempt + 1}/3)")
                if attempt < len(_BACKOFFS) - 1:
                    time.sleep(wait)
            except Exception as exc:
                print(f"[{self.site_id}] curl error: {exc} (attempt {attempt + 1}/3)")
                if attempt < len(_BACKOFFS) - 1:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> str | None:
        return self._curl_get(f"{_LIST_URL}?gubun={_GUBUN}&pageIndex={page}")

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return [{seqnum, title, list_date}, ...] from list page HTML."""
        soup = _make_soup(html)
        if soup is None:
            return []
        tbody = soup.find("tbody")
        if not tbody:
            return []
        items = []
        for row in tbody.find_all("tr"):
            tds = row.find_all("td")
            # Row layout: mhid | tit(icon) | tit(link) | date | attach | views
            seqnum = ""
            title = ""
            list_date = ""
            for td in tds:
                if "tit" in (td.get("class") or []):
                    a = td.find("a", href=re.compile(r"seqnum=\d+"))
                    if a:
                        m = re.search(r"seqnum=(\d+)", a.get("href", ""))
                        if m:
                            seqnum = m.group(1)
                            title = a.get_text(strip=True)
                            break
            if not seqnum:
                continue
            # Date: first TD with no class whose text looks like a date
            for td in tds:
                if not (td.get("class") or []):
                    txt = td.get_text(strip=True)
                    if re.match(r"\d{4}-\d{2}-\d{2}", txt):
                        list_date = txt
                        break
            items.append({"seqnum": seqnum, "title": title, "list_date": list_date})
        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, seqnum: str) -> str | None:
        return self._curl_get(f"{_DETAIL_URL}?seqnum={seqnum}&gubun={_GUBUN}")

    def _parse_detail(self, html: str) -> dict | None:
        """Parse detail page. Returns None when content is unavailable."""
        soup = _make_soup(html)
        if soup is None:
            return None

        table = soup.find("table", class_="tableVer")

        # Title
        title = ""
        if table:
            for th in table.find_all("th"):
                if "제목" in th.get_text():
                    td = th.find_next_sibling("td")
                    if td:
                        title = td.get_text(strip=True)
                    break

        # Date (등록일)
        published_date = ""
        if table:
            for th in table.find_all("th"):
                if "등록일" in th.get_text():
                    td = th.find_next_sibling("td")
                    if td:
                        raw = td.get_text(strip=True)
                        m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
                        if m:
                            published_date = (
                                f"{m.group(1)}-{int(m.group(2)):02d}"
                                f"-{int(m.group(3)):02d}"
                            )
                        else:
                            published_date = raw
                    break

        # Content (contArea td)
        content_td = soup.find("td", class_="contArea")
        abstract = ""
        if content_td:
            abstract = content_td.get_text(separator="\n", strip=True)
            abstract = re.sub(r"\n{3,}", "\n\n", abstract).strip()

        if not abstract:
            return None

        # Attachments: scan all <a> for download('file_id','display_name')
        attach_files: list[dict] = []
        seen_fids: set[str] = set()
        _dl_re = re.compile(r"download\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")
        for a in soup.find_all("a"):
            for attr in ("href", "onclick"):
                val = a.get(attr) or ""
                m = _dl_re.search(val)
                if m:
                    fid, fname = m.group(1), m.group(2)
                    if fid not in seen_fids:
                        seen_fids.add(fid)
                        attach_files.append({"file_id": fid, "filename": fname})

        pdf_url = None
        original_filename = None
        for f in attach_files:
            if f["filename"].lower().endswith(".pdf"):
                original_filename = f["filename"]
                # Actual download requires a POST; store best-effort reference URL
                pdf_url = (
                    f"https://file.scourt.go.kr/AttachDownload"
                    f"?file={f['file_id']}&path=001"
                )
                break
        if original_filename is None and attach_files:
            original_filename = attach_files[0]["filename"]

        return {
            "title": title,
            "published_date": published_date,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "attach_files": attach_files,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 대법원 포털 보도자료/언론보도해명.

        Paginates through the list (10 items/page) and fetches each detail
        page.  Stops when ``limit`` items are saved, no new items appear, or
        safety caps are hit.
        """
        start_time = time.time()
        saved = 0
        page = 1
        seen_seqnums: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # --- guards ---
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(
                    f"[{self.site_id}] Reached safety cap of {_MAX_PAGES} pages. Stopping."
                )
                break
            elapsed = time.time() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                print(
                    f"[{self.site_id}] Approaching 25-minute wall budget "
                    f"({elapsed:.0f}s). Stopping."
                )
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # --- fetch list page ---
            html = None
            for attempt, wait in enumerate(_BACKOFFS):
                html = self._fetch_list_page(page)
                if html:
                    break
                if attempt < len(_BACKOFFS) - 1:
                    print(
                        f"[{self.site_id}] List page {page} empty "
                        f"(attempt {attempt + 1}/3), retry in {wait}s"
                    )
                    time.sleep(wait)

            if not html:
                print(f"[{self.site_id}] Could not fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # --- URL deduplication (detects silent pagination loops) ---
            new_items = [it for it in items if it["seqnum"] not in seen_seqnums]
            if not new_items:
                print(f"[{self.site_id}] All seqnums on page {page} already seen. Done.")
                break
            for it in new_items:
                seen_seqnums.add(it["seqnum"])

            # --- per-item detail fetch + save ---
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                seqnum = item["seqnum"]
                detail_url = f"{_DETAIL_URL}?seqnum={seqnum}&gubun={_GUBUN}"

                try:
                    time.sleep(self._delay)

                    detail_html = None
                    for attempt, wait in enumerate(_BACKOFFS):
                        detail_html = self._fetch_detail(seqnum)
                        if detail_html:
                            break
                        if attempt < len(_BACKOFFS) - 1:
                            print(
                                f"[{self.site_id}] Detail seqnum={seqnum} empty "
                                f"(attempt {attempt + 1}/3), retry in {wait}s"
                            )
                            time.sleep(wait)

                    if not detail_html:
                        print(
                            f"[{self.site_id}] Could not fetch seqnum={seqnum}. Skipping."
                        )
                        continue

                    detail = self._parse_detail(detail_html)
                    if detail is None:
                        print(
                            f"[{self.site_id}] No content for seqnum={seqnum}. Skipping."
                        )
                        continue

                    abstract = detail["abstract"]
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] Abstract too short "
                            f"({len(abstract)} chars) for seqnum={seqnum}. Skipping."
                        )
                        continue

                    title = detail["title"] or item["title"]
                    pub_date = detail["published_date"]
                    listed_date = item["list_date"] or pub_date

                    paper = {
                        "site_id": self.site_id,
                        "external_id": seqnum,
                        "post_number": seqnum,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "publisher": "대법원",
                        "authors": "",
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "doi": "",
                        "category": "보도자료",
                        "metadata": json.dumps(
                            {
                                "seqnum": seqnum,
                                "gubun": _GUBUN,
                                "posted_date": pub_date,
                                "attach_files": detail.get("attach_files", []),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item seqnum={seqnum} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
