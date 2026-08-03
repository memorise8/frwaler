# -*- coding: utf-8 -*-
"""외교부 뉴포커스 crawler — https://www.mofa.go.kr/www/brd/m_4076/list.do

Absolute import is used throughout (no package context via spec_from_file_location).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup as _BS4
    _BS4_OK = True
except ImportError:
    _BS4_OK = False


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if not _BS4_OK:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MofaGoKrWwwCrawler(BaseCrawler):
    """외교부 뉴포커스 게시판 crawler."""

    site_id = "mofa-go-kr-www"
    site_name = "Custom: mofa-go-kr-www"
    base_url = "https://www.mofa.go.kr"

    _LIST_URL = "https://www.mofa.go.kr/www/brd/m_4076/list.do"
    _VIEW_BASE = "https://www.mofa.go.kr/www/brd/m_4076/view.do"

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, cookie_file: str) -> str | None:
        """GET via curl with TLS workaround + cookie jar.  Returns text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-c", cookie_file, "-b", cookie_file,
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = res.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[mofa-go-kr-www] empty response, retry {attempt+2}/3 in {wait}s ({url[-60:]})")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[mofa-go-kr-www] timeout, retry {attempt+2}/3 in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[mofa-go-kr-www] curl error {exc}, retry {attempt+2}/3 in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[mofa-go-kr-www] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List page parser
    # ------------------------------------------------------------------

    def _parse_list(self, html: str) -> list[dict]:
        """Parse one list page.  Returns list of item dicts."""
        items: list[dict] = []

        # --- BeautifulSoup path ---
        soup = _make_soup(html)
        if soup is not None:
            try:
                tbody = soup.find("tbody")
                if tbody:
                    for tr in tbody.find_all("tr"):
                        tds = tr.find_all("td")
                        if len(tds) < 4:
                            continue
                        # Column order: 번호 | 제목+link | 첨부 | 담당부서 | 등록일
                        post_number = tds[0].get_text(strip=True)

                        a_tag = tds[1].find("a", href=True)
                        if not a_tag:
                            continue
                        title = a_tag.get_text(strip=True)
                        href = a_tag.get("href", "")

                        # seq from href first, onclick fallback
                        seq_m = re.search(r"seq=(\d+)", href)
                        if not seq_m:
                            onclick = a_tag.get("onclick", "")
                            seq_m = re.search(r"f_view\('(\d+)'\)", onclick)
                        if not seq_m:
                            continue
                        seq = seq_m.group(1)

                        dept = tds[3].get_text(strip=True) if len(tds) > 3 else ""
                        listed_date = tds[4].get_text(strip=True) if len(tds) > 4 else ""

                        items.append({
                            "seq": seq,
                            "post_number": post_number,
                            "title": title,
                            "listed_date": listed_date,
                            "department": dept,
                            "url": f"{self._VIEW_BASE}?seq={seq}&page=1",
                        })
                    return items
            except Exception as exc:
                print(f"[mofa-go-kr-www] BS list parse error: {exc} — falling back to regex")

        # --- Regex fallback ---
        # Find each <tr> block containing a board entry link
        for tr_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
            block = tr_m.group(1)

            seq_m = re.search(r"seq=(\d+)", block)
            if not seq_m:
                continue
            seq = seq_m.group(1)

            num_m = re.search(r"<td[^>]*>\s*<div[^>]*>\s*(\d+)\s*</div>", block)
            post_number = num_m.group(1) if num_m else ""

            title_m = re.search(
                r'f_view\([^)]+\);"[^>]*>\s*(.*?)\s*</a>', block, re.DOTALL
            )
            title = _strip_html(title_m.group(1)).strip() if title_m else ""

            date_m = re.search(r"(\d{4}-\d{2}-\d{2})", block)
            listed_date = date_m.group(1) if date_m else ""

            if not title or not seq:
                continue

            items.append({
                "seq": seq,
                "post_number": post_number,
                "title": title,
                "listed_date": listed_date,
                "department": "",
                "url": f"{self._VIEW_BASE}?seq={seq}&page=1",
            })
        return items

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, seq: str) -> dict:
        """Parse detail page.  Returns dict with title/abstract/date/dept/pdf fields."""
        result: dict = {
            "title": "",
            "abstract": "",
            "published_date": "",
            "department": "",
            "pdf_url": None,
            "original_filename": None,
        }

        # --- BeautifulSoup path ---
        soup = _make_soup(html)
        if soup is not None:
            try:
                bo_head = soup.find("div", class_="bo_head")
                if bo_head:
                    h2 = bo_head.find("h2")
                    if h2:
                        result["title"] = h2.get_text(strip=True)

                    dts = bo_head.find_all("dt")
                    dds = bo_head.find_all("dd")
                    for i, dt in enumerate(dts):
                        label = dt.get_text(strip=True)
                        val = dds[i].get_text(strip=True) if i < len(dds) else ""
                        if "담당부서" in label:
                            result["department"] = val
                        elif "등록일" in label:
                            result["published_date"] = re.sub(r"\s+", "", val)

                bo_con = soup.find("div", class_="bo_con")
                if bo_con:
                    se = bo_con.find(class_="se-contents")
                    target = se if se else bo_con
                    result["abstract"] = _strip_html(str(target))

                # Attached files (PDF preferred)
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    lower = href.lower()
                    if any(ext in lower for ext in (".pdf", ".hwp", ".hwpx", "download")):
                        full = href if href.startswith("http") else self.base_url + href
                        if ".pdf" in lower and not result["pdf_url"]:
                            result["pdf_url"] = full
                            fn = href.split("/")[-1].split("?")[0]
                            if fn:
                                result["original_filename"] = fn

                return result
            except Exception as exc:
                print(f"[mofa-go-kr-www] BS detail parse error seq={seq}: {exc} — using regex")

        # --- Regex fallback ---
        h2_m = re.search(
            r'<div[^>]*class="bo_head"[^>]*>.*?<h2[^>]*>(.*?)</h2>',
            html, re.DOTALL,
        )
        if h2_m:
            result["title"] = _strip_html(h2_m.group(1)).strip()

        date_m = re.search(r"등록일.*?(\d{4}-\d{2}-\d{2})", html, re.DOTALL)
        if date_m:
            result["published_date"] = date_m.group(1)

        dept_m = re.search(r"담당부서</dt>\s*<dd>(.*?)</dd>", html, re.DOTALL)
        if dept_m:
            result["department"] = _strip_html(dept_m.group(1)).strip()

        con_m = re.search(
            r'<div[^>]*class="bo_con"[^>]*>(.*?)(?:<!--\s*//에디터|</div>\s*<!--)',
            html, re.DOTALL,
        )
        if con_m:
            result["abstract"] = _strip_html(con_m.group(1))
        else:
            se_m = re.search(r'class="se-contents"[^>]*>(.*?)</div>', html, re.DOTALL)
            if se_m:
                result["abstract"] = _strip_html(se_m.group(1))

        return result

    # ------------------------------------------------------------------
    # crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 뉴포커스 board with full pagination.

        Walks pages until saved >= limit, empty page, pagination loop, 200-page
        safety cap, or 25-minute wall-clock budget.
        """
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        saved = 0
        page = 1
        seen_urls: set[str] = set()
        empty_streak = 0
        limit_str = str(limit) if limit is not None else "∞"

        # Shared cookie jar for the lifetime of this crawl run
        cookie_fd, cookie_file = tempfile.mkstemp(suffix=".txt", prefix="mofa_")
        os.close(cookie_fd)

        try:
            while True:
                # --- stop conditions ---
                if limit is not None and saved >= limit:
                    break

                if page > 200:
                    print(f"[mofa-go-kr-www] Safety cap of 200 pages reached. Stopping.")
                    break

                elapsed = time.time() - start_time
                if elapsed > max_seconds:
                    print(
                        f"[mofa-go-kr-www] 25-min budget exhausted after {saved} items. Stopping."
                    )
                    break

                if page % 10 == 0:
                    print(f"[mofa-go-kr-www] page {page}: saved {saved}/{limit_str}")

                # --- fetch list page ---
                list_url = (
                    f"{self._LIST_URL}?page={page}"
                    "&srchFr=&srchTo=&srchWord=&srchTp="
                    "&multi_itm_seq=0&itm_seq_1=0&itm_seq_2=0"
                    "&company_cd=&company_nm="
                )
                list_html = self._curl_get(list_url, cookie_file)

                if not list_html:
                    empty_streak += 1
                    if empty_streak >= 3:
                        print(f"[mofa-go-kr-www] 3 consecutive list fetch failures. Stopping.")
                        break
                    page += 1
                    continue
                empty_streak = 0

                items = self._parse_list(list_html)
                if not items:
                    print(f"[mofa-go-kr-www] No items at page {page}. Done.")
                    break

                # Detect pagination loop: all URLs already seen
                new_count = sum(1 for i in items if i["url"] not in seen_urls)
                if new_count == 0:
                    print(
                        f"[mofa-go-kr-www] All page {page} items already seen — pagination loop. Stopping."
                    )
                    break

                # --- process each item ---
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    item_url = item["url"]
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)

                    seq = item["seq"]

                    try:
                        time.sleep(self._delay)

                        # Fetch detail with up to 3 retries
                        detail_html = None
                        for attempt in range(3):
                            detail_html = self._curl_get(item_url, cookie_file)
                            if detail_html:
                                break
                            wait = (attempt + 1) * 3
                            print(
                                f"[mofa-go-kr-www] detail seq={seq} attempt {attempt+1}/3,"
                                f" retry in {wait}s"
                            )
                            time.sleep(wait)

                        if not detail_html:
                            print(f"[mofa-go-kr-www] item seq={seq} failed: 3 fetch attempts exhausted")
                            continue

                        detail = self._parse_detail(detail_html, seq)

                        title = detail["title"] or item["title"]
                        abstract = detail["abstract"]
                        published_date = detail["published_date"] or item["listed_date"]
                        listed_date = item["listed_date"]
                        department = detail["department"] or item["department"]

                        # Skip items whose body text is too short
                        if len(abstract) < 50:
                            print(
                                f"[mofa-go-kr-www] seq={seq} skipped: abstract"
                                f" {len(abstract)} chars (<50)"
                            )
                            continue

                        self._save_paper({
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": seq,
                            "post_number": item["post_number"],
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "posted_date": listed_date,
                            "authors": "",
                            "publisher": "외교부",
                            "department": department,
                            "journal": "",
                            "url": item_url,
                            "pdf_url": detail["pdf_url"],
                            "doi": "",
                            "keywords": "",
                            "category": "뉴포커스",
                            "original_filename": detail["original_filename"],
                            "metadata": json.dumps(
                                {
                                    "posted_date": listed_date,
                                    "seq": seq,
                                    "board_id": "m_4076",
                                    "board_number": item["post_number"],
                                },
                                ensure_ascii=False,
                            ),
                        })
                        saved += 1
                        print(f"[mofa-go-kr-www] Saved {saved}/{limit_str}: {title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[mofa-go-kr-www] item seq={seq} failed: {exc}")
                        continue

                page += 1

        finally:
            try:
                os.unlink(cookie_file)
            except Exception:
                pass

        print(f"[mofa-go-kr-www] Done. Total saved: {saved}")
        return saved
