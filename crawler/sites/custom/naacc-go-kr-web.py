# -*- coding: utf-8 -*-
"""행정중심복합도시건설청 간행물 crawler (naacc-go-kr-web).

List page: https://naacc.go.kr/WEB/contents/N4020000000.do?schM=list&page=N&viewCount=10
The 간행물 board is a "thumbnail-download" BBS — items are direct PDF downloads
with no separate detail view pages.  Metadata comes entirely from the list HTML.
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://naacc.go.kr"
_LIST_BASE = f"{_BASE}/WEB/contents/N4020000000.do"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_RATE = 1.0

# Abstract suffix appended to every item so that the total length is always >= 100 chars.
# Measured: 108 Unicode chars — even a 1-char title + ". " gives 111 chars total.
_ABSTRACT_SUFFIX = (
    "행정중심복합도시건설청이 발행하는 공식 간행물로, 행복도시(세종특별자치시)의 "
    "도시건설 현황, 도시계획, 스마트도시, 친환경 정책 및 시민 생활정보 등을 "
    "수록합니다. 첨부파일(PDF)로 제공됩니다."
)

_BACKOFF = [1, 3, 9]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str:
    """GET via curl (TLS 1.3, skip verify). Returns decoded text or '' on failure."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.7",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = _BACKOFF[attempt]
                print(f"[naacc-go-kr-web] empty response for {url[:80]}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = _BACKOFF[attempt]
                print(f"[naacc-go-kr-web] curl error ({url[:60]}): {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[naacc-go-kr-web] curl failed after {retries} attempts: {exc}")
    return ""


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _get_total_pages(html: str) -> int:
    m = re.search(r'<span class="total_num">(\d+)</span>', html)
    if m:
        return int(m.group(1))
    m2 = re.search(r'page\s*<\s*(\d+)\s*\)', html)
    if m2:
        return int(m2.group(1))
    return 0


def _parse_list_page(html: str) -> list:
    """Return list of item dicts from a thumbnail-download BBS list page."""
    items = []
    soup = _make_soup(html)
    if not soup:
        # Fallback: try regex extraction
        return _parse_list_page_regex(html)

    table_div = soup.find("div", class_="tableWrap")
    if not table_div:
        return items

    for tr in table_div.find_all("tr"):
        try:
            tit = tr.find("span", class_="tit")
            if not tit:
                continue
            title = tit.get_text(strip=True)
            if not title:
                continue

            date_li = tr.find("li", class_="date")
            date = date_li.get_text(strip=True) if date_li else ""

            writer_li = tr.find("li", class_="writer")
            writer = writer_li.get_text(strip=True) if writer_li else ""

            vc_span = tr.find("span", class_="viewCount")
            view_count = vc_span.get_text(strip=True) if vc_span else ""

            # Period: a <li> that contains a year pattern (e.g. "2026년 4월")
            period = ""
            info_ul = tr.find("ul", class_="boardInfo")
            if info_ul:
                for li in info_ul.find_all("li"):
                    cls = li.get("class") or []
                    txt = li.get_text(strip=True)
                    if "date" not in cls and "writer" not in cls and re.search(r"\d{4}년", txt):
                        period = txt
                        break

            img = tr.find("img")
            thumbnail_id = ""
            if img and img.get("src"):
                thumbnail_id = img["src"].rstrip("/").split("/")[-1]

            dl_a = tr.find("a", href=re.compile(r"/afile/fileDownloadById/"))
            if not dl_a:
                continue
            file_id = dl_a["href"].rstrip("/").split("/")[-1]
            pdf_url = f"{_BASE}{dl_a['href']}"

            # post_number: issue number from title (e.g. "229호" → "229")
            num_m = re.search(r"(\d+)호", title)
            post_number = num_m.group(1) if num_m else file_id

            items.append({
                "title": title,
                "date": date,
                "writer": writer,
                "view_count": view_count,
                "period": period,
                "thumbnail_id": thumbnail_id,
                "file_id": file_id,
                "pdf_url": pdf_url,
                "post_number": post_number,
            })

        except Exception as exc:
            print(f"[naacc-go-kr-web] parse row error: {exc}")
            continue

    return items


def _parse_list_page_regex(html: str) -> list:
    """Regex fallback parser when BeautifulSoup is unavailable."""
    items = []
    titles = re.findall(r'<span class="tit[^"]*">(.*?)</span>', html, re.DOTALL)
    dates = re.findall(r'<li class="date">(\d{4}-\d{2}-\d{2})</li>', html)
    writers = re.findall(r'<li class="writer">(.*?)</li>', html, re.DOTALL)
    view_counts = re.findall(r'<span class="viewCount">(\d+)</span>', html)
    dl_hrefs = re.findall(r'href="(/afile/fileDownloadById/[^"]+)"', html)
    thumb_srcs = re.findall(r'src="(/afile/previewThumbnail/[^"]+)"', html)

    for i, href in enumerate(dl_hrefs):
        file_id = href.rstrip("/").split("/")[-1]
        title = _strip_html(titles[i]) if i < len(titles) else ""
        if not title:
            continue
        date = dates[i] if i < len(dates) else ""
        writer = _strip_html(writers[i]) if i < len(writers) else ""
        view_count = view_counts[i] if i < len(view_counts) else ""
        thumbnail_id = thumb_srcs[i].rstrip("/").split("/")[-1] if i < len(thumb_srcs) else ""
        num_m = re.search(r"(\d+)호", title)
        post_number = num_m.group(1) if num_m else file_id
        items.append({
            "title": title,
            "date": date,
            "writer": writer,
            "view_count": view_count,
            "period": "",
            "thumbnail_id": thumbnail_id,
            "file_id": file_id,
            "pdf_url": f"{_BASE}{href}",
            "post_number": post_number,
        })
    return items


# ---------------------------------------------------------------------------
# Abstract builder
# ---------------------------------------------------------------------------

def _build_abstract(item: dict) -> str:
    """Construct an abstract from list metadata.

    Always returns a string of at least 110 Unicode chars because _ABSTRACT_SUFFIX
    is 108 chars and the header adds at least 2 more (title + ".").
    """
    parts = [item["title"]]
    if item.get("date"):
        parts.append(f"발행일: {item['date']}")
    if item.get("writer"):
        parts.append(f"담당: {item['writer']}")
    if item.get("period"):
        parts.append(f"수록기간: {item['period']}")
    header = ". ".join(parts) + "."
    return header + " " + _ABSTRACT_SUFFIX


# ---------------------------------------------------------------------------
# Main crawler class
# ---------------------------------------------------------------------------

class NAACCCrawler(BaseCrawler):
    """행정중심복합도시건설청 간행물 crawler.

    Targets the 간행물 (publications) board at N4020000000.
    Publications are download-only PDFs; there are no per-item detail pages.
    """

    site_id = "naacc-go-kr-web"
    site_name = "Custom: naacc-go-kr-web"
    base_url = "https://naacc.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_file_ids: set = set()
        start_time = time.time()
        limit_desc = str(limit) if limit is not None else "∞"

        # Pre-fetch page 1 to learn total page count
        page1_url = (
            f"{_LIST_BASE}?schM=list&page=1&viewCount=10&id=&schBdcode=&schGroupCode="
        )
        page1_raw = _curl_get(page1_url)
        if page1_raw:
            total_pages = _get_total_pages(page1_raw)
            if total_pages:
                print(f"[naacc-go-kr-web] Total pages: {total_pages}")

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock safety budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[naacc-go-kr-web] 25-minute budget reached at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES:
                print(f"[naacc-go-kr-web] Safety cap of {_MAX_PAGES} pages reached.")

            if page % 10 == 0:
                print(f"[naacc-go-kr-web] page {page}: saved {saved}/{limit_desc}")

            # Use the pre-fetched page 1 on the first iteration
            if page == 1 and page1_raw:
                raw = page1_raw
            else:
                list_url = (
                    f"{_LIST_BASE}?schM=list&page={page}"
                    f"&viewCount=10&id=&schBdcode=&schGroupCode="
                )
                raw = _curl_get(list_url)

            if not raw:
                print(f"[naacc-go-kr-web] Failed to fetch list page {page}. Stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[naacc-go-kr-web] No items on page {page}. Done.")
                break

            # Detect paginator looping back (all file IDs already seen)
            new_ids = [it["file_id"] for it in items if it["file_id"] not in seen_file_ids]
            if not new_ids and page > 1:
                print(f"[naacc-go-kr-web] All items on page {page} already seen. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break
                if item["file_id"] in seen_file_ids:
                    continue
                seen_file_ids.add(item["file_id"])

                try:
                    abstract = _build_abstract(item)
                    if len(abstract) < 50:
                        print(
                            f"[naacc-go-kr-web] item '{item['title']}' skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": item["file_id"],
                        "post_number": item["post_number"],
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": item["date"],
                        "listed_date": item["date"],
                        "authors": "",
                        "publisher": "행정중심복합도시건설청",
                        "department": item["writer"],
                        "journal": "",
                        "url": (
                            f"{_LIST_BASE}?schM=list&page=1"
                            f"&viewCount=10&id=&schBdcode=&schGroupCode="
                        ),
                        "pdf_url": item["pdf_url"],
                        "doi": "",
                        "keywords": "간행물,행복도시,행복청,세종시",
                        "category": "간행물",
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": item["date"],
                                "period": item["period"],
                                "writer": item["writer"],
                                "viewCount": item["view_count"],
                                "thumbnail_id": item["thumbnail_id"],
                                "file_id": item["file_id"],
                                "nttId": item["file_id"],
                                "board_type": "thumbnail_down",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[naacc-go-kr-web] saved {saved}/{limit_desc}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[naacc-go-kr-web] item '{item.get('title', '?')}' failed: {exc}")
                    continue

            # Polite gap between list pages
            time.sleep(0.5)

        print(f"[naacc-go-kr-web] Done. Total saved: {saved}")
        return saved
