# -*- coding: utf-8 -*-
"""산림청 보도자료 crawler (forest.go.kr kfsweb BBS).

List:   https://www.forest.go.kr/kfsweb/cop/bbs/selectBoardList.do?bbsId=BBSMSTR_1036&mn=NKFS_04_02_01&pageIndex=N
Detail: https://www.forest.go.kr/kfsweb/cop/bbs/selectBoardArticle.do?nttId=NTTID&bbsId=BBSMSTR_1036&mn=NKFS_04_02_01
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.forest.go.kr"
_BBS_ID = "BBSMSTR_1036"
_MN = "NKFS_04_02_01"
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_DETAIL_SLEEP = 1.0
_MAX_RUNTIME_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


def _make_soup(html: str, context: str = "html"):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html or "", parser)
        except Exception as exc:
            print(f"[forest-go-kr-kfsweb] BeautifulSoup({parser}) failed ({context}): {exc}")
    return None


def _strip_tags(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class ForestGoKrKfswebCrawler(BaseCrawler):
    """산림청 보도자료 crawler."""

    site_id = "forest-go-kr-kfsweb"
    site_name = "Custom: forest-go-kr-kfsweb"
    base_url = "https://www.forest.go.kr"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, referer: str | None = None) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw and raw.strip():
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    print(f"[{self.site_id}] Empty response (attempt {attempt+1}/3), "
                          f"retrying in {waits[attempt]}s... URL: {url[:80]}")
                    time.sleep(waits[attempt])
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    print(f"[{self.site_id}] Timeout (attempt {attempt+1}/3), "
                          f"retrying in {waits[attempt]}s...")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error: {exc} (attempt {attempt+1}/3), "
                          f"retrying in {waits[attempt]}s...")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list(self, html: str) -> list[dict[str, str]]:
        """Parse list page; return list of {nttId, title, date, preview}."""
        items: list[dict[str, str]] = []
        seen_ids: set[str] = set()

        soup = _make_soup(html, "list")
        if not soup:
            return items

        for a_tag in soup.find_all("a", href=re.compile(r"nttId=\d+")):
            href = a_tag.get("href", "")
            m = re.search(r"nttId=(\d+)", href)
            if not m:
                continue
            ntt_id = m.group(1)
            if ntt_id in seen_ids:
                continue
            seen_ids.add(ntt_id)

            # Title: prefer title attribute, fallback to <strong> text
            title = a_tag.get("title", "").strip()
            if not title:
                strong = a_tag.find("strong")
                if strong:
                    title = strong.get_text(separator=" ", strip=True)
                    # Remove leading [org] tag like [중부지방산림청]
                    title = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()

            # Date from <span class="sa_date">
            date = ""
            date_span = a_tag.find("span", class_="sa_date")
            if date_span:
                date = date_span.get_text(strip=True)

            # Preview paragraph
            preview = ""
            p_tag = a_tag.find("p")
            if p_tag:
                preview = p_tag.get_text(separator=" ", strip=True)

            if ntt_id:
                items.append({
                    "nttId": ntt_id,
                    "title": title,
                    "date": date,
                    "preview": preview,
                })

        return items

    def _parse_detail(self, html: str, ntt_id: str) -> dict[str, Any]:
        """Parse detail page; return dict with extracted fields."""
        result: dict[str, Any] = {
            "title": "",
            "published_date": "",
            "author": "",
            "department": "",
            "abstract": "",
            "file_links": [],
        }

        soup = _make_soup(html, f"detail nttId={ntt_id}")
        if not soup:
            return result

        # Title from <div class="b_info"><strong>
        b_info = soup.find("div", class_="b_info")
        if b_info:
            strong = b_info.find("strong")
            if strong:
                result["title"] = strong.get_text(separator=" ", strip=True)

        # Date and author from <ul class="bd_view_ul_info">
        ul = soup.find("ul", class_="bd_view_ul_info")
        if ul:
            for li in ul.find_all("li"):
                spans = li.find_all("span")
                if len(spans) >= 2:
                    label = spans[0].get_text(strip=True)
                    value = spans[1].get_text(separator=" ", strip=True)
                    if "작성일" in label:
                        result["published_date"] = value.strip()
                    elif "작성자" in label:
                        # "부여국유림관리소 / 공다현 / 041-830-5012"
                        raw = value.strip()
                        parts = [p.strip() for p in raw.split("/")]
                        # Remove phone numbers
                        non_phone = [p for p in parts if not re.match(r"^[\d\-\(\)\s]+$", p)]
                        if len(non_phone) >= 2:
                            result["department"] = non_phone[0]
                            result["author"] = non_phone[1]
                        elif len(non_phone) == 1:
                            result["author"] = non_phone[0]
                        else:
                            result["author"] = raw

        # Abstract — primary: responsiveVoice hidden input (clean plain text)
        rv = soup.find("input", id="responsiveVoice")
        if rv:
            result["abstract"] = rv.get("value", "").strip()

        # Fallback: strip tags from <div class="b_content">
        if not result["abstract"]:
            b_content = soup.find("div", class_="b_content")
            if b_content:
                result["abstract"] = _strip_tags(str(b_content))

        # File attachments from <dl class="b_file">
        file_links: list[str] = []
        seen_links: set[str] = set()

        b_file = soup.find("dl", class_="b_file")
        candidates = b_file.find_all("a", href=re.compile(r"FileDown\.do")) if b_file else []

        # Fallback: look anywhere in the page
        if not candidates:
            candidates = soup.find_all("a", href=re.compile(r"FileDown\.do"))

        for a in candidates:
            href = a.get("href", "")
            # Strip jsessionid segment
            href = re.sub(r";jsessionid=[^?]+", "", href)
            href = href.replace("&amp;", "&")
            if not href or href in seen_links:
                continue
            seen_links.add(href)
            full = (_BASE + href) if href.startswith("/") else href
            file_links.append(full)

        result["file_links"] = file_links
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 산림청 보도자료 board pages and save each article.

        Parameters
        ----------
        limit:
            Maximum number of records to save. None means unlimited.
        """
        saved = 0
        seen_ntt_ids: set[str] = set()
        start_time = time.time()
        limit_label = str(limit) if limit is not None else "∞"
        list_referer = (
            f"{_BASE}/kfsweb/cop/bbs/selectBoardList.do"
            f"?bbsId={_BBS_ID}&mn={_MN}"
        )

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget
            if time.time() - start_time > _MAX_RUNTIME_S:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached at page {page}. Stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_url = (
                f"{_BASE}/kfsweb/cop/bbs/selectBoardList.do"
                f"?bbsId={_BBS_ID}&mn={_MN}&pageIndex={page}&pageUnit={_PAGE_SIZE}"
            )
            raw_list = self._curl_get(list_url)
            if not raw_list:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list(raw_list)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # Deduplication — detect silent pagination loops
            new_items = [it for it in items if it["nttId"] not in seen_ntt_ids]
            if not new_items:
                print(f"[{self.site_id}] Page {page}: all items already seen. Done.")
                break
            for it in items:
                seen_ntt_ids.add(it["nttId"])

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                ntt_id = item["nttId"]
                detail_url = (
                    f"{_BASE}/kfsweb/cop/bbs/selectBoardArticle.do"
                    f"?nttId={ntt_id}&bbsId={_BBS_ID}&mn={_MN}"
                )

                try:
                    time.sleep(_DETAIL_SLEEP)

                    raw_detail = self._curl_get(detail_url, referer=list_referer)
                    if not raw_detail:
                        print(f"[{self.site_id}] item {ntt_id} failed: could not fetch detail")
                        continue

                    detail = self._parse_detail(raw_detail, ntt_id)

                    title = detail["title"] or item["title"]
                    if not title:
                        title = f"산림청 보도자료 {ntt_id}"

                    # Abstract: detail body first, fall back to list preview
                    abstract = detail["abstract"]
                    if not abstract or len(abstract) < 100:
                        preview = item["preview"]
                        if len(preview) > len(abstract):
                            abstract = preview

                    if len(abstract) < 100:
                        print(f"[{self.site_id}] item {ntt_id} skipped: "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    # Date: normalize to YYYY-MM-DD
                    raw_date = detail["published_date"] or item["date"]
                    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", raw_date)
                    pub_date = (
                        f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                        if m else raw_date
                    )

                    file_links = detail["file_links"]
                    pdf_url = file_links[0] if file_links else None

                    paper: dict[str, Any] = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": ntt_id,
                        "title": title,
                        "authors": detail["author"] or "",
                        "abstract": abstract,
                        "category": "보도자료",
                        "keywords": "",
                        "published_date": pub_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": detail["department"] or "산림청",
                        "metadata": json.dumps({
                            "posted_date": item["date"],
                            "nttId": ntt_id,
                            "bbsId": _BBS_ID,
                            "author_raw": f"{detail['department']} / {detail['author']}".strip(" /"),
                            "preview": item["preview"],
                            "file_links": file_links,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {ntt_id} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
