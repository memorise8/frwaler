# -*- coding: utf-8 -*-
"""한국고용정보원 고용DB분석자료 crawler.

List:   https://www.keis.or.kr/keis/ko/bbs/123/list.do?searchCl1=1
Detail: https://www.keis.or.kr/keis/ko/bbs/123/detail.do?pstSn=N
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, unquote

sys.path.insert(0, __file__[:__file__.rindex('/crawler/')+1] if '/crawler/' in __file__ else '.')

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.keis.or.kr"
_LIST_URL = _BASE + "/keis/ko/bbs/123/list.do"
_DETAIL_PREFIX = _BASE + "/keis/ko/bbs/123/"
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _make_soup(html_text):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html_text, parser)
        except Exception:
            continue
    return None


def _strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    for ent, ch in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
                    ("&amp;", "&"), ("&quot;", '"')):
        s = s.replace(ent, ch)
    s = re.sub(r"&[a-zA-Z]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_date(raw):
    """YYYY.MM.DD or YYYY-MM-DD → YYYY-MM-DD; empty string if unrecognised."""
    if not raw:
        return ""
    d = re.sub(r"[./]", "-", raw.strip())
    return d if re.match(r"\d{4}-\d{2}-\d{2}", d) else ""


class KEISCrawler(BaseCrawler):
    site_id = "keis-or-kr-keis"
    site_name = "Custom: keis-or-kr-keis"
    base_url = _BASE

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}")
            wait = [1, 3, 9][attempt]
            if attempt < 2:
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > _MAX_SEC:
                print(f"[{self.site_id}] Time budget exceeded ({elapsed/60:.1f} min). Stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # --- Fetch list page ---
            list_url = (
                f"{_LIST_URL}?pageIndex={page}&searchCl1=1"
                f"&pageItm={_PAGE_SIZE}&searchOrderSort=0&searchGbn=0"
            )
            list_html = self._curl_get(list_url)
            if not list_html:
                print(f"[{self.site_id}] List page {page} unreachable. Stopping.")
                break

            list_soup = _make_soup(list_html)
            if not list_soup:
                print(f"[{self.site_id}] List page {page} parse failed. Stopping.")
                break

            rows = list_soup.select("table tbody tr")
            if not rows:
                print(f"[{self.site_id}] No rows on page {page}. Done.")
                break

            # Parse list rows
            page_items = []
            for row in rows:
                try:
                    link_tag = row.select_one("td.cell-subject a")
                    if not link_tag:
                        continue
                    href = link_tag.get("href", "")
                    if not href:
                        continue
                    detail_url = urljoin(_DETAIL_PREFIX, href)
                    if detail_url in seen_urls:
                        continue

                    m = re.search(r"pstSn=(\d+)", href)
                    pst_sn = m.group(1) if m else None

                    date_td = row.select_one("td.cell-last")
                    listed_date = _norm_date(date_td.get_text(strip=True) if date_td else "")

                    no_td = row.select_one("td.cell-no")
                    list_no = no_td.get_text(strip=True) if no_td else ""

                    page_items.append({
                        "title": link_tag.get_text(strip=True),
                        "detail_url": detail_url,
                        "pst_sn": pst_sn,
                        "list_no": list_no,
                        "listed_date": listed_date,
                    })
                except Exception as exc:
                    print(f"[{self.site_id}] Row parse error page {page}: {exc}")
                    continue

            new_items = [i for i in page_items if i["detail_url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] Page {page}: no new items. Done.")
                break

            # --- Fetch detail for each new item ---
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(item["detail_url"])

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(item["detail_url"])
                    if not detail_html:
                        print(f"[{self.site_id}] item {item['pst_sn']} failed: empty response")
                        continue

                    detail_soup = _make_soup(detail_html)
                    if not detail_soup:
                        print(f"[{self.site_id}] item {item['pst_sn']} failed: parse error")
                        continue

                    # Title
                    title_tag = detail_soup.select_one("h3.article-subject")
                    title = title_tag.get_text(strip=True) if title_tag else item["title"]

                    # 등록일 / 등록자 from article-info table
                    pub_date = ""
                    author = ""
                    for tr in detail_soup.select("div.article-info table tbody tr"):
                        cells = tr.find_all(["th", "td"])
                        texts = [c.get_text(strip=True) for c in cells]
                        for idx, t in enumerate(texts):
                            if t == "등록일" and idx + 1 < len(texts):
                                pub_date = _norm_date(texts[idx + 1])
                            if t == "등록자" and idx + 1 < len(texts):
                                author = texts[idx + 1]

                    if not pub_date:
                        pub_date = item["listed_date"]

                    # Abstract from se-contents
                    content_div = detail_soup.select_one("div.se-contents")
                    abstract = _strip_tags(str(content_div)) if content_div else ""

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping (short abstract "
                            f"{len(abstract)} chars): {title[:60]}"
                        )
                        continue

                    # Attached files — prefer first PDF
                    pdf_url = None
                    original_filename = None
                    all_files = []
                    for li in detail_soup.select("ul.attfile-list li"):
                        fname_span = li.select_one("span.file-name")
                        fname = fname_span.get_text(strip=True) if fname_span else ""
                        dl_link = li.select_one('a[href*="download.do"]')
                        if dl_link:
                            dl_href = dl_link.get("href", "").replace("&amp;", "&")
                            dl_url = urljoin(_BASE, dl_href)
                            all_files.append({"name": fname, "url": dl_url})
                            if fname.lower().endswith(".pdf") and pdf_url is None:
                                pdf_url = dl_url
                                # Extract original filename from fn= param
                                fn_m = re.search(r"[?&]fn=([^&]+)", dl_href)
                                original_filename = (
                                    unquote(fn_m.group(1), encoding="utf-8")
                                    if fn_m else fname
                                )

                    paper = {
                        "site_id": self.site_id,
                        "external_id": item["pst_sn"],
                        "post_number": item["pst_sn"],
                        "title": title,
                        "abstract": abstract,
                        "authors": author,
                        "publisher": "한국고용정보원",
                        "published_date": pub_date,
                        "posted_date": item["listed_date"],
                        "url": item["detail_url"],
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": "",
                        "category": "고용DB분석자료",
                        "doi": None,
                        "department": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": item["listed_date"],
                                "pstSn": item["pst_sn"],
                                "listNo": item["list_no"],
                                "attachedFiles": all_files,
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
                    print(f"[{self.site_id}] item {item.get('pst_sn', '?')} failed: {exc}")
                    continue

            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {_MAX_PAGES} pages.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
