# -*- coding: utf-8 -*-
"""국무조정실 국무총리비서실 보도자료 crawler (opm.go.kr/opm/news/press-release.do)."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.opm.go.kr"
_LIST_URL = f"{_BASE}/opm/news/press-release.do"
_PAGE_SIZE = 10
_SAFETY_CAP = 200
_WALL_MINUTES = 25


def _make_bs(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, params: dict | None = None) -> str | None:
    """GET via curl with Korean-gov TLS workaround; retry 3× with backoff."""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{url}?{qs}"
    else:
        full_url = url

    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                 "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                 full_url],
                capture_output=True, timeout=35,
            )
            raw = result.stdout.decode("utf-8", errors="replace").strip()
            if raw:
                return raw
            if attempt < 2:
                wait = 1 * (3 ** attempt)
                print(f"[opm-go-kr-opm] empty response, retry {attempt+1}/3 in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < 2:
                wait = 1 * (3 ** attempt)
                print(f"[opm-go-kr-opm] curl error: {exc}, retry {attempt+1}/3 in {wait}s")
                time.sleep(wait)
            else:
                print(f"[opm-go-kr-opm] curl failed after 3 attempts: {exc}")
    return None


def _strip(tag) -> str:
    if tag is None:
        return ""
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()


class OpmGoKrOpmCrawler(BaseCrawler):
    """Crawler for 국무조정실 국무총리비서실 보도자료."""

    site_id = "opm-go-kr-opm"
    site_name = "Custom: opm-go-kr-opm"
    base_url = "https://www.opm.go.kr"

    # ------------------------------------------------------------------
    # List-page helpers
    # ------------------------------------------------------------------

    def _fetch_list_page(self, offset: int) -> str | None:
        return _curl_get(_LIST_URL, {
            "mode": "list",
            "article.offset": str(offset),
            "articleLimit": str(_PAGE_SIZE),
        })

    def _parse_list_page(self, raw: str) -> list[dict]:
        """Return list of {article_no, title, dept, date} dicts."""
        items = []
        soup = _make_bs(raw)
        if soup is None:
            return items
        tbody = soup.find("tbody")
        if not tbody:
            return items
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            # td[1]: title cell contains the link
            title_td = tds[1]
            link = title_td.find("a", class_="c-board-title")
            if not link:
                continue
            href = link.get("href", "")
            ano_m = re.search(r"articleNo=(\d+)", href)
            if not ano_m:
                continue
            article_no = ano_m.group(1)
            title = _strip(link)

            # td[2]: department (plain text, sometimes has whitespace)
            dept = _strip(tds[2]) if len(tds) > 2 else ""

            # td[3]: date  (2026.05.06 — may have whitespace wrapper)
            date_raw = _strip(tds[3]) if len(tds) > 3 else ""
            date_m = re.search(r"(\d{4}\.\d{2}\.\d{2})", date_raw)
            date_str = date_m.group(1) if date_m else ""

            items.append({
                "article_no": article_no,
                "title": title,
                "dept": dept,
                "date": date_str,
            })
        return items

    def _get_last_offset(self, raw: str) -> int | None:
        """Extract the last-page offset from the pagination footer."""
        m = re.search(r'page-last[^>]*href="[^"]*article\.offset=(\d+)"', raw)
        if m:
            return int(m.group(1))
        return None

    # ------------------------------------------------------------------
    # Detail-page helpers
    # ------------------------------------------------------------------

    def _fetch_detail(self, article_no: str) -> str | None:
        return _curl_get(_LIST_URL, {
            "mode": "view",
            "articleNo": article_no,
            "article.offset": "0",
            "articleLimit": str(_PAGE_SIZE),
        })

    def _parse_detail(self, raw: str, article_no: str, list_title: str,
                      list_dept: str, list_date: str) -> dict | None:
        """Return a paper dict from the detail page, or None to skip."""
        soup = _make_bs(raw)
        if soup is None:
            return None

        # Title from <h4>
        h4 = soup.find("h4")
        title = _strip(h4) if h4 else list_title
        if not title:
            title = list_title

        # Date from board-etc-wrap
        date_str = list_date
        for li in soup.select(".board-etc-wrap li"):
            txt = _strip(li)
            m = re.search(r"(\d{4}\.\d{2}\.\d{2})", txt)
            if m:
                date_str = m.group(1)
                break
        published_date = date_str.replace(".", "-") if date_str else ""

        # Dept: prefer list value (not shown on detail page)
        dept = list_dept

        # Attached files
        file_names = []
        pdf_url = ""
        for wrap in soup.select(".board-view-file-wrap li"):
            a = wrap.find("a", href=re.compile(r"mode=download"))
            if a:
                fname = _strip(a)
                if fname:
                    file_names.append(fname)
                if not pdf_url and fname.lower().endswith(".pdf"):
                    href = a.get("href", "")
                    if href.startswith("?"):
                        pdf_url = f"{_LIST_URL}{href}"
                    else:
                        pdf_url = f"{_BASE}{href}" if href.startswith("/") else href

        # Body content
        body_text = ""
        body_div = soup.select_one(".board-view-txt")
        if body_div:
            body_text = _strip(body_div)
            # Remove duplicate of title from body text
            if body_text == title:
                body_text = ""

        # Build rich abstract
        parts = [title]
        meta_lines = []
        if dept:
            meta_lines.append(f"담당부서: {dept}")
        if date_str:
            meta_lines.append(f"등록일: {date_str}")
        detail_url = f"{_LIST_URL}?mode=view&articleNo={article_no}&article.offset=0&articleLimit=10"
        meta_lines.append(f"원문: {detail_url}")
        if meta_lines:
            parts.append("\n".join(meta_lines))
        if file_names:
            parts.append("첨부파일: " + " | ".join(file_names))
        if body_text:
            parts.append(body_text)
        abstract = "\n\n".join(parts)

        if len(abstract) < 50:
            print(f"[opm-go-kr-opm] skip articleNo={article_no}: abstract too short ({len(abstract)} chars)")
            return None

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": article_no,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": dept,
            "metadata": json.dumps({
                "article_no": article_no,
                "dept": dept,
                "file_names": file_names,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_label = str(limit) if limit is not None else "∞"

        offset = 0
        page_num = 0
        last_offset: int | None = None

        while True:
            # Wall-clock budget
            elapsed = (time.monotonic() - start_time) / 60
            if elapsed >= _WALL_MINUTES:
                print(f"[opm-go-kr-opm] wall-clock budget ({_WALL_MINUTES}m) reached at page {page_num}. Exiting.")
                break

            # Safety cap
            if page_num >= _SAFETY_CAP:
                print(f"[opm-go-kr-opm] safety cap of {_SAFETY_CAP} pages reached. Exiting.")
                break

            # Limit check
            if limit is not None and saved >= limit:
                break

            # Fetch list page
            raw_list = self._fetch_list_page(offset)
            if not raw_list:
                print(f"[opm-go-kr-opm] failed to fetch list page at offset={offset}. Stopping.")
                break

            # Discover total on first page
            if page_num == 0:
                last_offset = self._get_last_offset(raw_list)
                if last_offset is not None:
                    total_est = last_offset + _PAGE_SIZE
                    print(f"[opm-go-kr-opm] estimated total records: ~{total_est}")

            items = self._parse_list_page(raw_list)
            if not items:
                print(f"[opm-go-kr-opm] page {page_num+1} (offset={offset}): no items found. Done.")
                break

            # Dedup check — detect looping paginator
            first_url = f"{_LIST_URL}?mode=view&articleNo={items[0]['article_no']}"
            if first_url in seen_urls:
                print(f"[opm-go-kr-opm] paginator loop detected at offset={offset}. Stopping.")
                break

            page_num += 1
            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                article_no = item["article_no"]
                detail_url = f"{_LIST_URL}?mode=view&articleNo={article_no}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                time.sleep(self._delay)

                try:
                    raw_detail = self._fetch_detail(article_no)
                    if not raw_detail:
                        print(f"[opm-go-kr-opm] failed to fetch detail for articleNo={article_no}, skipping.")
                        continue

                    paper = self._parse_detail(
                        raw_detail, article_no,
                        item["title"], item["dept"], item["date"]
                    )
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[opm-go-kr-opm] saved {saved}/{limit_label}: {paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[opm-go-kr-opm] item articleNo={article_no} failed: {exc}")
                    continue

            if page_num % 10 == 0:
                print(f"[opm-go-kr-opm] page {page_num}: saved {saved}/{limit_label}")

            if new_on_page == 0:
                print(f"[opm-go-kr-opm] all items on offset={offset} already seen. Done.")
                break

            # Check if we've reached the last page
            if last_offset is not None and offset >= last_offset:
                print(f"[opm-go-kr-opm] reached last page (offset={offset}). Done.")
                break

            offset += _PAGE_SIZE

        print(f"[opm-go-kr-opm] Done. Total saved: {saved}")
        return saved
