# -*- coding: utf-8 -*-
"""工业和信息化部 政府信息公开年报 crawler.

Target: https://www.miit.gov.cn/zwgk/zfxxgkzl/zfxxgknb/gyhxxhb/index.html
List API: /api-gateway/jpaas-publish-server/front/page/build/unit (JSON → HTML)
Detail:   /zwgk/zfxxgkzl/zfxxgknb/gyhxxhb/art/{year}/art_{hash}.html (HTML)
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urljoin, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402

_BASE = "https://www.miit.gov.cn"
_LIST_API = f"{_BASE}/api-gateway/jpaas-publish-server/front/page/build/unit"
_LIST_PARAMS_BASE = {
    "parseType": "buildstatic",
    "webId": "8d828e408d90447786ddbe128d495e9e",
    "tplSetId": "209741b2109044b5b7695700b2bec37e",
    "pageType": "column",
    "tagId": "栏目1板块",
    "editType": "null",
    "pageId": "829759aed51b4ca49164b7a711a11de8",
}
_PAGE_SIZE = 15
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_REFERER = f"{_BASE}/zwgk/zfxxgkzl/zfxxgknb/gyhxxhb/index.html"


def _make_soup(raw: str):
    """Try BeautifulSoup parsers in fallback order; return soup or None."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


class MiitGovCnZwgkCrawler(BaseCrawler):
    site_id = "miit-gov-cn-zwgk"
    site_name = "Custom: miit-gov-cn-zwgk"
    base_url = _BASE

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: dict | None = None) -> str | None:
        """HTTP GET via curl with retry/backoff. Returns decoded text or None."""
        if params:
            url = url + "?" + urlencode(params, encoding="utf-8")
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--max-time", "30",
            "-A", _UA,
            "-H", "Accept: application/json,text/html,*/*;q=0.8",
            "-H", "Accept-Language: zh-CN,zh;q=0.9",
            "-H", f"Referer: {_REFERER}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw.strip():
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = (attempt + 1) ** 2  # 1s, 4s
                    print(f"[{self.site_id}] Empty response attempt {attempt+1}/3, retry in {wait}s")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] Timeout attempt {attempt+1}/3 for {url}")
                if attempt < 2:
                    time.sleep((attempt + 1) * 3)
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt+1}/3: {exc}")
                if attempt < 2:
                    time.sleep((attempt + 1) * 3)
        print(f"[{self.site_id}] Failed after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page_no: int) -> str | None:
        params = dict(_LIST_PARAMS_BASE)
        params["paramJson"] = json.dumps({"pageNo": page_no, "pageSize": _PAGE_SIZE})
        return self._curl_get(_LIST_API, params)

    def _parse_list_html(self, html: str) -> list[tuple[str, str, str]]:
        """Parse list-page HTML fragment; return [(title, full_url, listed_date)]."""
        try:
            soup = _make_soup(html)
        except Exception:
            soup = None
        if not soup:
            return []
        items = []
        for p in soup.find_all("p"):
            a_tag = p.find("a")
            span = p.find("span")
            if not a_tag or not span:
                continue
            href = a_tag.get("href", "").strip()
            title = (a_tag.get("title", "") or a_tag.get_text(strip=True)).strip()
            listed_date = span.get_text(strip=True)
            if href and title:
                full_url = urljoin(self.base_url, href)
                items.append((title, full_url, listed_date))
        return items

    def _get_total_pages(self, list_json_raw: str) -> int | None:
        """Extract total item count from list response and compute total pages."""
        try:
            data = json.loads(list_json_raw)
            html = data["data"]["html"]
            m = re.search(r'count="(\d+)"', html)
            if m:
                count = int(m.group(1))
                return (count + _PAGE_SIZE - 1) // _PAGE_SIZE
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> str | None:
        return self._curl_get(url)

    @staticmethod
    def _meta(soup, name: str) -> str:
        tag = soup.find("meta", attrs={"name": name})
        return (tag.get("content", "") or "").strip() if tag else ""

    def _parse_detail(self, html: str, url: str) -> dict:
        """Parse article detail page; return field dict."""
        try:
            soup = _make_soup(html)
        except Exception:
            soup = None
        if not soup:
            return {}

        title = self._meta(soup, "ArticleTitle")
        pub_date_raw = self._meta(soup, "PubDate")   # "2026-01-30 17:21"
        source = self._meta(soup, "ContentSource")
        keywords = self._meta(soup, "Keywords")
        description = self._meta(soup, "Description")
        author = self._meta(soup, "Author")

        pub_date = pub_date_raw[:10] if pub_date_raw else ""

        # Body text from #con_con div
        body_div = soup.find(id="con_con")
        body_text = ""
        if body_div:
            body_text = re.sub(r"\s+", " ", body_div.get_text(separator=" ", strip=True)).strip()

        # PDF/attachment links in body
        pdf_url = None
        original_filename = None
        if body_div:
            for a in body_div.find_all("a", href=True):
                href = a["href"]
                if re.search(r"\.(pdf|doc|docx)$", href, re.IGNORECASE) or "attach" in href.lower():
                    pdf_url = urljoin(self.base_url, href)
                    original_filename = href.rstrip("/").split("/")[-1]
                    break

        # Combine description (short summary) + body as abstract
        abstract = body_text or description

        # External ID: the art_HASH from URL path
        url_path = urlparse(url).path
        art_match = re.search(r"(art_[0-9a-f]+)", url_path)
        external_id = art_match.group(1) if art_match else url_path.split("/")[-1].replace(".html", "")

        return {
            "title": title,
            "abstract": abstract,
            "pub_date": pub_date,
            "source": source,
            "keywords": keywords,
            "description": description,
            "author": author,
            "external_id": external_id,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MIIT 政府信息公开年报, save via _save_paper. Returns saved count."""
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        saved = 0
        seen_urls: set[str] = set()
        page = 1
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        while True:
            # Limit / time guards
            if limit is not None and saved >= limit:
                break
            if page > max_pages:
                print(f"[{self.site_id}] Safety cap of {max_pages} pages reached. Stopping.")
                break
            if time.time() - start_time > max_seconds:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping.")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            raw = self._fetch_list_page(page)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            try:
                data = json.loads(raw)
                list_html = data["data"]["html"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                print(f"[{self.site_id}] Invalid list response at page {page}: {exc}. Stopping.")
                break

            items = self._parse_list_html(list_html)
            if not items:
                print(f"[{self.site_id}] No items at page {page}. Done.")
                break

            # URL deduplication — detect silently looping paginator
            new_items = [(t, u, d) for t, u, d in items if u not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break

            for title_list, url, listed_date in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > max_seconds:
                    print(f"[{self.site_id}] Time budget reached mid-page.")
                    break

                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail_raw = self._fetch_detail(url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {url} failed: empty response. Skipping.")
                        continue

                    detail = self._parse_detail(detail_raw, url)

                    art_title = detail.get("title") or title_list
                    abstract = detail.get("abstract", "")

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract <50 chars for {url}. Skipping.")
                        continue

                    external_id = detail.get("external_id", "")
                    pub_date = detail.get("pub_date", "")
                    source = detail.get("source", "")
                    keywords_str = detail.get("keywords", "")
                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename")
                    description = detail.get("description", "")

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": None,
                        "title": art_title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": listed_date,
                        "authors": detail.get("author", "") or "",
                        "publisher": source,
                        "department": "",
                        "journal": "",
                        "url": url,
                        "pdf_url": pdf_url,
                        "keywords": keywords_str,
                        "category": "政府信息公开年报",
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "description": description,
                                "originalFilename": original_filename,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_display}: {art_title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
