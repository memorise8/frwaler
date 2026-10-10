# -*- coding: utf-8 -*-
"""KEI 한국환경연구원: all eleven delivered catalogue and board scopes.

Starting URL: https://www.kei.re.kr/elibList.es?mid=a10101010000

List  : GET /elibList.es?mid=a10101010000&act=list&nPage={n}&keyField=&keyWord=
Detail: GET /elibList.es?mid=a10101010000&elibName=researchreport&class_id=
            &act=view&c_id={c_id}&rn={rn}&nPage={n}&keyField=&keyWord=
PDF   : https://library.kei.re.kr/pyxis-api/1/digital-files/{uuid}  (direct link from detail page)
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler
from crawler.kei_traversal import KeiTraversal
from crawler.kei_source import parse_listing, page_number, title_quality
from delivery.health.access_challenge import access_challenge

_BASE = "https://www.kei.re.kr"
_MID = "a10101010000"
_PUBLISHER = "한국환경연구원"


def _bs4(html):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("All BeautifulSoup parsers failed")


def _text(el):
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _norm_date(raw):
    """Normalize 'YYYY-MM-DD' / 'YYYY.MM.DD' / 'YYYYMMDD' -> 'YYYY-MM-DD'."""
    if not raw:
        return None
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m2 = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return None


def _parse_names(raw):
    """Split a ';'/','-separated author string into a list of names."""
    if not raw:
        return []
    return [a.strip() for a in re.split(r"[;,]", raw) if a.strip()]


def _find_abstract(soup):
    """Locate the '요약' tab content; fall back to any non-목차 view div."""
    target_id = None
    for a in soup.select("div.th_contents li a"):
        if _text(a) == "요약":
            href = a.get("href", "")
            if href.startswith("#"):
                target_id = href[1:]
            break

    div = soup.find("div", id=target_id) if target_id else None
    if div is None:
        div = soup.find("div", id="view2")
    if div is None:
        for cand in soup.select("div.tb_contents div.view"):
            if cand.get("id") != "view1":
                div = cand
                break
    if div is None:
        return ""
    return re.sub(r"\s+", " ", div.get_text(" ", strip=True)).strip()


class KeiReKrEliblistesCrawler(KeiTraversal, BaseCrawler):
    """Traverse reviewed KEI scope links with durable backfill checkpoints."""

    site_id = "kei-re-kr-eliblistes"
    site_name = "Custom: kei-re-kr-eliblistes"
    base_url = _BASE

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "0"))  # explicit run budget only; 0 means source bounds
    _TIMEOUT_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute wall-clock budget
    _MIN_ABSTRACT = 50        # skip items with a shorter abstract
    _RATE_SLEEP = 1.0         # seconds between detail fetches

    # ------------------------------------------------------------------
    # Low-level HTTP (curl workaround for Korean gov TLS quirks)
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=""):
        """GET via curl --tls-max 1.3, retrying 3x with 1s/3s/9s backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "--write-out", "\n%{http_code}",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        for attempt in range(3):
            self._check_cancelled()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                self._check_cancelled()
                body, separator, status_text = (result.stdout or b'').rpartition(b'\n')
                status = int(status_text) if separator and status_text.isdigit() else 0
                if status in (401, 403, 429, 503) or access_challenge(body):
                    self._access_held = True
                    self._report_source_failure('rate_limited' if status in (429, 503) else 'authentication_failed')
                    return None
                if result.returncode == 0 and 200 <= status < 300 and body.strip():
                    return body.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3) for {url}: {exc}")
            if attempt < 2:
                self._check_cancelled()
                time.sleep([1, 3, 9][attempt])
        self._report_source_failure('fetch_failed')
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        """Read the real source bounds and follow its exact adjacent-page link."""
        list_url = (f"{_BASE}/elibList.es?mid={_MID}" if page == 1
                    else getattr(self, '_next_list_url', None))
        if not list_url or page_number(list_url, 'researchreport') != page:
            raise ValueError('KEI adjacent source page not available')
        raw = self._curl_get(list_url, referer=self.base_url)
        if not raw:
            raise ConnectionError('KEI source listing unavailable')
        parsed = parse_listing(raw, 'researchreport', page)
        bounds = parsed['total'], parsed['last_page']
        if getattr(self, '_list_bounds', None) not in (None, bounds):
            raise ValueError('KEI source listing bounds changed during traversal')
        self._list_bounds, self._next_list_url = bounds, parsed['next_url']
        return [dict(c_id=r['source_id'], post_number=r['post_number'],
                     category=r['category'], date_raw=r['date_raw'], title=r['title'],
                     url=r['url'], list_url=list_url, source_attachments=r['attachments'])
                for r in parsed['items']]

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, detail_url, referer):
        """Fetch detail page; return a dict of parsed fields (empty on failure)."""
        raw = self._curl_get(detail_url, referer=referer)
        if not raw:
            return {}
        try:
            soup = _bs4(raw)
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error {detail_url}: {exc}")
            return {}

        result = {}

        tv = soup.find("div", class_="tstyle_view")
        right = tv.find("div", class_="right") if tv else None

        if right:
            tit_el = right.find("strong", class_="tit")
            if tit_el:
                result["title"] = _text(tit_el)

            cat_el = right.find("span", class_="cat")
            if cat_el:
                result["category"] = _text(cat_el)

            info_ul = right.find("ul")
            author_main, team, published_date = "", "", None
            if info_ul:
                for li in info_ul.find_all("li", recursive=False):
                    span = li.find("span")
                    em = li.find("em")
                    if not span or not em:
                        continue
                    key = _text(span)
                    val = _text(em)
                    if key == "저자" and val:
                        author_main = val
                    elif key == "연구진" and val:
                        team = val
                    elif key == "발간일" and val:
                        published_date = val

            authors = ([author_main] if author_main else []) + _parse_names(team)
            result["authors"] = authors
            if published_date:
                result["published_date"] = published_date

            file_a = right.select_one("ul.add_file li.clear a.file-down")
            if file_a:
                result["pdf_url"] = file_a.get("href", "")
                result["original_filename"] = _text(file_a)

        result["abstract"] = _find_abstract(soup)
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        return self._crawl_scopes(limit)
