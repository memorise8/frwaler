# -*- coding: utf-8 -*-
"""Ulsan Metropolitan City administrative publications BBS crawler.

BBS: 행정 간행물 (BBS_0000000000000170)
List:   POST /u/rep/bbs/list.ulsan?bbsId=BBS_0000000000000170&mId=001003007001000000
        with form field page=N  (5 items/page, ~12 pages)
Detail: GET  /u/rep/bbs/view.do?bbsId=...&mId=...&dataId={dataId}
PDF:    /u/enc/media/bbsFileDown.do?bbsId=...&atchFileId={enc}&fileSn={enc}
        (encrypted params extracted from HHBbs.EncDownFile onclick)
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import quote

from crawler.base_crawler import BaseCrawler

_BBS_ID = "BBS_0000000000000170"
_MID = "001003007001000000"
_BASE = "https://www.ulsan.go.kr"
_LIST_URL = f"{_BASE}/u/rep/bbs/list.ulsan?bbsId={_BBS_ID}&mId={_MID}"
_VIEW_BASE = f"{_BASE}/u/rep/bbs/view.do"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _curl_raw(url: str, *, post_data: str = None, timeout: int = 30) -> bytes:
    """Fetch via curl --tls-max 1.3 -sk; retry 3x with exponential backoff."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
    ]
    if post_data is not None:
        cmd += [
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "--data", post_data,
        ]
    cmd.append(url)

    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception as exc:
            print(f"[ulsan-go-kr-u] curl error (attempt {attempt + 1}/3): {exc}")
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _make_soup(text: str):
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


class UlsanGoKrUCrawler(BaseCrawler):
    site_id = "ulsan-go-kr-u"
    site_name = "Custom: ulsan-go-kr-u"
    base_url = "https://www.ulsan.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_val = limit if limit is not None else float("inf")
        lim_str = str(limit) if limit is not None else "inf"
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        for page in range(1, MAX_PAGES + 1):
            if time.time() - start_time > MAX_WALL:
                print(f"[ulsan-go-kr-u] 25-min wall-clock budget reached at page {page}, stopping.")
                break
            if saved >= limit_val:
                break
            if page == MAX_PAGES:
                print(f"[ulsan-go-kr-u] Safety cap of {MAX_PAGES} pages reached, stopping.")
            if page % 10 == 0:
                print(f"[ulsan-go-kr-u] page {page}: saved {saved}/{lim_str}")

            raw = _curl_raw(_LIST_URL, post_data=f"page={page}")
            if raw is None:
                print(f"[ulsan-go-kr-u] Failed to fetch list page {page}, stopping.")
                break

            html = _decode(raw)
            items = self._parse_list(html)
            if not items:
                print(f"[ulsan-go-kr-u] No items on page {page}, done.")
                break

            new_items = [
                (num, did) for num, did in items
                if f"{_VIEW_BASE}?bbsId={_BBS_ID}&mId={_MID}&dataId={did}" not in seen_urls
            ]
            if not new_items:
                print(f"[ulsan-go-kr-u] All items on page {page} already seen, done.")
                break

            for post_num, data_id in new_items:
                if saved >= limit_val:
                    break

                detail_url = f"{_VIEW_BASE}?bbsId={_BBS_ID}&mId={_MID}&dataId={data_id}"
                seen_urls.add(detail_url)

                try:
                    paper = self._fetch_detail(data_id, post_num, detail_url)
                    if paper is None:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[ulsan-go-kr-u] Skipping dataId={data_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ulsan-go-kr-u] item dataId={data_id} failed: {exc}")
                    continue

                time.sleep(self._delay)

        print(f"[ulsan-go-kr-u] Done. Saved {saved} items.")
        return saved

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, html: str) -> list:
        """Return [(post_number, data_id), ...] from list HTML."""
        # fn_view('NNNNN') onclick is the primary source
        fn_ids = re.findall(r"fn_view\('(\d+)'\)", html)
        # Fallback: href dataId=NNNNN pattern
        href_ids = re.findall(r"dataId=(\d+)", html)

        seen: set = set()
        data_ids = []
        for did in (fn_ids or href_ids):
            if did not in seen:
                seen.add(did)
                data_ids.append(did)

        # Sequential post numbers from first column of the table
        soup = _make_soup(html)
        post_nums = []
        if soup:
            for tr in soup.select("table tbody tr"):
                tds = tr.find_all("td")
                if tds:
                    num = tds[0].get_text(strip=True)
                    if num.isdigit():
                        post_nums.append(num)

        return [
            (post_nums[i] if i < len(post_nums) else did, did)
            for i, did in enumerate(data_ids)
        ]

    # ------------------------------------------------------------------
    # Detail page fetch + parse
    # ------------------------------------------------------------------

    def _fetch_detail(self, data_id: str, post_num: str, detail_url: str) -> dict:
        raw = None
        for attempt in range(3):
            raw = _curl_raw(detail_url)
            if raw:
                break
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[ulsan-go-kr-u] retry {attempt + 1}/3 for dataId={data_id} in {wait}s")
                time.sleep(wait)

        if not raw:
            print(f"[ulsan-go-kr-u] Failed to fetch detail dataId={data_id}, skipping.")
            return None

        html = _decode(raw)
        soup = _make_soup(html)
        if soup is None:
            return None

        # ── Metadata table (제목/작성자/작성일자/발행년도/발행부서/첨부파일/내용) ──
        meta = {}
        cb = soup.find("div", class_="content_box")
        if cb:
            for tr in cb.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True)
                    val = cells[1].get_text(" ", strip=True)
                    meta[key] = val

        title = meta.get("제목") or f"울산광역시 행정간행물 {data_id}"
        author = meta.get("작성자", "")
        raw_date = meta.get("작성일자", "")
        pub_year = meta.get("발행년도", "")
        department = meta.get("발행부서", "")
        view_count = meta.get("조회수", "")
        att_raw = meta.get("첨부파일", "")

        # "2026.03.26" → "2026-03-26"
        published_date = ""
        dm = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", raw_date)
        if dm:
            published_date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"

        # ── Abstract ──────────────────────────────────────────────────
        # Primary: text content from the 내용 row
        content_text = ""
        if cb:
            for tr in cb.find_all("tr"):
                for th in tr.find_all("th"):
                    if "내용" in th.get_text():
                        tds = tr.find_all("td")
                        if tds:
                            content_text = re.sub(
                                r"\s+", " ", tds[0].get_text(" ", strip=True)
                            ).strip()

        # Secondary: sibling elements rendered after the HWP editor placeholder
        hwp_text = ""
        hwp = soup.find(id="hwpEditorBoardContent")
        if hwp:
            parts = []
            for sib in hwp.next_siblings:
                if hasattr(sib, "get_text"):
                    t = sib.get_text(" ", strip=True)
                    if t:
                        parts.append(t)
            hwp_text = re.sub(r"\s+", " ", " ".join(parts)).strip()

        abstract = hwp_text if len(hwp_text) > len(content_text) else content_text

        # Fallback: synthesize from full metadata when abstract is too short
        if len(abstract) < 100:
            parts = [f"[울산광역시 행정간행물] {title}"]
            info_bits = []
            if author:
                info_bits.append(f"작성자: {author}")
            if raw_date:
                info_bits.append(f"작성일자: {raw_date}")
            if pub_year:
                info_bits.append(f"발행년도: {pub_year}")
            if department:
                info_bits.append(f"발행부서: {department}")
            if info_bits:
                parts.append(" | ".join(info_bits))
            if att_raw:
                att_clean = re.sub(r"\s*\([\d.]+[KMG]Byte\)\s*", " ", att_raw)
                att_clean = re.sub(r"\s*(미리보기|미리듣기)\s*", " ", att_clean)
                att_clean = re.sub(r"\s+", " ", att_clean).strip()
                if att_clean:
                    parts.append(f"첨부파일: {att_clean}")
            if abstract:
                parts.append(abstract)
            abstract = " ".join(parts)

        abstract = re.sub(r"\s{2,}", " ", abstract).strip()

        # ── PDF file (first non-image attachment) ─────────────────────
        pdf_url = None
        original_filename = None

        file_items = (cb or soup).select(".file-download-item") if (cb or soup) else []

        for item in file_items:
            # Download link with EncDownFile onclick
            a_tag = None
            for a in item.find_all("a"):
                if "EncDownFile" in (a.get("onclick") or ""):
                    a_tag = a
                    break
            if not a_tag:
                continue

            m = re.search(
                r"EncDownFile\(['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"],"
                r"\s*['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"]",
                a_tag.get("onclick", ""),
            )
            if not m:
                continue
            ctx, bbs_id_v, enc_id, enc_sn = m.groups()

            # Filename from the preview button title
            fname = ""
            for btn in item.find_all("button"):
                t = btn.get("title", "")
                if re.search(r"\.[a-zA-Z]{2,5}", t):
                    fname = t
                    break

            # Skip cover images
            if re.search(r"\.(jpg|jpeg|png|gif|bmp)(\s|\(|$)", fname, re.I):
                continue

            pdf_url = (
                f"{_BASE}{ctx}/enc/media/bbsFileDown.do"
                f"?bbsId={bbs_id_v}"
                f"&atchFileId={quote(enc_id)}"
                f"&fileSn={quote(enc_sn)}"
            )
            clean = re.sub(r"\s*\([\d.]+[KMG]Byte\)\s*", "", fname).strip()
            clean = re.sub(r"^[★\s]+", "", clean).strip()
            original_filename = clean or None
            break

        # Regex fallback if BeautifulSoup found nothing
        if not pdf_url:
            all_enc = re.findall(
                r"EncDownFile\(['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"],"
                r"\s*['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"]",
                html,
            )
            if all_enc:
                ctx, bbs_id_v, enc_id, enc_sn = all_enc[0]
                pdf_url = (
                    f"{_BASE}{ctx}/enc/media/bbsFileDown.do"
                    f"?bbsId={bbs_id_v}"
                    f"&atchFileId={quote(enc_id)}"
                    f"&fileSn={quote(enc_sn)}"
                )

        return {
            "site_id": self.site_id,
            "external_id": data_id,
            "post_number": post_num,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": author or None,
            "publisher": "울산광역시",
            "department": department or None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": "행정간행물",
            "keywords": None,
            "doi": None,
            "metadata": json.dumps(
                {
                    "dataId": data_id,
                    "post_number": post_num,
                    "posted_date": raw_date,
                    "pub_year": pub_year,
                    "view_count": view_count,
                },
                ensure_ascii=False,
            ),
        }
