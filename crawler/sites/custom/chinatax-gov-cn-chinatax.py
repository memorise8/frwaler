# -*- coding: utf-8 -*-
"""Crawler for 国家税务总局 政府信息公开年报 (chinatax.gov.cn).

Starting URL: https://www.chinatax.gov.cn/chinatax/n810214/c102384r/index.html?tab=gknb
List API:     POST /getFileListByCodeId
              channelId = 61eaab3127b040d4950fff177f09e561
              Total ~32 records (Government Information Disclosure Annual Reports)
Detail page:  /chinatax/n810214/c102384r/c<id>/content.html
              Abstract in <div class="article-content" id="zoomcon">
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_CHANNEL_ID = "61eaab3127b040d4950fff177f09e561"
_LIST_URL = "https://www.chinatax.gov.cn/getFileListByCodeId"
_PAGE_SIZE = 20
_MAX_PAGES = 200
_WALL_BUDGET_SECS = 25 * 60


def _bs4_parse(html: str, parser: str):
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, parser)
    except Exception:
        return None


def _parse_html(html: str):
    """Try html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        soup = _bs4_parse(html, parser)
        if soup is not None:
            return soup
    return None


def _strip_tags(html_fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


class ChinataxGovCnCrawler(BaseCrawler):
    site_id = "chinatax-gov-cn-chinatax"
    site_name = "Custom: chinatax-gov-cn-chinatax"
    base_url = "https://www.chinatax.gov.cn"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries (3 attempts)."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", f"Referer: {self.base_url}",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            "-L", url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    time.sleep((attempt + 1) ** 2)
            except Exception as exc:
                print(f"[{self.site_id}] GET error (attempt {attempt+1}/3) {url}: {exc}")
                if attempt < 2:
                    time.sleep((attempt + 1) ** 2)
                else:
                    print(f"[{self.site_id}] GET failed after 3 attempts: {url}")
        return None

    def _curl_post_form(self, url: str, data: dict) -> str | None:
        """POST form-encoded data via curl with retries."""
        form_args = []
        for k, v in data.items():
            form_args.extend(["-d", f"{k}={v}"])
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", f"Referer: {self.base_url}",
            "-X", "POST",
        ] + form_args + [url]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    time.sleep((attempt + 1) ** 2)
            except Exception as exc:
                print(f"[{self.site_id}] POST error (attempt {attempt+1}/3): {exc}")
                if attempt < 2:
                    time.sleep((attempt + 1) ** 2)
                else:
                    print(f"[{self.site_id}] POST failed after 3 attempts")
        return None

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> dict | None:
        raw = self._curl_post_form(_LIST_URL, {
            "channelId": _CHANNEL_ID,
            "page": page,
            "size": _PAGE_SIZE,
            "relateSubChannels": "true",
        })
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            print(f"[{self.site_id}] JSON decode error on list page {page}")
            return None

    def _fetch_detail_abstract(self, url: str) -> str:
        """Fetch and extract article body text from a detail page."""
        url = url.replace("http://www.chinatax.gov.cn", "https://www.chinatax.gov.cn")
        raw = self._curl_get(url)
        if not raw:
            return ""

        soup = None
        try:
            soup = _parse_html(raw)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for {url}: {exc}")

        if soup is not None:
            # Primary: <div class="article-content" id="zoomcon">
            div = soup.find(id="zoomcon")
            if div is None:
                div = soup.find("div", class_="article-content")
            if div is not None:
                text = div.get_text(separator=" ", strip=True)
                return re.sub(r"\s+", " ", text).strip()

        # Fallback: raw-regex extraction from the article-content section
        for marker in ('class="article-content"', 'id="zoomcon"', "article-content"):
            idx = raw.find(marker)
            if idx >= 0:
                return _strip_tags(raw[idx: idx + 30000])

        return ""

    def _get_meta_value(self, item: dict, key: str) -> str:
        """Extract a metadata value from domainMetaList by key."""
        for dm in (item.get("domainMetaList") or []):
            for rl in (dm.get("resultList") or []):
                if rl.get("key") == key and rl.get("value"):
                    return str(rl["value"])
        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 国家税务总局 政府信息公开年报 via /getFileListByCodeId JSON API.

        Paginates until saved >= limit, no new items, or safety caps.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > _WALL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget exceeded at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")

            # Fetch list page with retry
            data = None
            for retry in range(3):
                data = self._fetch_list_page(page)
                if data is not None:
                    break
                wait = (retry + 1) * 3
                print(f"[{self.site_id}] list page {page} retry {retry+1}/3 in {wait}s")
                time.sleep(wait)

            if data is None:
                print(f"[{self.site_id}] list page {page}: 3 failures. Stopping.")
                break

            try:
                res_data = data["results"]["data"]
                items = res_data.get("results") or []
                total = res_data.get("total", 0)
            except (KeyError, TypeError):
                print(f"[{self.site_id}] unexpected response structure at page {page}. Stopping.")
                break

            if page == 1:
                print(f"[{self.site_id}] Total records on server: {total}")

            if not items:
                print(f"[{self.site_id}] No items at page {page}. Done.")
                break

            page_had_new = False

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    title = _strip_tags(item.get("titleHtml") or "").strip()
                    if not title:
                        title = _strip_tags(item.get("subTitleHtml") or "").strip()
                    if not title:
                        continue

                    url = (item.get("redirectUrl") or item.get("url") or "").strip()
                    url = url.replace("http://www.chinatax.gov.cn",
                                      "https://www.chinatax.gov.cn")
                    if not url:
                        continue

                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    page_had_new = True

                    # external_id from URL: .../c5247234/content.html → "5247234"
                    m = re.search(r"/c(\d+)/content\.html", url)
                    external_id = m.group(1) if m else re.sub(r"[^a-zA-Z0-9_-]", "_", url)[-64:]

                    pub_time_str = (item.get("publishedTimeStr") or "").strip()
                    published_date = pub_time_str.split(" ")[0] if pub_time_str else ""

                    publisher = (
                        self._get_meta_value(item, "source")
                        or self._get_meta_value(item, "fwdw")
                    )

                    time.sleep(self._delay)
                    abstract = self._fetch_detail_abstract(url)

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] abstract too short ({len(abstract)} chars) "
                              f"for '{title[:50]}' — skipping")
                        continue

                    self._save_paper({
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": url,
                        "pdf_url": None,
                        "original_filename": None,
                        "publisher": publisher or "国家税务总局",
                        "authors": None,
                        "keywords": None,
                        "doi": None,
                        "category": "政府信息公开年报",
                        "department": None,
                        "metadata": json.dumps({
                            "posted_date": pub_time_str,
                            "source": publisher,
                            "channel_id": _CHANNEL_ID,
                        }, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            if not page_had_new:
                print(f"[{self.site_id}] page {page}: all items already seen. Done.")
                break

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
