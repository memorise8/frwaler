# -*- coding: utf-8 -*-
"""산림청 산림통계포털 통계자료실 crawler.

Target: https://kfss.forest.go.kr/stat/ptl/article/articleList.do?curMenu=9795&bbsId=ptlPdsBase

API endpoints discovered:
  List  : GET /stat/ptl/article/selectArticleList.do
  Files : GET /stat/ptl/article/selectArticleFileList.do
  Down  : GET /stat/ptl/article/articleFileDown.do?fileSeq=X&workSeq=X
"""

import json
import re
import subprocess
import time
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler

_BASE = "https://kfss.forest.go.kr"
_CTX = "/stat"


def _make_soup(raw):
    """BeautifulSoup with html5lib → lxml → html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _strip_html(html):
    """Strip HTML tags and collapse whitespace, returning plain text."""
    if not html:
        return ""
    # Try BeautifulSoup first for clean extraction
    soup = _make_soup(html)
    if soup is not None:
        try:
            text = soup.get_text(separator=" ", strip=True)
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            pass
    # Regex fallback
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


class KfssForestGoKrStatCrawler(BaseCrawler):
    """산림청 통계자료실 (ptlPdsBase) crawler."""

    site_id = "kfss-forest-go-kr-stat"
    site_name = "Custom: kfss-forest-go-kr-stat"
    base_url = "https://kfss.forest.go.kr"

    _LIST_URL = f"{_BASE}{_CTX}/ptl/article/selectArticleList.do"
    _FILE_URL = f"{_BASE}{_CTX}/ptl/article/selectArticleFileList.do"
    _DOWN_URL = f"{_BASE}{_CTX}/ptl/article/articleFileDown.do"
    _BBS_ID = "ptlPdsBase"
    _MENU_ID = "9795"
    _PAGE_SIZE = 10
    _MAX_PAGES = 200
    _WALL_SECONDS = 25 * 60

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl (TLS workaround) with 3-attempt exponential backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json, text/html, */*",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw:
                    return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    print(f"[kfss-forest-go-kr-stat] Empty response attempt {attempt+1}/3, "
                          f"retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[kfss-forest-go-kr-stat] curl error: {exc}, "
                          f"retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[kfss-forest-go-kr-stat] curl failed after 3 attempts: {exc}")
        return None

    def _curl_get_json(self, url):
        """GET JSON API, return parsed dict on code==1 or None."""
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            d = json.loads(raw)
            if d.get("code") == 1:
                return d
            print(f"[kfss-forest-go-kr-stat] API code != 1: {d.get('msg', '')} url={url}")
        except (ValueError, TypeError) as exc:
            print(f"[kfss-forest-go-kr-stat] JSON parse error: {exc} url={url}")
        return None

    # ------------------------------------------------------------------
    # Fetchers
    # ------------------------------------------------------------------

    def _fetch_list(self, page):
        """Return list of row dicts for one page, or [] on error."""
        qs = urlencode({
            "bbsId": self._BBS_ID,
            "curMenu": self._MENU_ID,
            "pageIndex": page,
            "pageSize": self._PAGE_SIZE,
        })
        d = self._curl_get_json(f"{self._LIST_URL}?{qs}")
        if not d:
            return []
        data = d.get("data", [])
        return data if isinstance(data, list) else []

    def _fetch_files(self, article_seq):
        """Return list of file dicts for an article, or [] on error."""
        qs = urlencode({"workPath": "Article", "workSeq": article_seq})
        for attempt in range(3):
            d = self._curl_get_json(f"{self._FILE_URL}?{qs}")
            if d is not None:
                data = d.get("data", [])
                return data if isinstance(data, list) else []
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[kfss-forest-go-kr-stat] File list {article_seq} attempt "
                      f"{attempt+1}/3, retry in {wait}s")
                time.sleep(wait)
        return []

    # ------------------------------------------------------------------
    # Abstract builder
    # ------------------------------------------------------------------

    def _build_abstract(self, row, files):
        """Build a comprehensive abstract from cont HTML + metadata + file names.

        Combines cont text, publication dates, source name, and attachment
        file names so the result is meaningful even when cont alone is sparse.
        """
        cont_html = row.get("cont", "") or ""
        cont_text = _strip_html(cont_html)

        reg_dtm = (row.get("regDtm") or "")[:10]
        updt_dtm = (row.get("updtDtm") or "")[:10]
        bbs_nm = row.get("bbsNm") or "통계자료실"

        parts = []
        if cont_text:
            parts.append(cont_text)

        meta_pieces = []
        if reg_dtm:
            meta_pieces.append(f"등록일: {reg_dtm}")
        if updt_dtm and updt_dtm != reg_dtm:
            meta_pieces.append(f"수정일: {updt_dtm}")
        meta_pieces.append(f"출처: {bbs_nm}")
        parts.append(" | ".join(meta_pieces))

        if files:
            names = [f.get("fileNm", "") for f in files if f.get("fileNm")]
            if names:
                parts.append("첨부파일: " + ", ".join(names))

        return "\n".join(p for p in parts if p).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 산림청 통계자료실 and persist documents.

        Parameters
        ----------
        limit:
            Maximum records to save.  None = unlimited.
        """
        saved = 0
        page = 1
        seen_seqs = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - start_time > self._WALL_SECONDS:
                print(f"[kfss-forest-go-kr-stat] 25-min wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[kfss-forest-go-kr-stat] Safety cap of {self._MAX_PAGES} pages reached.")
                break

            if page % 10 == 0:
                print(f"[kfss-forest-go-kr-stat] page {page}: saved {saved}/{limit_str}")

            rows = self._fetch_list(page)
            if not rows:
                print(f"[kfss-forest-go-kr-stat] page {page}: no items. Done.")
                break

            new_rows = [r for r in rows if str(r.get("articleSeq", "")) not in seen_seqs]
            if not new_rows:
                print(f"[kfss-forest-go-kr-stat] page {page}: all items already seen. Stopping.")
                break
            for r in rows:
                seen_seqs.add(str(r.get("articleSeq", "")))

            for row in new_rows:
                if limit is not None and saved >= limit:
                    break

                article_seq = str(row.get("articleSeq", ""))
                if not article_seq:
                    continue

                try:
                    time.sleep(self._delay)

                    files = self._fetch_files(article_seq)

                    abstract = self._build_abstract(row, files)

                    if len(abstract) < 50:
                        print(f"[kfss-forest-go-kr-stat] seq={article_seq}: "
                              f"abstract too short ({len(abstract)} chars), skipping")
                        continue

                    reg_dtm = (row.get("regDtm") or "")
                    updt_dtm = (row.get("updtDtm") or "")
                    pub_date = reg_dtm[:10] if reg_dtm else None
                    listed_date = updt_dtm[:10] if updt_dtm else pub_date

                    # Prefer PDF; fall back to first file
                    pdf_file = None
                    for f in files:
                        if (f.get("fileExt") or "").lower() == "pdf":
                            pdf_file = f
                            break
                    if pdf_file is None and files:
                        pdf_file = files[0]

                    pdf_url = None
                    original_filename = None
                    if pdf_file:
                        pdf_url = (
                            f"{self._DOWN_URL}"
                            f"?fileSeq={pdf_file['fileSeq']}&workSeq={article_seq}"
                        )
                        original_filename = pdf_file.get("fileNm")

                    detail_url = (
                        f"{self.base_url}{_CTX}/ptl/article/articleDtl.do"
                        f"?bbsId={self._BBS_ID}&curMenu={self._MENU_ID}"
                        f"&articleSeq={article_seq}"
                    )

                    paper = {
                        "site_id": self.site_id,
                        "external_id": article_seq,
                        "post_number": article_seq,
                        "title": (row.get("title") or "").strip(),
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "authors": None,
                        "publisher": "산림청 산림통계포털",
                        "department": row.get("bbsNm") or "통계자료실",
                        "journal": None,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": None,
                        "category": None,
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "articleSeq": article_seq,
                                "bbsId": row.get("bbsId"),
                                "bbsNm": row.get("bbsNm"),
                                "regNm": row.get("regNm"),
                                "regDtm": reg_dtm,
                                "updtNm": row.get("updtNm"),
                                "updtDtm": updt_dtm,
                                "posted_date": listed_date,
                                "originalFilename": original_filename,
                                "files": [
                                    {
                                        "fileSeq": f.get("fileSeq"),
                                        "fileNm": f.get("fileNm"),
                                        "fileExt": f.get("fileExt"),
                                        "fileSize": f.get("fileSize"),
                                    }
                                    for f in files
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[kfss-forest-go-kr-stat] Saved {saved}/{limit_str}: "
                          f"{paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kfss-forest-go-kr-stat] item {article_seq} failed: {exc}")
                    continue

            page += 1

        print(f"[kfss-forest-go-kr-stat] Done. Total saved: {saved}")
        return saved
