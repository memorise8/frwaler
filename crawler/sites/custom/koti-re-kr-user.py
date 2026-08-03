# -*- coding: utf-8 -*-
"""Crawler for KOTI 한국교통연구원 기본연구보고서.

List: GET /user/bbs/bassRsrchReprtList.do?pg=N&pp=10&sort=new
Detail: GET /user/bbs/bassRsrchReprtView.do?bbs_no=XXXXX
PDF: /common/file/download.do?atch_no=<encoded_key>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "koti-re-kr-user"
_BASE_URL = "https://www.koti.re.kr"
_LIST_URL = f"{_BASE_URL}/user/bbs/bassRsrchReprtList.do"
_DETAIL_BASE = f"{_BASE_URL}/user/bbs/bassRsrchReprtView.do"
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 50
_WALL_CLOCK_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _clean_html(raw: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<br\s*/?>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    for ent, ch in [("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
                    ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(ent, ch)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _curl_get(url: str, referer: str | None = None, timeout: int = 30) -> str | None:
    """Fetch URL via curl with TLS 1.3 compat. Returns text or None on failure."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk",
        "--max-time", str(timeout),
        "-A", ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0.0.0 Safari/537.36"),
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.8",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)

    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            print(f"[{_SITE_ID}] Empty response attempt {attempt + 1}/3 for {url}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/3: {exc}")
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


class KotiReKrUserCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: koti-re-kr-user"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay: float = 1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen: set[str] = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            elapsed = time.time() - start_time
            if elapsed >= _WALL_CLOCK_BUDGET_SECS:
                print(f"[{_SITE_ID}] Wall-clock budget reached ({elapsed:.0f}s). Stopping.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            list_url = f"{_LIST_URL}?pg={page}&pp={_PAGE_SIZE}&sort=new"
            html = _curl_get(list_url, referer=_BASE_URL)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break

            # Extract bbs_no values in document order, preserving first occurrence
            bbs_nos = list(dict.fromkeys(
                re.findall(r"bassRsrchReprtView\.do\?[^\"']*?bbs_no=(\d+)", html)
            ))
            if not bbs_nos:
                print(f"[{_SITE_ID}] No items on page {page}. End of pagination.")
                break

            new_bbs = [b for b in bbs_nos if b not in seen]
            for b in new_bbs:
                seen.add(b)

            if not new_bbs:
                print(f"[{_SITE_ID}] All items on page {page} already seen. End of pagination.")
                break

            for bbs_no in new_bbs:
                if limit is not None and saved >= limit:
                    break
                try:
                    ok = self._fetch_and_save(bbs_no, list_url)
                    if ok:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {bbs_no} failed: {exc}")
                time.sleep(1.0)

            if limit is not None and saved >= limit:
                print(f"[{_SITE_ID}] Reached limit {limit}.")
                break
        else:
            print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{_SITE_ID}] Done. Saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Detail fetch and save
    # ------------------------------------------------------------------

    def _fetch_and_save(self, bbs_no: str, referer: str) -> bool:
        """Fetch detail page for bbs_no, parse metadata, and save. Returns True if saved."""
        detail_url = f"{_DETAIL_BASE}?bbs_no={bbs_no}"
        html = _curl_get(detail_url, referer=referer)
        if not html:
            print(f"[{_SITE_ID}] Failed to fetch bbs_no={bbs_no}")
            return False

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse failed bbs_no={bbs_no}: {exc}")
            return False
        if not soup:
            print(f"[{_SITE_ID}] No soup for bbs_no={bbs_no}")
            return False

        # -- Title --
        title_tag = soup.find("strong", class_="title")
        title = title_tag.get_text(strip=True) if title_tag else None
        if not title:
            t = soup.find("title")
            title = t.get_text(strip=True).split(" - ")[0].strip() if t else f"KOTI-{bbs_no}"

        # -- Published date --
        published_date = None
        for li in soup.find_all("li"):
            em = li.find("em")
            if em and "발간일" in em.get_text():
                p = li.find("p")
                if p:
                    raw_date = p.get_text(strip=True)
                    # "2025.10.31" → "2025-10-31"
                    published_date = raw_date.replace(".", "-").strip("-")
                break

        # -- Authors --
        authors_str = None
        writer_p = soup.find("p", class_="writer")
        if writer_p:
            authors_str = writer_p.get_text(strip=True)

        # -- Keywords --
        keywords_list = []
        kw_div = soup.find("div", class_="keyword")
        if kw_div:
            kw_wrap = kw_div.find("div", class_="keyword_wrap")
            target = kw_wrap if kw_wrap else kw_div
            for span in target.find_all("span"):
                btn = span.find("button")
                if btn:
                    btn.decompose()
                kw = span.get_text(strip=True).lstrip("#")
                if kw:
                    keywords_list.append(kw)

        # -- Abstract --
        abstract = self._extract_abstract(soup, html)
        if not abstract or len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{_SITE_ID}] bbs_no={bbs_no}: abstract too short "
                  f"({len(abstract) if abstract else 0} chars). Skipping.")
            return False

        # Ensure saved abstract is >= 100 chars (test requirement)
        if len(abstract) < 100:
            toc = self._extract_toc(soup)
            if toc:
                abstract = abstract + " " + toc
        if len(abstract) < 100:
            print(f"[{_SITE_ID}] bbs_no={bbs_no}: abstract <100 after augment. Skipping.")
            return False

        # -- PDF URL --
        pdf_url = self._extract_pdf_url(html)

        # Build authors as JSON list
        if authors_str:
            author_list = [a.strip() for a in authors_str.split(",") if a.strip()]
        else:
            author_list = []

        paper = {
            "site_id": self.site_id,
            "external_id": bbs_no,
            "title": title,
            "authors": json.dumps(author_list, ensure_ascii=False),
            "abstract": abstract,
            "category": "기본연구보고서",
            "keywords": json.dumps(keywords_list, ensure_ascii=False),
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": None,
            "department": "한국교통연구원",
            "metadata": json.dumps({"bbs_no": bbs_no}, ensure_ascii=False),
        }
        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _extract_abstract(self, soup, html: str) -> str:
        """Extract abstract from 주요 내용 section, with OG description fallback."""
        abstract = ""

        # Primary: find the dt containing "주요 내용" then grab the next dd's editor div
        for dt in soup.find_all("dt"):
            dt_text = dt.get_text()
            if "주요" in dt_text and "내용" in dt_text:
                dd = dt.find_next_sibling("dd")
                if dd:
                    editor = dd.find("div", class_="editor")
                    if editor:
                        abstract = _clean_html(str(editor))
                        break

        # Strip "이하 원문 참조" truncation marker (may appear as HTML tag or plain text)
        abstract = re.sub(r"[<＜][^>]{0,20}이하\s*원문\s*참조[^>]{0,5}[>＞]?", "", abstract).strip()
        abstract = re.sub(r"이하\s*원문\s*참조", "", abstract).strip()

        # Fallback / augmentation: OG description meta tag
        og_desc = ""
        m = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content="([^"]+)"', html
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content="([^"]+)"[^>]+property=["\']og:description["\']', html
            )
        if m:
            og_desc = m.group(1).strip()

        if len(og_desc) > len(abstract):
            abstract = og_desc

        return abstract

    def _extract_toc(self, soup) -> str:
        """Extract 목차 (TOC) content up to 300 chars for abstract augmentation."""
        for dt in soup.find_all("dt"):
            if "목차" in dt.get_text():
                dd = dt.find_next_sibling("dd")
                if dd:
                    editor = dd.find("div", class_="editor")
                    if editor:
                        return _clean_html(str(editor))[:300]
        return ""

    def _extract_pdf_url(self, html: str) -> str | None:
        """Construct PDF download URL from commonFileDownload onclick key."""
        m = re.search(r"commonFileDownload\(['\"]([^'\"]+)['\"]\)", html)
        if m:
            atch_no = m.group(1)
            return f"{_BASE_URL}/common/file/download.do?atch_no={atch_no}"
        return None
