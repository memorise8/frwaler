# -*- coding: utf-8 -*-
"""경기도뉴스포털(gnews.gg.go.kr) 보도자료 board crawler.

Target: https://gnews.gg.go.kr/briefing/brief_gongbo.do

Site structure (discovered via curl, 2026-07):

- List page: GET /briefing/brief_gongbo.do?page=N&BS_CODE=s017&period_1=
  &period_2=&search=0&keyword=&subject_Code=BO01 — a JSP board rendered
  server-side (no JSON API), 10 rows per page in a <table>. Each row has:
    * td.num       — board-displayed article number ("번호"), decreasing by
                      1 per row, newest = highest. Used as ``post_number``.
    * td.tit > a    — title + href containing ``number=<id>`` (the site's
                      native/internal article id used to fetch the detail
                      page). Used as ``external_id``.
    * td.dp        — 담당부서 (department)
    * td.date      — 등록일 (list-page date, date only, no time)
- Detail page: GET /briefing/brief_gongbo_view.do?BS_CODE=s017&number=<id>
  &subject_Code=BO01 — contains the full article body (div.postBody),
  담당부서/작성일시 (div.postinfo) and attachments (span.file-item with
  data-url/data-name attributes; formats seen are .hwpx/.jpg, no native
  PDFs on this board).
- The site's WAF occasionally returns a tiny EUC-KR "access blocked" page
  (HTTP 200) when requests come in too fast without a persisted session;
  using a shared ``requests.Session`` (via ``self._session``) with >=1s
  pacing between requests avoids this in practice. ``_is_blocked_page``
  detects the block page defensively so a crawl run stops cleanly instead
  of looping on garbage.
"""

import json
import os
import re
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_LIST_PATH = "/briefing/brief_gongbo.do"
_DETAIL_PATH = "/briefing/brief_gongbo_view.do"
_BS_CODE = "s017"
_SUBJECT_CODE = "BO01"
_PUBLISHER = "경기도"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_ABSTRACT_MIN_CHARS = 100
_RETRY_DELAYS = (1, 3, 9)


def _make_soup(html: str):
    """Build BeautifulSoup with parser fallback chain (html5lib -> lxml -> html.parser)."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _is_blocked_page(text: str) -> bool:
    """Detect the site's WAF/access-block placeholder page (tiny EUC-KR body)."""
    return "charset=euc-kr" in text.lower()


def _parse_date(raw: str) -> str:
    """Convert '2026.07.18' or '2026.07.18 18:04:28' -> '2026-07-18'. '' on failure."""
    if not raw:
        return ""
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", raw)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


class GnewsGgGoKrBriefingCrawler(BaseCrawler):
    site_id = "gnews-gg-go-kr-briefing"
    site_name = "Custom: gnews-gg-go-kr-briefing"
    base_url = "https://gnews.gg.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 1
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget exceeded ({_MAX_WALL_SECONDS}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            raw = self._get(_LIST_PATH, params={
                "page": page,
                "BS_CODE": _BS_CODE,
                "period_1": "",
                "period_2": "",
                "search": "0",
                "keyword": "",
                "subject_Code": _SUBJECT_CODE,
            })
            if raw is None:
                print(f"[{self.site_id}] Failed to fetch list page {page} after retries. Stopping.")
                break

            if _is_blocked_page(raw):
                print(f"[{self.site_id}] Access blocked (rate-limited) on list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{self.site_id}] HTML parse error on list page {page}: {exc}. Stopping.")
                break

            rows = [r for r in soup.find_all("tr") if r.find("td", class_="num")]
            if not rows:
                print(f"[{self.site_id}] No data rows on page {page}. Done.")
                break

            new_on_page = 0
            for row in rows:
                if limit is not None and saved >= limit:
                    break
                try:
                    processed, did_save = self._process_row(row, seen_urls, saved, limit_str)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] row processing failed: {exc}")
                    continue
                if processed:
                    new_on_page += 1
                if did_save:
                    saved += 1

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # Only end the crawl when a page yields zero *new* (non-duplicate)
            # rows — a page whose items were all skipped for short abstracts
            # still counts as "new" here, so pagination correctly continues
            # past runs of filtered-out items instead of stopping early.
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page}. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _get(self, path_or_url, params=None):
        """GET with rate-limit pacing + retry/backoff (1s, 3s, 9s). Returns text or None."""
        url = path_or_url if path_or_url.startswith("http") else urljoin(self.base_url, path_or_url)
        last_exc = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            time.sleep(self._delay)
            try:
                resp = self._session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                resp.encoding = "utf-8"
                return resp.text
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] request error ({attempt + 1}/{len(_RETRY_DELAYS) + 1}) for {url}: {exc}")
                if attempt < len(_RETRY_DELAYS):
                    wait = _RETRY_DELAYS[attempt]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] giving up on {url}: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Per-row processing (isolated)
    # ------------------------------------------------------------------

    def _process_row(self, row, seen_urls: set, saved: int, limit_str: str):
        """Process one list row.

        Returns ``(processed, did_save)``: ``processed`` is True whenever the
        row is a genuinely new (non-duplicate) item — used by the caller to
        detect real end-of-pagination vs. a run of filtered-out items —
        while ``did_save`` is True only if a document was persisted.
        """
        num_td = row.find("td", class_="num")
        tit_td = row.find("td", class_="tit")
        if not num_td or not tit_td:
            return False, False

        a_tag = tit_td.find("a")
        if not a_tag:
            return False, False

        href = a_tag.get("href", "")
        m = re.search(r"number=(\d+)", href)
        if not m:
            return False, False
        number = m.group(1)

        detail_url = f"{self.base_url}{_DETAIL_PATH}?BS_CODE={_BS_CODE}&number={number}&subject_Code={_SUBJECT_CODE}"
        if detail_url in seen_urls:
            return False, False
        seen_urls.add(detail_url)

        num_text = num_td.get_text(strip=True)
        num = num_text if num_text.isdigit() else None

        dp_td = row.find("td", class_="dp")
        department = dp_td.get_text(strip=True) if dp_td else ""

        date_td = row.find("td", class_="date")
        listed_date = _parse_date(date_td.get_text(strip=True)) if date_td else ""

        list_title = a_tag.get_text(strip=True)

        detail = self._fetch_detail(detail_url, number)
        if detail is None:
            print(f"[{self.site_id}] item {number} detail fetch failed, skipping")
            return True, False

        abstract = detail.get("abstract", "")
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{self.site_id}] item {number} abstract too short "
                  f"({len(abstract)} chars < {_ABSTRACT_MIN_CHARS}), skipping")
            return True, False

        title = detail.get("title") or list_title
        published_date = detail.get("published_date") or listed_date
        department = detail.get("department") or department
        pdf_url = detail.get("pdf_url") or ""
        orig_filename = detail.get("original_filename") or ""
        attachments = detail.get("attachments", [])

        paper = {
            "site_id": self.site_id,
            "external_id": number,
            "post_number": num or number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": "",
            "publisher": _PUBLISHER,
            "department": department,
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": "보도자료",
            "doi": "",
            "original_filename": orig_filename,
            "metadata": json.dumps({
                "posted_date": listed_date,
                "originalFilename": orig_filename,
                "number": number,
                "num": num,
                "BS_CODE": _BS_CODE,
                "subject_Code": _SUBJECT_CODE,
                "department": department,
                "attachments": attachments,
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
        counter = f"{saved + 1}/{limit_str}"
        print(f"[{self.site_id}] saved {counter}: {title[:60]}")
        return True, True

    # ------------------------------------------------------------------
    # Detail page fetch
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str, number: str):
        raw = self._get(url)
        if raw is None:
            return None

        if _is_blocked_page(raw):
            print(f"[{self.site_id}] item {number} detail request blocked (rate-limited)")
            return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error {number}: {exc}")
            return None

        contents = soup.find("div", id="contents") or soup

        title = ""
        for h in contents.find_all("h3"):
            classes = h.get("class") or []
            if "fileset26-title" in classes:
                continue
            title = h.get_text(strip=True)
            break

        published_date = ""
        department = ""
        postinfo = contents.find("div", class_="postinfo")
        if postinfo:
            full_text = postinfo.get_text(" ", strip=True)
            dm = re.search(r"담당부서\s*(\S+)", full_text)
            if dm:
                department = dm.group(1)
            tm = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*\d{2}:\d{2}:\d{2}", full_text)
            if tm:
                published_date = _parse_date(tm.group(1))

        abstract = ""
        body_div = contents.find("div", class_="postBody")
        if body_div:
            abstract = re.sub(r"\s+", " ", body_div.get_text(" ", strip=True)).strip()

        attachments = []
        for span in contents.select("span.file-item"):
            file_url = span.get("data-url")
            if not file_url:
                continue
            attachments.append({
                "url": urljoin(self.base_url, file_url),
                "filename": span.get("data-name") or "",
            })

        pdf_url = None
        orig_filename = None
        for att in attachments:
            if att["filename"].lower().endswith(".pdf"):
                pdf_url = att["url"]
                orig_filename = att["filename"]
                break
        if not pdf_url:
            for att in attachments:
                if not att["filename"].lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                    pdf_url = att["url"]
                    orig_filename = att["filename"]
                    break
        if not pdf_url and attachments:
            pdf_url = attachments[0]["url"]
            orig_filename = attachments[0]["filename"]

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "department": department,
            "pdf_url": pdf_url,
            "original_filename": orig_filename,
            "attachments": attachments,
        }
