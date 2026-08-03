# -*- coding: utf-8 -*-
"""Crawler for SASTIND 国家国防科技工业局 时政要闻 news list.

List pages are static HTML files:
  Page 1 : /n10086200/n10086319/index.html
  Page N  : /n10086200/n10086319/index_10126608_N.html  (N >= 1, ~363 pages)

Article detail URL pattern:
  /n10086200/n10086319/c{ID}/content.html
"""

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


SITE_ID = "sastind-gov-cn-n10086200"


class SastindGovCnN10086200Crawler(BaseCrawler):
    site_id = SITE_ID
    site_name = "Custom: sastind-gov-cn-n10086200"
    base_url = "https://www.sastind.gov.cn"

    _START_URL = "https://www.sastind.gov.cn/n10086200/n10086319/index.html"
    _PAGE_BASE = "https://www.sastind.gov.cn/n10086200/n10086319/index_10126608_{n}.html"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 100

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None, timeout=45):
        cmd = [
            "curl", "--tlsv1.2", "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = (1, 3, 9)
        last_error = None
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
                if result.returncode == 0 and result.stdout:
                    return self._decode(result.stdout)
                stderr = self._decode(result.stderr or b"").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except Exception as exc:
                last_error = str(exc)
            if attempt < 2:
                w = waits[attempt]
                print(f"[{SITE_ID}] curl failed for {url} (attempt {attempt+1}/3): {last_error}; retrying in {w}s")
                time.sleep(w)
        print(f"[{SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _decode(raw):
        if raw is None:
            return ""
        for enc in ("utf-8-sig", "gb18030", "gbk", "gb2312"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return BeautifulSoup("", "html.parser")

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        value = unescape(str(value))
        value = value.replace("\xa0", " ").replace("　", " ")
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _date_only(raw):
        raw = SastindGovCnN10086200Crawler._clean(raw)
        m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", raw)
        if m:
            y, mo, d = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        return ""

    def _abs_url(self, href):
        href = self._clean(href)
        if not href or href.lower().startswith(("javascript:", "mailto:")):
            return ""
        return urljoin(self.base_url + "/", href.lstrip("/"))

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _list_url(self, page_num):
        """Return the URL for list page number (1-based)."""
        if page_num == 1:
            return self._START_URL
        return self._PAGE_BASE.format(n=page_num)

    def _detect_max_page(self, soup):
        """Detect total page count from the hidden anchor list in the page."""
        # The page embeds <a href='...index_10126608_N.html'></a> for every page.
        max_n = 1
        for a in soup.find_all("a", href=True):
            m = re.search(r"index_\d+_(\d+)\.html", a["href"])
            if m:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
        return max_n

    def _parse_list(self, soup):
        """Return list of (title, list_abstract, detail_url, listed_date_str, article_id)."""
        items = []
        # PC content block — ul > li with <a> and date span
        for li in soup.select("div.list_infor_news ul li"):
            a_tag = li.find("a", href=True)
            if not a_tag:
                continue
            href = a_tag.get("href", "")
            if not href or "content.html" not in href:
                continue

            detail_url = self._abs_url(href)
            if not detail_url:
                continue

            # Article ID from path like /n.../c10751806/content.html
            m_id = re.search(r"/c(\d+)/content\.html", detail_url)
            article_id = m_id.group(1) if m_id else ""

            title = self._clean(a_tag.get("title") or "")
            if not title:
                p_first = a_tag.find("p", class_="list_first")
                title = self._clean(p_first.get_text(" ", strip=True)) if p_first else ""

            list_abstract = ""
            p_second = a_tag.find("p", class_="list_second")
            if p_second:
                list_abstract = self._clean(p_second.get_text(" ", strip=True))

            date_span = li.find("span", class_="list_news_time")
            listed_date_raw = self._clean(date_span.get_text(" ", strip=True)) if date_span else ""

            items.append((title, list_abstract, detail_url, listed_date_raw, article_id))
        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, detail_url, raw, list_title, list_abstract, listed_date_raw, article_id):
        soup = self._soup(raw)

        # Title
        title_tag = soup.find("div", class_="article_title")
        if title_tag:
            b_tag = title_tag.find("b")
            title = self._clean(b_tag.get_text(" ", strip=True) if b_tag else title_tag.get_text(" ", strip=True))
        else:
            title = ""
        if not title:
            title = list_title

        # Date + source from .wz_xx span
        published_date = ""
        publisher = ""
        wz_xx = soup.find("span", class_="wz_xx")
        if wz_xx:
            span_text = self._clean(wz_xx.get_text(" ", strip=True))
            # 发布日期：2026-06-02 　信息来源：中国政府网
            d_m = re.search(r"发布日期[：:]\s*(\d{4}-\d{2}-\d{2})", span_text)
            if d_m:
                published_date = d_m.group(1)
            s_m = re.search(r"信息来源[：:]\s*([^\s　&]+)", span_text)
            if s_m:
                publisher = self._clean(s_m.group(1))

        # Build listed_date from list page date (MM-DD) + year from published_date
        listed_date = ""
        if listed_date_raw and published_date:
            year = published_date[:4]
            m_md = re.match(r"(\d{2})-(\d{2})", listed_date_raw)
            if m_md:
                listed_date = f"{year}-{m_md.group(1)}-{m_md.group(2)}"
        if not listed_date:
            listed_date = published_date

        # Full article text from #article div
        article_div = soup.find("div", id="article")
        if article_div:
            for node in article_div.select("script, style, noscript"):
                node.decompose()
            full_text = self._clean(article_div.get_text(" ", strip=True))
        else:
            full_text = ""

        # Abstract: prefer full text, fallback to list snippet
        abstract = full_text if len(full_text) >= len(list_abstract) else list_abstract

        # PDF attachments
        pdf_url = ""
        original_filename = ""
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if re.search(r"\.(pdf|PDF)($|\?)", href):
                pdf_url = self._abs_url(href)
                fname = href.rstrip("/").split("/")[-1].split("?")[0]
                original_filename = fname
                break

        external_id = article_id or detail_url.rstrip("/").split("/")[-2]
        post_number = article_id if article_id else None

        metadata = {
            "posted_date": listed_date_raw,
            "publisher_raw": publisher,
            "detail_url": detail_url,
            "list_abstract": list_abstract,
        }

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": "",
            "publisher": publisher,
            "department": "国家国防科技工业局",
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url or None,
            "keywords": "",
            "category": "时政要闻",
            "doi": "",
            "original_filename": original_filename or None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        max_page = None

        page_num = 1
        while page_num <= self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time >= self._MAX_SECONDS - 30:
                print(f"[{SITE_ID}] approaching 25 min budget; exiting cleanly")
                break

            list_url = self._list_url(page_num)
            raw_list = self._curl_get(list_url, referer=self._START_URL)
            if not raw_list:
                print(f"[{SITE_ID}] page {page_num}: empty response; stopping")
                break

            soup_list = self._soup(raw_list)

            # Detect total pages on first page
            if max_page is None:
                max_page = self._detect_max_page(soup_list)
                if max_page > 1:
                    print(f"[{SITE_ID}] detected {max_page} list pages")

            items = self._parse_list(soup_list)
            if not items:
                print(f"[{SITE_ID}] page {page_num}: no items parsed; stopping")
                break

            new_on_page = 0
            for title, list_abstract, detail_url, listed_date_raw, article_id in items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= self._MAX_SECONDS - 30:
                    print(f"[{SITE_ID}] approaching 25 min budget; exiting cleanly")
                    return saved

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                item_label = article_id or detail_url
                try:
                    if self.detail_delay:
                        time.sleep(self.detail_delay)
                    raw_detail = self._curl_get(detail_url, referer=list_url)
                    if not raw_detail:
                        raise RuntimeError("empty detail response after retries")

                    paper = self._parse_detail(
                        detail_url, raw_detail, title, list_abstract, listed_date_raw, article_id
                    )

                    if not paper.get("title"):
                        print(f"[{SITE_ID}] item {item_label} skipped: missing title")
                        continue

                    abstract = self._clean(paper.get("abstract", ""))
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{SITE_ID}] item {item_label} skipped: "
                            f"short abstract ({len(abstract)} chars)"
                        )
                        continue
                    paper["abstract"] = abstract

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{SITE_ID}] item {item_label} failed: {exc}")
                    continue

            if page_num % 10 == 0:
                print(f"[{SITE_ID}] page {page_num}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{SITE_ID}] page {page_num}: 0 new items; stopping")
                break

            if max_page is not None and page_num >= max_page:
                print(f"[{SITE_ID}] reached final list page {page_num}/{max_page}")
                break

            page_num += 1

        if page_num > self._MAX_PAGES:
            print(f"[{SITE_ID}] reached safety page cap {self._MAX_PAGES}; stopping")

        return saved
