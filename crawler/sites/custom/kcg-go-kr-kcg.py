# -*- coding: utf-8 -*-
"""Korea Coast Guard (해양경찰청) 보도자료 crawler.

Board: https://www.kcg.go.kr/kcg/na/ntt/selectNttList.do?mi=2799&bbsId=313

List page  : POST /kcg/na/ntt/selectNttList.do  (form fields below)
Detail page: GET  /kcg/na/ntt/selectNttInfo.do?nttSn=XXXXX
File check : GET  /kcg/na/ntt/fileDownChk.do?bbsId=313&mi=2799&nttSn=XXXXX
File DL    : GET  /common/nttFileDownload.do?fileKey=XXXXX
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.kcg.go.kr"
_LIST_URL = f"{_BASE}/kcg/na/ntt/selectNttList.do"
_DETAIL_BASE = f"{_BASE}/kcg/na/ntt/selectNttInfo.do"
_FILE_DL_BASE = f"{_BASE}/common/nttFileDownload.do"

_BBS_ID = "313"
_MI = "2799"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_CURL_BASE = [
    "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
    "-H", f"User-Agent: {_UA}",
    "-H", "Accept-Language: ko-KR,ko;q=0.9",
    "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
    "-H", f"Referer: {_LIST_URL}",
]


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str) -> bytes | None:
    """GET with TLS workaround; 3 retries with exponential backoff."""
    cmd = _CURL_BASE + [url]
    for attempt in range(3):
        if attempt:
            time.sleep(3 ** attempt)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            pass
    return None


def _curl_post(url: str, data: dict) -> bytes | None:
    """POST with TLS workaround; 3 retries with exponential backoff."""
    form = "&".join(f"{k}={v}" for k, v in data.items())
    cmd = _CURL_BASE + [
        "-X", "POST",
        "-H", "Content-Type: application/x-www-form-urlencoded",
        "-d", form,
        url,
    ]
    for attempt in range(3):
        if attempt:
            time.sleep(3 ** attempt)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(raw):
    """html5lib → lxml → html.parser fallback; returns None on total failure."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


def _strip_html(tag) -> str:
    """Strip all HTML tags from a bs4 Tag or string, normalize whitespace."""
    if tag is None:
        return ""
    if hasattr(tag, "get_text"):
        text = tag.get_text(separator=" ")
    else:
        text = re.sub(r"<[^>]+>", " ", str(tag))
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_date(raw: str) -> str | None:
    """YYYY.MM.DD or YYYY-MM-DD → ISO YYYY-MM-DD. Returns None if no match."""
    if not raw:
        return None
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KcgGoKrKcgCrawler(BaseCrawler):
    site_id = "kcg-go-kr-kcg"
    site_name = "Custom: kcg-go-kr-kcg"
    base_url = "https://www.kcg.go.kr"

    def crawl(self, limit=None):
        saved = 0
        limit_or_inf = limit if limit is not None else float("inf")
        seen_urls: set = set()
        start_time = time.time()

        MAX_PAGES = 200
        MAX_MINUTES = 25

        page = 1
        while True:
            # Budget checks
            if (time.time() - start_time) / 60 >= MAX_MINUTES:
                print(f"[{self.site_id}] wall-clock budget {MAX_MINUTES}m reached, stopping")
                break
            if page > MAX_PAGES:
                print(f"[{self.site_id}] safety cap {MAX_PAGES} pages reached, stopping")
                break
            if saved >= limit_or_inf:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            raw = _curl_post(_LIST_URL, {
                "currPage": str(page),
                "bbsId": _BBS_ID,
                "mi": _MI,
                "listUseAt": "Y",
                "minSn": "0",
                "maxSn": "10",
                "useAt": "Y",
                "noticeAt": "Y",
            })
            if not raw:
                print(f"[{self.site_id}] page {page}: list fetch failed, stopping")
                break

            soup = _make_soup(raw)
            if not soup:
                print(f"[{self.site_id}] page {page}: parse failed, stopping")
                break

            rows = _parse_list_rows(soup)
            if not rows:
                print(f"[{self.site_id}] page {page}: no rows, end of pagination")
                break

            new_this_page = 0
            for row in rows:
                if saved >= limit_or_inf:
                    break

                url = row["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_this_page += 1

                try:
                    ok = self._process_item(row)
                    if ok:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {row.get('ntt_sn')} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if new_this_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen, stopping")
                break

            page += 1

        print(f"[{self.site_id}] done: saved={saved}")
        return saved

    def _process_item(self, row: dict) -> bool:
        url = row["url"]
        ntt_sn = row["ntt_sn"]

        raw = _curl_get(url)
        if not raw:
            print(f"[{self.site_id}] detail fetch failed for nttSn={ntt_sn}")
            return False

        soup = _make_soup(raw)
        if not soup:
            print(f"[{self.site_id}] detail parse failed for nttSn={ntt_sn}")
            return False

        # --- Title (authoritative from detail page) ---
        title = row["title"]
        title_th = soup.find("th", class_="title")
        if title_th:
            t = title_th.get_text(strip=True)
            if t:
                title = t

        # --- Author ---
        author = row.get("author") or ""
        auth_th = soup.find("th", string=lambda s: s and "작성자" in s)
        if auth_th:
            auth_td = auth_th.find_next_sibling("td")
            if auth_td:
                author = auth_td.get_text(strip=True)

        # --- Date ---
        date_raw = row.get("date") or ""
        date_th = soup.find("th", string=lambda s: s and "등록일" in s)
        if date_th:
            date_td = date_th.find_next_sibling("td")
            if date_td:
                date_raw = date_td.get_text(strip=True)
        published_date = _parse_date(date_raw)

        # --- Abstract (content body) ---
        abstract = ""
        # The content body is in a td with style containing word-break
        content_td = soup.find("td", style=re.compile(r"word-break"))
        if content_td:
            abstract = _strip_html(content_td)
        if len(abstract) < 50:
            # Fallback: any large td with colspan=4
            for td in soup.find_all("td", attrs={"colspan": "4"}):
                text = _strip_html(td)
                if len(text) > len(abstract):
                    abstract = text
        if len(abstract) < 50:
            # Last-resort: look for div with content class
            main = soup.find("div", class_=re.compile(r"BD_table|sub_content"))
            if main:
                abstract = _strip_html(main)

        abstract = re.sub(r"\s+", " ", abstract).strip()

        if len(abstract) < 50:
            print(f"[{self.site_id}] abstract too short ({len(abstract)} chars), skipping nttSn={ntt_sn}")
            return False
        if len(abstract) < 100:
            print(f"[{self.site_id}] abstract too short ({len(abstract)} chars), skipping nttSn={ntt_sn}")
            return False

        # --- Attachments ---
        pdf_url = None
        original_filename = None
        file_entries = []

        file_ul = soup.find("ul", class_="file")
        if file_ul:
            for a in file_ul.find_all("a", href=True):
                href = a["href"].strip()
                if "nttFileDownload" not in href and "fileKey" not in href:
                    continue
                fname = a.get_text(strip=True)
                full = f"{_BASE}{href}" if href.startswith("/") else href
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                file_entries.append({"name": fname, "url": full, "ext": ext})

        # Prefer PDF attachment; fall back to first available file
        for fe in file_entries:
            if fe["ext"] == "pdf":
                pdf_url = fe["url"]
                original_filename = fe["name"]
                break
        if not pdf_url and file_entries:
            pdf_url = file_entries[0]["url"]
            original_filename = file_entries[0]["name"]

        # --- Metadata ---
        metadata = {
            "nttSn": ntt_sn,
            "posted_date": date_raw,
            "post_number": row.get("post_number"),
        }
        if file_entries:
            metadata["files"] = [{"name": fe["name"], "url": fe["url"]} for fe in file_entries]

        self._save_paper({
            "site_id": self.site_id,
            "external_id": ntt_sn,
            "title": title,
            "abstract": abstract,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "published_date": published_date,
            "listed_date": published_date,
            "posted_date": date_raw,
            "authors": author or None,
            "publisher": "해양경찰청",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        })
        return True


# ---------------------------------------------------------------------------
# List-page parser (module-level for testability)
# ---------------------------------------------------------------------------

def _parse_list_rows(soup) -> list:
    """Extract item stubs from a list-page soup. Returns list of dicts."""
    rows = []
    tbody = soup.find("tbody")
    if not tbody:
        return rows

    for tr in tbody.find_all("tr"):
        # Must have a title link
        link_td = tr.find("td", class_="ta_l")
        if not link_td:
            continue
        a = link_td.find("a", href=True)
        if not a:
            continue

        href = a["href"].strip()
        m = re.search(r"nttSn=(\d+)", href)
        if not m:
            continue
        ntt_sn = m.group(1)

        title = re.sub(r"\s+", " ", a.get_text(separator=" ", strip=True)).strip()
        full_url = f"{_BASE}{href}" if href.startswith("/") else href

        all_tds = tr.find_all("td")

        # Post number: first BD_tm_none td
        post_number = None
        bd_tds = tr.find_all("td", class_="BD_tm_none")
        if bd_tds:
            post_number = bd_tds[0].get_text(strip=True)

        # Date: td whose text matches YYYY.MM.DD
        date_str = None
        for td in all_tds:
            t = td.get_text(strip=True)
            if re.match(r"\d{4}\.\d{2}\.\d{2}$", t):
                date_str = t
                break

        # Author: td immediately after the title td (link_td)
        author = None
        try:
            idx = all_tds.index(link_td)
            if idx + 1 < len(all_tds):
                cand = all_tds[idx + 1].get_text(strip=True)
                if cand and not re.match(r"\d{4}\.\d{2}\.\d{2}", cand):
                    author = cand
        except ValueError:
            pass

        has_file = bool(tr.find("a", class_="listFileDown"))

        rows.append({
            "ntt_sn": ntt_sn,
            "post_number": post_number,
            "title": title,
            "author": author,
            "date": date_str,
            "url": full_url,
            "has_file": has_file,
        })

    return rows
