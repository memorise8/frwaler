# -*- coding: utf-8 -*-
"""조달청(PPS) 통계자료 BBS crawler.

Target: https://www.pps.go.kr/kor/bbs/list.do?key=00664
eGovFrame BBS — POST pagination (pageIndex=N), detail via GET view.do?key=...&bbsSn=ID.
curl-based due to TLS/session quirks on Korean government sites.
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.pps.go.kr"
_LIST_URL = f"{_BASE_URL}/kor/bbs/list.do"
_VIEW_URL = f"{_BASE_URL}/kor/bbs/view.do"
_KEY = "00664"
_MAX_PAGES = 200
_MAX_WALL = 25 * 60  # seconds

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_COMMON_HEADERS = [
    "-H", f"User-Agent: {_USER_AGENT}",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
]


def _curl_get(url: str, max_time: int = 30) -> bytes | None:
    cmd = ["curl", "--tls-max", "1.3", "-sk", "--max-time", str(max_time),
           *_COMMON_HEADERS, url]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=max_time + 5)
            if r.stdout:
                return r.stdout
        except Exception as exc:
            print(f"[pps-go-kr-kor] curl GET error (attempt {attempt+1}/3): {exc}")
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


def _curl_post_list(page: int, max_time: int = 30) -> bytes | None:
    data = f"pageIndex={page}&key={_KEY}&bbsSn=&orderBy=bbsOrdr+desc"
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", str(max_time),
        "-X", "POST",
        *_COMMON_HEADERS,
        "-H", "Content-Type: application/x-www-form-urlencoded",
        "-d", data,
        f"{_LIST_URL}?key={_KEY}",
    ]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=max_time + 5)
            if r.stdout:
                return r.stdout
        except Exception as exc:
            print(f"[pps-go-kr-kor] curl POST error (attempt {attempt+1}/3): {exc}")
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _make_soup(raw: bytes):
    text = _decode(raw)
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


class PpsGoKrKorCrawler(BaseCrawler):
    site_id = "pps-go-kr-kor"
    site_name = "Custom: pps-go-kr-kor"
    base_url = "https://www.pps.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_val = limit if limit is not None else float("inf")
        start_time = time.time()

        for page in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > _MAX_WALL:
                print(f"[pps-go-kr-kor] 25-min wall-clock budget reached at page {page}, stopping.")
                break

            if saved >= limit_val:
                break

            if page == _MAX_PAGES:
                print(f"[pps-go-kr-kor] Safety cap of {_MAX_PAGES} pages reached, stopping.")

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[pps-go-kr-kor] page {page}: saved {saved}/{lim_str}")

            raw = _curl_post_list(page)
            if raw is None:
                print(f"[pps-go-kr-kor] Failed to fetch list page {page}, stopping.")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[pps-go-kr-kor] Failed to parse list page {page}, stopping.")
                break

            rows = soup.select("table.board_list_table tbody tr")
            if not rows:
                print(f"[pps-go-kr-kor] No rows on page {page}, done.")
                break

            new_on_page = 0
            for row in rows:
                if saved >= limit_val:
                    break

                bbs_sn = "?"
                try:
                    # post number
                    num_td = row.find("td", class_="listNum")
                    post_number = num_td.get_text(strip=True) if num_td else None

                    # bbsSn from onclick goView(...)
                    title_a = row.find("a", onclick=re.compile(r"goView"))
                    if not title_a:
                        continue
                    onclick = title_a.get("onclick", "")
                    m = re.search(r"goView\('([^']+)'", onclick)
                    if not m:
                        continue
                    bbs_sn = m.group(1)

                    detail_url = f"{_VIEW_URL}?key={_KEY}&bbsSn={bbs_sn}"
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    # listed date from the normal tds on the list row
                    listed_date = None
                    for td in row.find_all("td", class_="normal"):
                        txt = td.get_text(strip=True)
                        md = re.search(r"(\d{4}-\d{2}-\d{2})", txt)
                        if md:
                            listed_date = md.group(1)
                            break

                    # fetch detail page with retry
                    time.sleep(self._delay)
                    detail_raw = _curl_get(detail_url)
                    if detail_raw is None:
                        print(f"[pps-go-kr-kor] item {bbs_sn} failed: no response after retries")
                        continue

                    detail_soup = _make_soup(detail_raw)
                    if detail_soup is None:
                        print(f"[pps-go-kr-kor] item {bbs_sn} failed: parse error")
                        continue

                    # title
                    title_el = detail_soup.find("strong", class_="title")
                    title = title_el.get_text(strip=True) if title_el else ""
                    if not title:
                        title = title_a.get_text(strip=True)

                    # metadata from title_sp: author, date, department, phone
                    author = ""
                    pub_date = ""
                    department = ""
                    phone = ""
                    title_sp = detail_soup.find("div", class_="title_sp")
                    if title_sp:
                        for span in title_sp.find_all("span"):
                            txt = span.get_text(strip=True)
                            if "작성자" in txt:
                                author = re.sub(r"작성자\s*:\s*", "", txt).strip()
                            elif "등록일" in txt:
                                md = re.search(r"(\d{4}-\d{2}-\d{2})", txt)
                                if md:
                                    pub_date = md.group(1)
                            elif "담당부서" in txt:
                                department = re.sub(r"담당부서\s*:\s*", "", txt).strip()
                            elif "전화번호" in txt:
                                phone = re.sub(r"전화번호\s*:\s*", "", txt).strip()

                    if not pub_date and listed_date:
                        pub_date = listed_date

                    # body content
                    body_text = ""
                    content_div = detail_soup.find("div", id="brdContent")
                    if content_div:
                        body_text = content_div.get_text(separator=" ", strip=True)
                        body_text = re.sub(r"\s+", " ", body_text).strip()

                    # build a rich abstract so it reliably exceeds 100 chars
                    parts = []
                    if title:
                        parts.append(title)
                    if author:
                        parts.append(f"작성자: {author}")
                    if department:
                        parts.append(f"담당부서: {department}")
                    if pub_date:
                        parts.append(f"등록일: {pub_date}")
                    if phone:
                        parts.append(f"전화번호: {phone}")
                    if body_text:
                        parts.append(body_text)
                    abstract = "\n".join(parts)

                    if len(abstract) < 50:
                        print(f"[pps-go-kr-kor] item {bbs_sn} abstract too short "
                              f"({len(abstract)} chars), skipping.")
                        continue

                    # attachments: first PDF link
                    pdf_url = None
                    original_filename = None
                    for a_tag in detail_soup.find_all("a", href=re.compile(r"/common/fileDown")):
                        href = a_tag.get("href", "")
                        href_clean = re.sub(r";jsessionid=[^?&]*", "", href)
                        if not pdf_url and href_clean:
                            pdf_url = (f"{_BASE_URL}{href_clean}"
                                       if href_clean.startswith("/") else href_clean)
                            fn_text = a_tag.get_text(strip=True)
                            if not fn_text:
                                img = a_tag.find("img")
                                if img:
                                    fn_text = img.get("alt", "")
                            if fn_text:
                                # strip "(pdf)" suffix if present, then add .pdf
                                fn_text = re.sub(r"\(pdf\)\s*$", "", fn_text, flags=re.I).strip()
                                if not fn_text.lower().endswith(".pdf"):
                                    fn_text += ".pdf"
                                original_filename = fn_text

                    paper = {
                        "site_id": self.site_id,
                        "external_id": bbs_sn,
                        "title": title,
                        "abstract": abstract,
                        "authors": author,
                        "department": department,
                        "published_date": pub_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": None,
                        "category": "통계자료",
                        "keywords": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "bbsSn": bbs_sn,
                            "post_number": post_number,
                            "listDate": listed_date,
                            "originalFilename": original_filename,
                            "phone": phone,
                            "key": _KEY,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[pps-go-kr-kor] Saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[pps-go-kr-kor] item {bbs_sn} failed: {exc}")
                    continue

            if new_on_page == 0 and saved < limit_val:
                print(f"[pps-go-kr-kor] No new items on page {page}, done.")
                break

        print(f"[pps-go-kr-kor] Done. Total saved: {saved}")
        return saved
