# -*- coding: utf-8 -*-
"""Crawler for lib.cssn.cn digital resource directory (学科分类浏览).

Target: http://lib.cssn.cn/zy/dzzy/?column=xueke
List:   http://lib.cssn.cn/zy/dzzy/sjk_1/axkflll/  (11 pages)
Detail: http://lib.cssn.cn/zy/dzzy/sjk_1/YYYYMM/tYYYYMMDD_ID.shtml
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_LIST_BASE = "http://lib.cssn.cn/zy/dzzy/sjk_1/axkflll/"
_DETAIL_BASE = "http://lib.cssn.cn/zy/dzzy/sjk_1/"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_CLOCK_LIMIT = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
_MIN_ABSTRACT = 100  # chars; skip items below this


def _curl_get(url, retries=3):
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-A", "Mozilla/5.0",
                 "--max-time", "30", url],
                capture_output=True, timeout=40,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", errors="replace")
            print(f"[lib-cssn-cn-zy] curl exit {r.returncode} (attempt {attempt+1}/{retries}): {url}")
        except subprocess.TimeoutExpired:
            print(f"[lib-cssn-cn-zy] curl timeout (attempt {attempt+1}/{retries}): {url}")
        except Exception as exc:
            print(f"[lib-cssn-cn-zy] curl error (attempt {attempt+1}/{retries}): {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


def _make_soup(html):
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _text(tag):
    if tag is None:
        return ""
    return tag.get_text(separator=" ", strip=True)


class LibCssnCnZyCrawler(BaseCrawler):
    site_id = "lib-cssn-cn-zy"
    site_name = "Custom: lib-cssn-cn-zy"
    base_url = "http://lib.cssn.cn"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page_num in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > _WALL_CLOCK_LIMIT:
                print(f"[lib-cssn-cn-zy] Wall-clock limit reached, stopping.")
                break

            if page_num % 10 == 0:
                print(f"[lib-cssn-cn-zy] page {page_num}: saved {saved}/{limit_str}")

            list_url = _LIST_BASE if page_num == 1 else f"{_LIST_BASE}index_{page_num}.shtml"

            html = _curl_get(list_url)
            if not html:
                print(f"[lib-cssn-cn-zy] Failed to fetch list page {page_num}, stopping.")
                break

            soup = _make_soup(html)
            if not soup:
                print(f"[lib-cssn-cn-zy] Failed to parse list page {page_num}, stopping.")
                break

            detail_spans = soup.select("span.detail a[href]")
            if not detail_spans:
                print(f"[lib-cssn-cn-zy] No items on page {page_num}, end of pagination.")
                break

            new_on_page = 0
            for link in detail_spans:
                if limit is not None and saved >= limit:
                    break

                href = link.get("href", "").strip()
                if not href:
                    continue

                # href is relative from axkflll/: '../YYYYMM/tXXX.shtml'
                if href.startswith("../"):
                    detail_url = _DETAIL_BASE + href[3:]
                elif href.startswith("http"):
                    detail_url = href
                else:
                    detail_url = _DETAIL_BASE + href

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    item = self._fetch_detail(detail_url)
                    if item is None:
                        continue

                    abstract = item.get("abstract", "") or ""
                    if len(abstract) < _MIN_ABSTRACT:
                        print(f"[lib-cssn-cn-zy] Short abstract ({len(abstract)} chars), skipping: {detail_url}")
                        continue

                    self._save_paper(item)
                    saved += 1
                    time.sleep(1.0)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[lib-cssn-cn-zy] item {detail_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[lib-cssn-cn-zy] No new items on page {page_num}, end of pagination.")
                break

            if page_num == _MAX_PAGES:
                print(f"[lib-cssn-cn-zy] Safety cap of {_MAX_PAGES} pages reached.")

            if limit is not None and saved >= limit:
                break

        print(f"[lib-cssn-cn-zy] Crawl complete: saved {saved} items.")
        return saved

    def _fetch_detail(self, url):
        html = _curl_get(url)
        if not html:
            return None

        soup = _make_soup(html)
        if not soup:
            return None

        h1 = soup.find("h1")
        title = _text(h1) if h1 else ""
        if not title:
            return None

        # Extract date and IDs from URL pattern: .../YYYYMM/tYYYYMMDD_NNNNN.shtml
        m = re.search(r'/(\d{6})/(t(\d{8})_(\d+))\.shtml', url)
        if m:
            date_str = m.group(3)          # "20180517"
            post_number = m.group(4)       # "4296241"
            external_id = m.group(2)       # "t20180517_4296241"
            published_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        else:
            post_number = None
            published_date = None
            slug = url.rstrip("/").rsplit("/", 1)[-1].replace(".shtml", "")
            external_id = re.sub(r"[^a-zA-Z0-9_-]", "_", slug) or url

        # Parse key-value table
        fields = {}
        table = soup.find("table")
        if table:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True)
                    fields[label] = cells[1]  # keep tag for rich extraction

        def field_text(key):
            tag = fields.get(key)
            if tag is None:
                return ""
            return tag.get_text(separator=" ", strip=True)

        abstract = field_text("数据库介绍")
        category = field_text("学科")
        resource_type = field_text("资源类型")
        language = field_text("语种")
        depth = field_text("揭示深度")
        db_category = field_text("数据库类别")
        access_method = field_text("使用方式")
        access_scope = field_text("使用范围")
        db_name = field_text("数据库名称") or title

        # db_url from the link inside 数据库网址 cell
        db_url = ""
        db_url_tag = fields.get("数据库网址")
        if db_url_tag:
            a = db_url_tag.find("a")
            db_url = a["href"] if a and a.get("href") else field_text("数据库网址")

        metadata = {
            "db_name": db_name,
            "db_url": db_url,
            "db_category": db_category,
            "language": language,
            "resource_type": resource_type,
            "depth": depth,
            "access_method": access_method,
            "access_scope": access_scope,
        }

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": None,
            "category": category,
            "keywords": resource_type,
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
