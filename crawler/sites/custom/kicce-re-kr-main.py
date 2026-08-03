# -*- coding: utf-8 -*-
"""Crawler for KICCE statistics data collection board (통계자료집).

Starting URL: https://www.kicce.re.kr/main/board/index.do?menu_idx=35&manage_idx=43

List page:   /main/board/index.do?menu_idx=35&manage_idx=43&nowPage=N
Detail page: /main/board/view.do?board_idx=XXXXX&menu_idx=35&manage_idx=43

Items are linked via onclick="viewBoard(XXXXX)".
Content in <span class="tit-ico ico1"> (목차/TOC) and ico2 (요약문/summary).
File download: /board/boardFile/download/{manage_idx}/{board_idx}/{file_idx}.do
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.kicce.re.kr"
_MENU_IDX = "35"
_MANAGE_IDX = "43"
_LIST_URL = (
    f"{_BASE}/main/board/index.do"
    f"?menu_idx={_MENU_IDX}&manage_idx={_MANAGE_IDX}"
)
_RATE_SLEEP = 1.0
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _curl_get(url):
    """GET via curl with TLS workaround. Retries 3× with backoff. Returns bytes or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL",
        "--max-time", "30",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        url,
    ]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception as exc:
            print(f"[kicce-re-kr-main] curl attempt {attempt + 1}/3 failed for {url}: {exc}")
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


def _decode(raw):
    """Decode bytes to str; falls back to errors='replace'."""
    for enc in ("utf-8", "euc-kr"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _make_soup(html):
    """Parse HTML with BeautifulSoup; falls back through html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
    except Exception:
        pass
    return None


def _text(node):
    """Return normalised plain text from a BeautifulSoup node or raw HTML string."""
    if node is None:
        return ""
    if hasattr(node, "get_text"):
        t = node.get_text(separator=" ")
    else:
        t = re.sub(r"<[^>]+>", " ", str(node))
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KICCEMainCrawler(BaseCrawler):
    """Crawler for 육아정책연구소 통계자료집 (statistics data collection)."""

    site_id = "kicce-re-kr-main"
    site_name = "Custom: kicce-re-kr-main"
    base_url = "https://www.kicce.re.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_val = limit if limit is not None else float("inf")
        lim_str = str(limit) if limit is not None else "inf"
        start_time = time.time()

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget guard
            if time.time() - start_time > _WALL_BUDGET_SEC:
                print(
                    f"[kicce-re-kr-main] wall-clock budget reached at page {page}, stopping"
                )
                break

            if page == _MAX_PAGES:
                print(
                    f"[kicce-re-kr-main] safety cap of {_MAX_PAGES} pages reached, stopping"
                )

            # Progress log every 10 pages
            if page % 10 == 0:
                print(f"[kicce-re-kr-main] page {page}: saved {saved}/{lim_str}")

            # Fetch list page
            list_url = f"{_LIST_URL}&nowPage={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[kicce-re-kr-main] failed to fetch list page {page}, stopping")
                break

            html = _decode(raw)
            items = self._parse_list(html)

            if not items:
                print(f"[kicce-re-kr-main] page {page}: empty list, stopping")
                break

            # URL-based deduplication detects pagination loop-back
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(
                    f"[kicce-re-kr-main] page {page}: all items already seen, stopping"
                )
                break

            for it in new_items:
                if saved >= limit_val:
                    break
                seen_urls.add(it["url"])

                try:
                    detail = self._fetch_detail(it)
                    if detail is None:
                        print(
                            f"[kicce-re-kr-main] item {it['board_idx']}: "
                            "detail fetch failed, skipping"
                        )
                        continue

                    abstract = detail.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[kicce-re-kr-main] item {it['board_idx']} "
                            f"'{it['title'][:40]}': abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue

                    paper = {
                        "id": f"{self.site_id}_{it['board_idx']}",
                        "site_id": self.site_id,
                        "external_id": it["board_idx"],
                        "title": it["title"],
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "통계자료집",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": it.get("date", ""),
                        "url": (
                            f"{_BASE}/main/board/view.do"
                            f"?board_idx={it['board_idx']}"
                            f"&menu_idx={_MENU_IDX}&manage_idx={_MANAGE_IDX}"
                        ),
                        "pdf_url": detail.get("pdf_url", ""),
                        "doi": "",
                        "department": "육아정책연구소",
                        "metadata": json.dumps(
                            detail.get("meta", {}), ensure_ascii=False
                        ),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[kicce-re-kr-main] saved [{saved}/{lim_str}] "
                        f"{it['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[kicce-re-kr-main] item {it.get('board_idx', '?')} "
                        f"failed: {exc}"
                    )
                    continue

                time.sleep(_RATE_SLEEP)

            if saved >= limit_val:
                break

        return saved

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list(self, html):
        """Parse the board list page. Returns list of {board_idx, title, date, url}."""
        items = []
        soup = _make_soup(html)

        if soup is not None:
            try:
                for a_tag in soup.find_all(
                    True, onclick=re.compile(r"viewBoard\(\d+\)")
                ):
                    onclick = a_tag.get("onclick", "")
                    m = re.search(r"viewBoard\((\d+)\)", onclick)
                    if not m:
                        continue
                    bid = m.group(1)
                    url = (
                        f"/main/board/view.do?board_idx={bid}"
                        f"&menu_idx={_MENU_IDX}&manage_idx={_MANAGE_IDX}"
                    )

                    # Title: prefer title attr (contains item name), else span text
                    title = (
                        a_tag.get("title", "")
                        .replace("상세보기로 이동합니다.", "")
                        .strip()
                    )
                    if not title:
                        span = a_tag.find("span")
                        title = _text(span) if span else _text(a_tag)
                    title = title or f"Board {bid}"

                    # Date: walk up to <tr>, find the date <td>
                    date = ""
                    tr = a_tag.find_parent("tr")
                    if tr:
                        date_td = tr.find("td", class_=re.compile(r"date"))
                        if date_td:
                            date = _text(date_td)

                    items.append(
                        {"board_idx": bid, "title": title, "date": date, "url": url}
                    )
            except Exception as exc:
                print(
                    f"[kicce-re-kr-main] soup list parse error: {exc}, "
                    "falling back to regex"
                )
                items = []

        if not items:
            # Regex fallback
            board_ids = re.findall(r"viewBoard\((\d+)\)", html)
            titles_raw = re.findall(
                r'title="([^"]+?)(?:\s+상세보기로 이동합니다\.)?"\s', html
            )
            dates = re.findall(r'class="date[^"]*"[^>]*>\s*([\d.]+)\s*<', html)
            for i, bid in enumerate(board_ids):
                url = (
                    f"/main/board/view.do?board_idx={bid}"
                    f"&menu_idx={_MENU_IDX}&manage_idx={_MANAGE_IDX}"
                )
                title = (
                    titles_raw[i].strip() if i < len(titles_raw) else f"Item {bid}"
                )
                date = dates[i] if i < len(dates) else ""
                items.append(
                    {"board_idx": bid, "title": title, "date": date, "url": url}
                )

        return items

    def _fetch_detail(self, it):
        """Fetch and parse a detail page. Returns {abstract, pdf_url, meta} or None."""
        bid = it["board_idx"]
        detail_url = (
            f"{_BASE}/main/board/view.do?board_idx={bid}"
            f"&menu_idx={_MENU_IDX}&manage_idx={_MANAGE_IDX}"
        )

        raw = _curl_get(detail_url)
        if not raw:
            return None

        html = _decode(raw)
        toc_text = ""
        summary_text = ""

        # BeautifulSoup extraction of content sections
        soup = _make_soup(html)
        if soup is not None:
            try:
                for span in soup.find_all(
                    "span", class_=re.compile(r"tit-ico")
                ):
                    ico_classes = span.get("class", [])
                    ico_str = (
                        " ".join(ico_classes)
                        if isinstance(ico_classes, list)
                        else str(ico_classes)
                    )
                    parent_div = span.find_parent(
                        "div", class_=re.compile(r"view-type")
                    )
                    if not parent_div:
                        continue
                    # Collect child text, skipping the h3 heading element
                    parts = []
                    for child in parent_div.children:
                        if getattr(child, "name", None) == "h3":
                            continue
                        parts.append(_text(child))
                    content = re.sub(r"\s+", " ", " ".join(parts)).strip()

                    if "ico1" in ico_str:
                        toc_text = content
                    elif "ico2" in ico_str:
                        summary_text = content
            except Exception as exc:
                print(
                    f"[kicce-re-kr-main] soup detail parse error for {bid}: {exc}"
                )
                toc_text = ""
                summary_text = ""

        # Regex fallback when soup yielded nothing
        if not toc_text and not summary_text:
            icos = re.findall(
                r'<span class="tit-ico (ico\d+)"></span>.*?</h3>'
                r'(.*?)(?=<h3>|</div>\s*</div>)',
                html,
                re.DOTALL,
            )
            for ico_class, content in icos:
                t = re.sub(r"<[^>]+>", " ", content)
                t = re.sub(r"\s+", " ", t).strip()
                if "ico1" in ico_class:
                    toc_text = t
                elif "ico2" in ico_class:
                    summary_text = t

        # Build abstract: combine both sections for maximum coverage.
        # TOC (목차) tends to be long; summary (요약문) may be richer but shorter.
        if toc_text and summary_text:
            if len(summary_text) >= 100:
                abstract = summary_text + " " + toc_text
            else:
                abstract = toc_text + " " + summary_text
        elif toc_text:
            abstract = toc_text
        else:
            abstract = summary_text

        abstract = re.sub(r"\s+", " ", abstract).strip()

        # File download URL (HWP or PDF)
        pdf_url = ""
        fm = re.search(r"/board/boardFile/download/\d+/\d+/\d+\.do", html)
        if fm:
            pdf_url = f"{_BASE}{fm.group(0)}"

        meta: dict = {
            "board_idx": bid,
            "manage_idx": _MANAGE_IDX,
            "menu_idx": _MENU_IDX,
        }
        if toc_text:
            meta["toc"] = toc_text
        if summary_text:
            meta["summary_raw"] = summary_text

        return {"abstract": abstract, "pdf_url": pdf_url, "meta": meta}
