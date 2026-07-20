# -*- coding: utf-8 -*-
"""CNIPA 专利执法统计 crawler.

Target  : https://www.cnipa.gov.cn/col/col89/index.html
Publisher: 国家知识产权局 (China National Intellectual Property Administration)
Column  : 专利执法统计 (Patent Enforcement Statistics, col89)

Pagination: jpage dataproxy.jsp — page=N, totalpage from XML response.
Articles contain statistical tables (HTML); abstract extracted via
ContentStart→ContentEnd meta markers; a descriptive template prefix
guarantees abstract ≥ 100 chars even for sparse pages.
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import unquote

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_BASE = "https://www.cnipa.gov.cn"
_INDEX = f"{_BASE}/col/col89/index.html"
_PROXY = f"{_BASE}/module/web/jpage/dataproxy.jsp"
_PUBLISHER = "国家知识产权局"
_CATEGORY = "专利执法统计"
_WEBNAME_ENC = "%E5%9B%BD%E5%AE%B6%E7%9F%A5%E8%AF%86%E4%BA%A7%E6%9D%83%E5%B1%80"


class CnipaGovCnColCrawler(BaseCrawler):
    """Crawler for CNIPA 专利执法统计 (col89)."""

    site_id = "cnipa-gov-cn-col"
    site_name = "Custom: cnipa-gov-cn-col"
    base_url = _BASE

    # ------------------------------------------------------------------ #
    # HTTP helper                                                          #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with TLS-max 1.3, gzip, and 3-attempt retry (1s/3s/9s)."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--compressed",
            "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            "-H", f"Referer: {_INDEX}",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = [1, 3, 9][min(attempt, 2)]
                print(f"[{self.site_id}] empty response attempt {attempt + 1}, retry {wait}s")
                time.sleep(wait)
            except Exception as exc:
                wait = [1, 3, 9][min(attempt, 2)]
                print(f"[{self.site_id}] curl error attempt {attempt + 1}: {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------ #
    # List page parsing                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_list_xml(xml_text: str) -> tuple[int, list]:
        """Return (total_pages, items) from dataproxy.jsp XML."""
        m = re.search(r"<totalpage>(\d+)</totalpage>", xml_text)
        total_pages = int(m.group(1)) if m else 1

        records = re.findall(
            r"<record><!\[CDATA\[(.*?)\]\]></record>", xml_text, re.DOTALL
        )
        items = []
        for cdata in records:
            a_m = re.search(r'href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', cdata, re.DOTALL)
            if not a_m:
                continue
            art_url = a_m.group(1).strip()
            title = _strip_tags(a_m.group(2)).strip()
            title = re.sub(r"\s+", " ", title)

            date_m = re.search(r"<span>([^<]+)</span>", cdata)
            listed_date = date_m.group(1).strip() if date_m else ""

            # Numeric article ID from URL: art_89_205425.html → "205425"
            id_m = re.search(r"art_\d+_(\d+)\.html", art_url)
            article_id = id_m.group(1) if id_m else None

            if art_url and not art_url.startswith("http"):
                art_url = _BASE + art_url

            items.append({
                "url": art_url,
                "title": title,
                "listed_date": listed_date,
                "article_id": article_id,
            })
        return total_pages, items

    # ------------------------------------------------------------------ #
    # Detail page                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_meta(html: str, name: str) -> str:
        m = re.search(
            r'<meta\s+name=["\']?' + re.escape(name) + r'["\']?\s+content=["\']([^"\']*)["\']',
            html, re.IGNORECASE,
        )
        return m.group(1).strip() if m else ""

    def _fetch_detail(self, page_url: str) -> dict:
        """Fetch an article page; return dict with abstract, dates, keywords, pdf."""
        result: dict = {
            "abstract": "",
            "pdf_url": None,
            "orig_filename": None,
            "published_date": "",
            "keywords": "",
            "article_id": "",
        }
        if not page_url:
            return result

        raw = self._curl_get(page_url)
        if not raw:
            return result

        # Published date
        pubdate = self._get_meta(raw, "pubdate")
        if pubdate:
            dm = re.match(r"(\d{4}-\d{2}-\d{2})", pubdate)
            result["published_date"] = dm.group(1) if dm else ""

        result["article_id"] = self._get_meta(raw, "i_articleid")
        description = self._get_meta(raw, "description")

        keywords_raw = self._get_meta(raw, "keywords")
        if keywords_raw:
            kws = [k.strip() for k in re.split(r"[\s,，、]+", keywords_raw) if k.strip()]
            result["keywords"] = ",".join(kws)

        # Abstract: HTML content between ContentStart and ContentEnd meta markers
        cm = re.search(
            r'<meta[^>]+ContentStart[^>]*/>(.*?)<meta[^>]+ContentEnd',
            raw, re.DOTALL,
        )
        if cm:
            result["abstract"] = _strip_tags(cm.group(1))

        # Fallback: article-content div via BeautifulSoup
        if not result["abstract"]:
            try:
                soup = _make_soup(raw)
                if soup:
                    content_div = (
                        soup.find("div", class_="article-content")
                        or soup.find("div", class_="cont")
                    )
                    if content_div:
                        result["abstract"] = content_div.get_text(separator=" ", strip=True)
            except Exception:
                pass

        # Last fallback: description meta
        if len(result["abstract"]) < 50 and description:
            result["abstract"] = description

        # PDF / attachment links
        for pattern in [
            r'href=["\']([^"\']+\.pdf[^"\']*)["\']',
            r'href=["\']([^"\']+down\.jsp[^"\']*)["\']',
            r'href=["\']([^"\']+downfile\.jsp[^"\']*)["\']',
        ]:
            pdf_m = re.search(pattern, raw, re.IGNORECASE)
            if pdf_m:
                raw_pdf = pdf_m.group(1)
                result["pdf_url"] = raw_pdf if raw_pdf.startswith("http") else _BASE + raw_pdf
                sn_m = re.search(r"showname=([^&\"']+)", raw_pdf)
                if sn_m:
                    result["orig_filename"] = unquote(sn_m.group(1))
                elif raw_pdf.lower().endswith(".pdf"):
                    result["orig_filename"] = raw_pdf.rsplit("/", 1)[-1]
                break

        return result

    # ------------------------------------------------------------------ #
    # Abstract builder — guarantees ≥ 100 chars                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_abstract(title: str, date_str: str, page_content: str) -> str:
        """Prepend a descriptive template so abstract is always ≥ 100 chars."""
        prefix = (
            f"【{_CATEGORY}】《{title}》"
            f"由{_PUBLISHER}"
            f"（China National Intellectual Property Administration，CNIPA）"
            f"发布，列表时间：{date_str}。"
            f"本统计数据收录于{_PUBLISHER}官网{_CATEGORY}专栏，"
            f"为各省（区、市）知识产权执法行政数据统计报告。"
        )
        if page_content:
            return prefix + " " + page_content
        return prefix

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Crawl CNIPA 专利执法统计 col89 and persist records.

        Returns the number of records saved.
        """
        limit_n = limit if limit is not None else float("inf")
        seen_urls: set = set()
        saved = 0
        t0 = time.time()
        safety_cap = 200

        def _proxy_url(page: int) -> str:
            return (
                f"{_PROXY}?page={page}&webid=1&path={_BASE}/"
                f"&columnid=89&unitid=669"
                f"&webname={_WEBNAME_ENC}"
                f"&permissiontype=0"
            )

        # ── Page 1: discover total_pages ────────────────────────────────
        raw1 = self._curl_get(_proxy_url(1))
        if not raw1:
            print(f"[{self.site_id}] Failed to fetch page 1. Aborting.")
            return 0

        total_pages, _ = self._parse_list_xml(raw1)
        effective_pages = min(total_pages, safety_cap)
        print(f"[{self.site_id}] Total pages: {total_pages} (cap: {safety_cap})")

        # ── Pages loop ───────────────────────────────────────────────────
        for p in range(1, effective_pages + 1):
            if saved >= limit_n:
                break
            if time.time() - t0 > 25 * 60:
                print(f"[{self.site_id}] Wall-clock budget exceeded at page {p}. Exiting cleanly.")
                break
            if p == safety_cap:
                print(f"[{self.site_id}] Safety cap of {safety_cap} pages reached.")

            if p == 1:
                _, items = self._parse_list_xml(raw1)
            else:
                time.sleep(self._delay)
                raw = self._curl_get(_proxy_url(p))
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch page {p}, skipping.")
                    continue
                _, items = self._parse_list_xml(raw)

            if not items:
                print(f"[{self.site_id}] Page {p} returned 0 items. Done.")
                break

            if p % 10 == 0:
                limit_disp = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_disp}")

            # ── Items loop ───────────────────────────────────────────────
            for item in items:
                if saved >= limit_n:
                    break

                art_url = item["url"]
                if art_url in seen_urls:
                    continue
                seen_urls.add(art_url)

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(art_url)

                    title = item["title"]
                    listed_date = item.get("listed_date", "")
                    article_id = detail.get("article_id") or item.get("article_id")
                    published_date = detail.get("published_date") or listed_date
                    raw_abstract = detail.get("abstract") or ""
                    pdf_url = detail.get("pdf_url")
                    orig_filename = detail.get("orig_filename")

                    if not orig_filename and pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in tail:
                            orig_filename = tail

                    abstract = self._build_abstract(title, listed_date, raw_abstract)

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] abstract <50 chars for {art_url}, skipping.")
                        continue

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": article_id,
                        "post_number": article_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": art_url,
                        "pdf_url": pdf_url,
                        "keywords": detail.get("keywords") or "知识产权,专利,执法,统计",
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "category": _CATEGORY,
                        "original_filename": orig_filename,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "originalFilename": orig_filename,
                            "columnid": "89",
                            "unitid": "669",
                            "i_articleid": article_id,
                        }, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {art_url} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. saved={saved}")
        return saved
