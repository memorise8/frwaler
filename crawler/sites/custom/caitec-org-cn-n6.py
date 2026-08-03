# -*- coding: utf-8 -*-
"""CAITEC (商务部国际贸易经济合作研究院) Research Reports crawler.

Starting URL: https://www.caitec.org.cn/n6/sy_xsyj_yjbg/
List JSON:    /n6/sy_xsyj_yjbg/json/index.html (page 1),
              /n6/sy_xsyj_yjbg/json/index_N.html (page N >= 2)
Detail HTML:  /n6/sy_xsyj_yjbg/json/{id}.html  (plain HTML, not JSON despite path)
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.caitec.org.cn"
_LIST_P1 = f"{_BASE}/n6/sy_xsyj_yjbg/json/index.html"
_LIST_PN = f"{_BASE}/n6/sy_xsyj_yjbg/json/index_{{page}}.html"
_SITE_ID = "caitec-org-cn-n6"


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3):
    """Fetch URL via curl --tls-max 1.3 with exponential-backoff retry.
    Returns decoded string or None on total failure.
    """
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/json,*/*;q=0.8",
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, "
                      f"retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _fetch_list_page(page: int):
    """Return parsed JSON dict for list page N, or None on failure."""
    url = _LIST_P1 if page == 1 else _LIST_PN.format(page=page)
    raw = _curl_get(url)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        print(f"[{_SITE_ID}] JSON parse error on list page {page}: {exc}")
        return None


def _parse_detail(html: str, detail_url: str, list_title: str = "") -> dict:
    """Extract title, abstract, published_date, publisher, pdf_url from detail HTML.

    Abstract strategy: use article body text; if < 100 chars, prepend the title
    so attachment-only pages still meet the minimum length requirement.
    """
    title = ""
    abstract = ""
    published_date = ""
    publisher = ""
    pdf_url = ""
    original_filename = None

    try:
        soup = _make_soup(html)
        if soup is None:
            raise RuntimeError("all HTML parsers failed")

        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

        # Date/source metadata block: <div style="font-size: 12px; ...">
        date_div = soup.find("div", style=lambda s: s and "font-size: 12px" in s)
        if date_div:
            date_text = date_div.get_text(" ", strip=True)
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", date_text)
            if dm:
                published_date = dm.group(1)
            sm = re.search(r"文章来源[：:]\s*(.+?)(?:\s{2,}|$)", date_text)
            if sm:
                publisher = sm.group(1).strip()

        # Article body text
        art = soup.find(class_="art-bd")
        body_text = ""
        if art:
            for tag in art.find_all(["script", "style"]):
                tag.decompose()
            body_text = re.sub(r"\s+", " ", art.get_text(separator=" ", strip=True)).strip()

        # Build abstract: prepend title when body alone is under 100 chars
        effective_title = title or list_title
        if len(body_text) >= 100:
            abstract = body_text
        elif effective_title:
            abstract = (effective_title + " " + body_text).strip()
        else:
            abstract = body_text

        # First PDF link on the page
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower():
                if not href.startswith("http"):
                    href = _BASE + href
                pdf_url = href
                tail = href.rstrip("/").split("/")[-1].split("?")[0]
                original_filename = tail if tail else None
                break

    except Exception as exc:
        print(f"[{_SITE_ID}] detail parse error ({detail_url}): {exc}")
        # Regex fallback so one bad page never crashes the run
        try:
            body_text = re.sub(r"<[^>]+>", " ", html)
            body_text = re.sub(r"\s+", " ", body_text).strip()
            effective_title = list_title
            if len(body_text) >= 100:
                abstract = body_text[:3000]
            elif effective_title:
                abstract = (effective_title + " " + body_text).strip()[:3000]
            else:
                abstract = body_text[:3000]
            m = re.search(r'href="(/upfiles[^"]+\.pdf)"', html, re.I)
            if m:
                pdf_url = _BASE + m.group(1)
                original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
        except Exception:
            pass

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "publisher": publisher,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
    }


class CaitecOrgCnN6Crawler(BaseCrawler):
    """Crawler for CAITEC research reports (学术研究 > 研究报告)."""

    site_id = "caitec-org-cn-n6"
    site_name = "Custom: caitec-org-cn-n6"
    base_url = "https://www.caitec.org.cn"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        crawl_start = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        max_pages = 200
        limit_display = str(limit) if limit is not None else "inf"

        page = 1
        while page <= max_pages:
            if time.time() - crawl_start > max_wall:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            data = _fetch_list_page(page)
            if not data:
                print(f"[{_SITE_ID}] page {page}: failed to fetch list. Stopping.")
                break

            items = data.get("data") or []
            if not items:
                print(f"[{_SITE_ID}] page {page}: empty list. Done.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                alink = item.get("alink", "")
                if not alink:
                    continue

                detail_url = alink if alink.startswith("http") else _BASE + alink
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                # Numeric ID from URL e.g. /n6/sy_xsyj_yjbg/json/7030.html → "7030"
                m = re.search(r"/(\d+)\.html$", alink)
                post_number = m.group(1) if m else re.sub(r"\.html$", "", alink.split("/")[-1])
                external_id = post_number

                listed_date = item.get("dtime", "")
                list_title = item.get("atitle", "")

                try:
                    time.sleep(self._delay)

                    detail_html = _curl_get(detail_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] item {external_id}: failed to fetch detail, skipping")
                        continue

                    detail = _parse_detail(detail_html, detail_url, list_title=list_title)

                    title = detail["title"] or list_title
                    abstract = detail["abstract"]
                    published_date = detail["published_date"] or listed_date
                    publisher = detail["publisher"]
                    pdf_url = detail["pdf_url"]
                    original_filename = detail["original_filename"]

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] skipping (abstract <50 chars): {title[:60]}")
                        continue

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "publisher": publisher,
                        "original_filename": original_filename,
                        "category": "研究报告",
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "originalFilename": original_filename,
                            "category": "研究报告",
                        }, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {external_id} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: all items seen/filtered. Done.")
                break

            next_page = str(data.get("nextPage", "false")).lower()
            if next_page != "true":
                print(f"[{_SITE_ID}] page {page}: nextPage=false. Done.")
                break

            page += 1

        print(f"[{_SITE_ID}] crawl complete: saved={saved}")
        return saved
