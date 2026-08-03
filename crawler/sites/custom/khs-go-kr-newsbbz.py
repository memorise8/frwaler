# -*- coding: utf-8 -*-
"""국가유산청 보도/설명 게시판 crawler.

Starting URL: https://www.khs.go.kr/newsBbz/selectNewsBbzList.do?sectionId=all_sec_1&mn=NS_01_02
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, __file__.split("/crawler/")[0])
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.khs.go.kr"
_LIST_URL = _BASE + "/newsBbz/selectNewsBbzList.do"
_DETAIL_URL = _BASE + "/newsBbz/selectNewsBbzView.do"


def _try_bs4(content):
    """Parse HTML with fallback parser chain. Returns BeautifulSoup or None."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(content, parser)
        except Exception:
            continue
    return None


def _strip_tags(html):
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


class KhsGoKrNewsBbzCrawler(BaseCrawler):
    site_id = "khs-go-kr-newsbbz"
    site_name = "Custom: khs-go-kr-newsbbz"
    base_url = _BASE

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None):
        """GET via curl with TLS workaround. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                text = res.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[khs-go-kr-newsbbz] curl error (attempt {attempt+1}): {exc}")
            if attempt < 2:
                wait = (attempt + 1) * 3
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _parse_list(self, content):
        """Return list of item dicts from a list-page HTML."""
        items = []
        soup = _try_bs4(content)
        if soup is None:
            rows_raw = re.findall(
                r'data-column="번호">(\d+)</td>.*?'
                r'href="(/newsBbz/selectNewsBbzView\.do[^"]+)".*?'
                r'<span>(.*?)</span>.*?'
                r'data-column="주관부서">(.*?)</td>.*?'
                r'data-column="등록일">([\d-]+)</td>',
                content, re.S,
            )
            for num, href, title, dept, date in rows_raw:
                id_m = re.search(r'newsItemId=(\d+)', href)
                if not id_m:
                    continue
                items.append({
                    "post_number": num,
                    "news_item_id": id_m.group(1),
                    "title": _strip_tags(title).strip(),
                    "department": _strip_tags(dept).strip(),
                    "date": date.strip(),
                    "href": href,
                })
            return items

        for row in soup.find_all("tr"):
            try:
                num_td = row.find("td", attrs={"data-column": "번호"})
                if not num_td:
                    continue
                post_number = num_td.get_text(strip=True)
                if not post_number.isdigit():
                    continue

                title_td = row.find("td", attrs={"data-column": "제목"})
                if not title_td:
                    continue
                link = title_td.find("a", class_="b_tit")
                if not link:
                    continue
                href = link.get("href", "")
                id_m = re.search(r'newsItemId=(\d+)', href)
                if not id_m:
                    continue
                news_item_id = id_m.group(1)
                span = link.find("span")
                title = span.get_text(strip=True) if span else link.get_text(strip=True)

                dept_td = row.find("td", attrs={"data-column": "주관부서"})
                dept = dept_td.get_text(strip=True) if dept_td else ""

                date_td = row.find("td", attrs={"data-column": "등록일"})
                date_str = date_td.get_text(strip=True) if date_td else ""

                items.append({
                    "post_number": post_number,
                    "news_item_id": news_item_id,
                    "title": title,
                    "department": dept,
                    "date": date_str,
                    "href": href,  # full href including jsessionid
                })
            except Exception:
                continue
        return items

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, content):
        """Return dict: abstract, pdf_url, original_filename, attachments."""
        result = {
            "abstract": "",
            "pdf_url": None,
            "original_filename": None,
            "attachments": [],
        }
        soup = _try_bs4(content)

        if soup:
            vc = soup.find(class_="board-view-content")
            if vc:
                for tag in vc.find_all(["script", "style"]):
                    tag.decompose()
                result["abstract"] = vc.get_text(separator="\n", strip=True)

            # If board-view-content not found, try common fallbacks
            if not result["abstract"]:
                for cls in ("view-content", "bbs_content", "cont_area", "article_content"):
                    el = soup.find(class_=cls)
                    if el:
                        result["abstract"] = el.get_text(separator="\n", strip=True)
                        break

            for dl_link in soup.find_all("a", href=re.compile(r"/newsBbz/FileDown\.do")):
                href = dl_link.get("href", "")
                title_attr = dl_link.get("title", "")
                filename = re.sub(r'\s*다운로드\s*$', '', title_attr).strip()
                fn_m = re.search(r'([^\s/\\]+\.[a-zA-Z0-9]+)\s*$', filename)
                if fn_m:
                    filename = fn_m.group(1)
                full_url = _BASE + href if href.startswith("/") else href
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                result["attachments"].append({"url": full_url, "filename": filename, "ext": ext})
                if ext == "pdf" and result["pdf_url"] is None:
                    result["pdf_url"] = full_url
                    result["original_filename"] = filename
        else:
            # Regex fallback
            vc_m = re.search(r'class="board-view-content"[^>]*>(.*?)</div>', content, re.S)
            if vc_m:
                result["abstract"] = _strip_tags(vc_m.group(1))

            for href, title_attr in re.findall(
                r'href="(/newsBbz/FileDown\.do[^"]+)"[^>]*title="([^"]*)"', content
            ):
                filename = re.sub(r'\s*다운로드\s*$', '', title_attr).strip()
                fn_m = re.search(r'([^\s/\\]+\.[a-zA-Z0-9]+)\s*$', filename)
                if fn_m:
                    filename = fn_m.group(1)
                full_url = _BASE + href
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                result["attachments"].append({"url": full_url, "filename": filename, "ext": ext})
                if ext == "pdf" and result["pdf_url"] is None:
                    result["pdf_url"] = full_url
                    result["original_filename"] = filename

        if not result["original_filename"] and result["attachments"]:
            result["original_filename"] = result["attachments"][0]["filename"]

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_ids = set()
        start_time = time.time()
        limit_label = str(limit) if limit is not None else "∞"
        list_referer = f"{_LIST_URL}?sectionId=all_sec_1&mn=NS_01_02"
        # Detail URL template — all params required by the server to render content.
        _DETAIL_TMPL = (
            _DETAIL_URL
            + "?newsItemId={news_item_id}&sectionId=b_sec_1"
            "&pageIndex={page_index}&pageUnit=10"
            "&strWhere=&strValue=&sdate=&edate=&category=&mn=NS_01_02"
        )

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[khs-go-kr-newsbbz] 25-min budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[khs-go-kr-newsbbz] page {page}: saved {saved}/{limit_label}")

            list_url = f"{_LIST_URL}?sectionId=all_sec_1&mn=NS_01_02&pageIndex={page}"
            content = None
            for attempt in range(3):
                content = self._curl_get(list_url, referer=list_referer)
                if content:
                    break
                wait = (attempt + 1) * 3
                print(f"[khs-go-kr-newsbbz] list page {page} fetch failed, retry in {wait}s")
                time.sleep(wait)

            if not content:
                print(f"[khs-go-kr-newsbbz] Cannot fetch list page {page}. Stopping.")
                break

            items = self._parse_list(content)
            if not items:
                print(f"[khs-go-kr-newsbbz] No items on page {page}. Done.")
                break

            # End-of-pagination: all items already seen (loop-back guard)
            new_items = [it for it in items if it["news_item_id"] not in seen_ids]
            if not new_items:
                print(f"[khs-go-kr-newsbbz] Page {page}: all items already seen. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                news_item_id = item["news_item_id"]
                if news_item_id in seen_ids:
                    continue
                seen_ids.add(news_item_id)

                try:
                    time.sleep(self._delay)

                    # Construct canonical detail URL with all params the server needs.
                    detail_url = _DETAIL_TMPL.format(
                        news_item_id=news_item_id,
                        page_index=page,
                    )

                    detail_content = None
                    for attempt in range(3):
                        detail_content = self._curl_get(detail_url, referer=list_url)
                        if detail_content:
                            break
                        wait = (attempt + 1) * 3
                        print(
                            f"[khs-go-kr-newsbbz] detail {news_item_id} "
                            f"retry {attempt+1} in {wait}s"
                        )
                        time.sleep(wait)

                    if not detail_content:
                        print(f"[khs-go-kr-newsbbz] item {news_item_id} failed: no response")
                        continue

                    detail = self._parse_detail(detail_content)
                    abstract = detail["abstract"]

                    if len(abstract) < 50:
                        print(
                            f"[khs-go-kr-newsbbz] item {news_item_id} abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    published_date = item["date"]
                    detail_page_url = detail_url

                    paper = {
                        "site_id": self.site_id,
                        "external_id": news_item_id,
                        "post_number": item["post_number"],
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": "",
                        "publisher": "국가유산청",
                        "department": item["department"],
                        "url": detail_page_url,
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "keywords": "",
                        "category": "보도자료",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "newsItemId": news_item_id,
                                "post_number": item["post_number"],
                                "posted_date": published_date,
                                "originalFilename": detail["original_filename"],
                                "attachments": detail["attachments"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[khs-go-kr-newsbbz] Saved {saved}/{limit_label}: "
                        f"{item['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[khs-go-kr-newsbbz] item {news_item_id} failed: {exc}")
                    continue

        print(f"[khs-go-kr-newsbbz] Done. Total saved: {saved}")
        return saved
