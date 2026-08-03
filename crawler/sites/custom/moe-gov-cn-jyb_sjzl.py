# -*- coding: utf-8 -*-
"""MOE China – 全国教育经费执行情况统计公告 (Education Fund Execution Announcements) crawler.

List source:  http://www.moe.gov.cn/jyb_sjzl/sjzl_jfzxgg/
Pagination:   WAS AJAX API  /was5/web/search?channelid=254874&chnlid=2147438816&page=N
Total:        ~28 records, 20 per page  → 2 pages
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

# Absolute import required — spec_from_file_location has no package context.
from crawler.base_crawler import BaseCrawler


class MoeGovCnJybSjzlCrawler(BaseCrawler):
    """Crawler for 中华人民共和国教育部 – 全国教育经费执行情况统计公告."""

    site_id = "moe-gov-cn-jyb_sjzl"
    site_name = "Custom: moe-gov-cn-jyb_sjzl"
    base_url = "http://www.moe.gov.cn"

    _LIST_URL = "http://www.moe.gov.cn/jyb_sjzl/sjzl_jfzxgg/"
    _WAS_URL = "http://www.moe.gov.cn/was5/web/search"
    _WAS_CHANNEL_ID = "254874"
    _WAS_CHNL_ID = "2147438816"
    _PAGE_SIZE = 20

    # Paragraphs that match this pattern are navigation/footer noise, not content.
    _NOISE_RE = re.compile(
        r"^(版权所有|Copyright|收藏|分享|打印|关闭|您访问|地址：|责任编辑|来源：|发布日期)",
        re.IGNORECASE,
    )

    # ------------------------------------------------------------------ #
    # Network helpers
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str, referer: str = None) -> str | None:
        """GET via curl with exponential-backoff retry (3 attempts).

        Returns response text (UTF-8, errors='replace') or None on failure.
        """
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        delays = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                print(
                    f"[{self.site_id}] Empty response attempt {attempt + 1}/3 "
                    f"(retry in {delays[attempt]}s): {url}"
                )
            except subprocess.TimeoutExpired:
                print(
                    f"[{self.site_id}] Timeout attempt {attempt + 1}/3 "
                    f"(retry in {delays[attempt]}s): {url}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl error attempt {attempt + 1}/3: {exc}"
                )
            if attempt < 2:
                time.sleep(delays[attempt])
        return None

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_soup(html: str):
        """Try BeautifulSoup with fallback parsers. Returns None on total failure."""
        from bs4 import BeautifulSoup  # noqa: PLC0415 — lazy import for robustness
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _strip_text(t: str) -> str:
        """Normalize whitespace and remove full-width/non-breaking spaces."""
        t = re.sub(r"[　\xa0]+", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    def _parse_list_page(self, html: str) -> list:
        """Parse WAS list HTML fragment; return [(title, href, listed_date), ...]."""
        items = []
        soup = None
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] list soup construction failed: {exc}")

        if soup:
            try:
                for li in soup.find_all("li"):
                    a = li.find("a", href=True)
                    span = li.find("span")
                    if not a:
                        continue
                    href = (a.get("href") or "").strip()
                    title = (a.get("title") or a.get_text(strip=True) or "").strip()
                    date = span.get_text(strip=True) if span else ""
                    if href and title:
                        items.append((title, href, date))
                if items:
                    return items
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup list parse error: {exc}")

        # Regex fallback
        try:
            for m in re.finditer(
                r'<a\s[^>]*href="([^"]+)"[^>]*title="([^"]+)"[^>]*>.*?'
                r"<span>([\d-]+)</span>",
                html,
                re.DOTALL,
            ):
                items.append((m.group(2).strip(), m.group(1).strip(), m.group(3).strip()))
        except Exception as exc:
            print(f"[{self.site_id}] Regex list parse error: {exc}")
        return items

    def _parse_detail_page(self, html: str, page_url: str) -> dict:
        """Extract abstract, published_date, publisher, attachment from a detail page."""
        result = {
            "abstract": "",
            "published_date": "",
            "publisher": "",
            "pdf_url": None,
            "original_filename": None,
        }

        soup = None
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] detail soup construction failed: {exc}")

        # ---- published_date + publisher (from #downloadContent) ----
        try:
            dl_text = ""
            if soup:
                dl_div = soup.find(id="downloadContent")
                if dl_div:
                    dl_text = self._strip_text(dl_div.get_text(separator=" ", strip=True))
            if not dl_text:
                m = re.search(
                    r'id="downloadContent"[^>]*>(.*?)(?:</div>|id=)', html, re.DOTALL
                )
                if m:
                    dl_text = self._strip_text(re.sub(r"<[^>]+>", " ", m.group(1)))

            dm = re.search(r"发布日期[：:]\s*([\d-]+)", dl_text)
            if dm:
                result["published_date"] = dm.group(1).strip()
            sm = re.search(r"来源[：:]\s*([^\s　　<]+)", dl_text)
            if sm:
                result["publisher"] = sm.group(1).strip()
        except Exception as exc:
            print(f"[{self.site_id}] date/publisher parse error: {exc}")

        # Fallback: raw HTML scan for date
        if not result["published_date"]:
            dm = re.search(r"发布日期[：:]\s*([\d-]+)", html)
            if dm:
                result["published_date"] = dm.group(1).strip()

        # ---- Attachment (doc/docx/pdf/xls/xlsx/zip) ----
        try:
            if soup:
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if re.search(
                        r"\.(doc|docx|pdf|xls|xlsx|zip)(\?[^\"]*)?$",
                        href,
                        re.IGNORECASE,
                    ):
                        full_url = href if href.startswith("http") else urljoin(page_url, href)
                        oldsrc = (a.get("oldsrc") or "").strip()
                        fname = oldsrc if oldsrc else href.split("/")[-1].split("?")[0]
                        result["pdf_url"] = full_url
                        result["original_filename"] = fname if fname else None
                        break
        except Exception as exc:
            print(f"[{self.site_id}] attachment parse error: {exc}")

        # ---- Abstract from article body paragraphs ----
        try:
            paras = []
            if soup:
                # Prefer a known content container; fall back to whole document
                body = None
                for search_kwargs in [
                    {"class_": re.compile(r"TRS_Editor", re.IGNORECASE)},
                    {"id": "content"},
                    {"class_": re.compile(r"\bcontent\b", re.IGNORECASE)},
                    {"class_": re.compile(r"article", re.IGNORECASE)},
                ]:
                    try:
                        body = soup.find(**search_kwargs)
                    except Exception:
                        pass
                    if body:
                        break

                source = body if body else soup
                for p in source.find_all("p"):
                    t = self._strip_text(p.get_text(separator=" ", strip=True))
                    if len(t) > 15 and not self._NOISE_RE.search(t):
                        paras.append(t)

            if not paras:
                for m in re.finditer(r"<p[^>]*>(.*?)</p>", html, re.DOTALL):
                    t = self._strip_text(re.sub(r"<[^>]+>", " ", m.group(1)))
                    if len(t) > 15 and not self._NOISE_RE.search(t):
                        paras.append(t)

            result["abstract"] = "\n".join(paras)
        except Exception as exc:
            print(f"[{self.site_id}] abstract parse error: {exc}")

        return result

    @staticmethod
    def _extract_post_number(url: str) -> str | None:
        """Extract numeric ID from URL segment like t20251231_1426092.html → '1426092'."""
        m = re.search(r"t\d{6,8}_(\d+)\.html", url)
        if m:
            return m.group(1)
        m = re.search(r"_(\d{6,})\.html", url)
        if m:
            return m.group(1)
        return None

    # ------------------------------------------------------------------ #
    # Main crawl
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Crawl all 教育经费执行公告 from MOE China.

        Uses the WAS AJAX endpoint for all pages (consistent, no static/AJAX split).
        Stops when: saved >= limit, page returns no new records, or safety cap hit.
        """
        saved = 0
        seen_urls: set = set()
        start_ts = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        # Determine total pages from the main list page's embedded JS
        main_html = self._curl_get(self._LIST_URL)
        total_records = 0
        if main_html:
            m = re.search(r"var\s+recordCount\s*=\s*(\d+)", main_html)
            if m:
                total_records = int(m.group(1))
        # Ceiling division
        total_pages = max(1, (total_records + self._PAGE_SIZE - 1) // self._PAGE_SIZE) \
            if total_records else 200
        print(f"[{self.site_id}] recordCount={total_records}, totalPages={total_pages}")

        page = 1
        MAX_PAGES = 200

        while True:
            # Time-budget guard
            if time.time() - start_ts > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            # WAS API works for every page number
            was_url = (
                f"{self._WAS_URL}?channelid={self._WAS_CHANNEL_ID}"
                f"&chnlid={self._WAS_CHNL_ID}&page={page}"
            )
            list_html = self._curl_get(was_url, referer=self._LIST_URL)
            if not list_html:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(list_html)
            if not items:
                print(f"[{self.site_id}] No items at page {page}. Done.")
                break

            new_on_page = 0
            for title, href, listed_date in items:
                if limit is not None and saved >= limit:
                    break

                # Resolve absolute detail URL
                if href.startswith("http"):
                    detail_url = href
                else:
                    detail_url = urljoin(self._LIST_URL, href)

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                # Per-item error isolation — one bad page must not abort the run
                try:
                    time.sleep(self._delay)

                    detail_html = self._curl_get(detail_url, referer=was_url)
                    if not detail_html:
                        print(f"[{self.site_id}] Failed to fetch detail: {detail_url}")
                        continue

                    detail = self._parse_detail_page(detail_html, detail_url)
                    abstract = detail["abstract"]

                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] Abstract too short "
                            f"({len(abstract)} chars), skipping: {title[:50]}"
                        )
                        continue

                    post_number = self._extract_post_number(detail_url)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_number,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": detail["published_date"] or listed_date,
                        "posted_date": listed_date,
                        "publisher": detail["publisher"] or "教育部",
                        "url": detail_url,
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "category": "教育经费执行公告",
                        "keywords": None,
                        "authors": None,
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "originalFilename": detail["original_filename"],
                                "channelid": self._WAS_CHANNEL_ID,
                                "chnlid": self._WAS_CHNL_ID,
                                "category": "教育经费执行公告",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({title[:40]}): {exc}")
                    continue

            # Stop if all items on this page were already seen (loop-back detection)
            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break

            # Stop once we've covered all known pages
            if total_records and page >= total_pages:
                print(f"[{self.site_id}] Reached last page ({page}/{total_pages}). Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
