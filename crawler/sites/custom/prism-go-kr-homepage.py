# -*- coding: utf-8 -*-
"""PRISM 정책연구관리시스템 전체검색 crawler.

Target : https://www.prism.go.kr/homepage/prtl/totalsearch/list
API    : POST https://api.prism.go.kr/prism-be-prtl/search/totalSearch.do
         collection="report" returns all research reports with file info.
         query="연구" (extremely broad) → ~380 K total records.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402


class PrismGoKrHomepageCrawler(BaseCrawler):
    """Crawls PRISM 정책연구관리시스템 보고서 전체검색 목록."""

    site_id = "prism-go-kr-homepage"
    site_name = "Custom: prism-go-kr-homepage"
    base_url = "https://www.prism.go.kr"

    _SEARCH_API = "https://api.prism.go.kr/prism-be-prtl/search/totalSearch.do"
    _DETAIL_BASE = "https://www.prism.go.kr/homepage/asmt/popup"
    # "연구" (research) is present in virtually every record → ~380 K hits.
    _DEFAULT_QUERY = "연구"
    _PAGE_SIZE = 20
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))      # safety cap (log when reached)
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # curl POST helper
    # ------------------------------------------------------------------

    def _curl_post(self, payload: dict) -> str | None:
        """POST JSON via curl with 3 retries (exp backoff 3s, 9s).

        Returns raw response text, or None on failure.
        """
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {self.base_url}/homepage/prtl/totalsearch/list",
            "-H", f"Origin: {self.base_url}",
            "-d", json.dumps(payload, ensure_ascii=False),
            self._SEARCH_API,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=35,
                    errors="replace",
                )
                text = result.stdout.strip()
                if text:
                    return text
                print(
                    f"[{self.site_id}] Empty response (attempt {attempt + 1}/3)"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl error attempt {attempt + 1}/3: {exc}"
                )
            if attempt < 2:
                wait = 3 ** (attempt + 1)   # 3, 9
                print(f"[{self.site_id}] Retrying in {wait}s…")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(text: str | None) -> str:
        """Strip PRISM highlight markers, HTML tags/entities; collapse whitespace.

        Uses regex — appropriate for JSON field snippets (not full pages).
        For full HTML page parsing, prefer BeautifulSoup with html5lib.
        """
        if not text:
            return ""
        # PRISM search-result highlight markers
        text = re.sub(r"<!H[SE]>", "", text)
        # <br> / <BR /> → newline
        text = re.sub(r"<[Bb][Rr]\s*/?>", "\n", text)
        # Remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Common HTML entities
        text = text.replace("&nbsp;", " ")
        text = text.replace("&amp;", "&")
        text = text.replace("&lt;", "<")
        text = text.replace("&gt;", ">")
        text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
        return re.sub(r"[ \t]+", " ", text).strip()

    @staticmethod
    def _parse_date(raw: str | None) -> str | None:
        """Parse '2026.05.12' or '2026-05-12' → 'YYYY-MM-DD'.  Returns None if unparseable."""
        if not raw:
            return None
        m = re.match(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})", str(raw).strip())
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl PRISM 전체검색 보고서 목록 via JSON API.

        Parameters
        ----------
        limit:
            Maximum records to save.  None = unlimited.
        """
        saved = 0
        page = 0
        seen_ids: set[str] = set()
        start_time = time.time()

        while True:
            # ── Termination guards ──────────────────────────────────────
            if limit is not None and saved >= limit:
                break
            if page >= self._MAX_PAGES:
                print(
                    f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping."
                )
                break
            elapsed = time.time() - start_time
            if elapsed > self._MAX_SECONDS:
                print(
                    f"[{self.site_id}] 25-minute budget exceeded ({elapsed/60:.1f} min). "
                    "Stopping cleanly."
                )
                break

            # ── Fetch list page ─────────────────────────────────────────
            payload = {
                "query": self._DEFAULT_QUERY,
                "collection": "report",
                "startCount": page * self._PAGE_SIZE,
                "listCount": self._PAGE_SIZE,
                "jrsdInstGrntNo": "",
                "hghrkFwkClsfSysId": "",
                "asmtOtln": "",
                "kywdCn": "",
                "rptpDtlCn": "",
                "thssSmryCn": "",
                "startDate": "",
                "endDate": "",
            }

            raw = self._curl_post(payload)
            if raw is None:
                print(f"[{self.site_id}] page {page}: fetch failed. Stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] page {page}: JSON parse error: {exc}. Stopping.")
                break

            items = (data.get("report") or {}).get("searchResultList") or []
            if not items:
                print(f"[{self.site_id}] page {page}: no items. Done.")
                break

            # ── Progress log every 10 pages ─────────────────────────────
            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(
                    f"[{self.site_id}] page {page}: saved {saved}/{limit_str}"
                )

            # ── Process items ───────────────────────────────────────────
            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    # Unique doc ID: strip leading '#' and '0' prefix.
                    docid_raw = item.get("DOCID") or ""
                    docid = re.sub(r"^#+0*", "", docid_raw)
                    asmt_id = item.get("ASMT_ID") or ""
                    uid = docid or asmt_id
                    if not uid:
                        continue
                    if uid in seen_ids:
                        continue
                    seen_ids.add(uid)

                    title = self._strip_html(item.get("ASMT_NM") or "")
                    if not title:
                        continue

                    # Build abstract from richest available fields.
                    abs_parts = []
                    for fld in ("THSS_SMRY_CN", "ASMT_OTLN", "RPTP_DTL_CN", "FILE_CONTENT"):
                        val = self._strip_html(item.get(fld) or "")
                        if val and val not in abs_parts:
                            abs_parts.append(val)
                    abstract = "\n\n".join(abs_parts)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skip (abstract {len(abstract)} chars): "
                            f"{title[:50]}"
                        )
                        continue

                    # Dates
                    published_date = self._parse_date(
                        item.get("FRST_REG_DT") or item.get("DATE")
                    )
                    listed_date = self._parse_date(item.get("DATE"))

                    # Detail page URL (React popup route)
                    detail_url = (
                        f"{self._DETAIL_BASE}/{asmt_id}" if asmt_id else ""
                    )

                    # PDF storage path (direct URL; download requires POST API)
                    file_path = (item.get("FILE_PATH_NM") or "").rstrip("/")
                    strg_file = item.get("STRG_FILE_NM") or ""
                    pdf_url: str | None = (
                        f"{self.base_url}{file_path}/{strg_file}"
                        if file_path and strg_file
                        else None
                    )

                    # Original filename (strip highlight markers)
                    file_nm = self._strip_html(item.get("FILE_NM") or "")
                    original_filename = file_nm or strg_file or None

                    # Keywords: PRISM separates multiple terms with '||'
                    kw_raw = self._strip_html(item.get("KYWD_CN") or "")
                    keywords = re.sub(r"\|\|+", ", ", kw_raw).strip(", ") or None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": uid,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,   # adapter maps → posted_date col
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "publisher": item.get("INST_NM") or "",
                        "authors": "",
                        "journal": "",
                        "keywords": keywords,
                        "category": item.get("CLSF_SYS_NM") or "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": item.get("DATE"),
                                "originalFilename": item.get("FILE_NM"),
                                "asmtId": asmt_id,
                                "docid": docid_raw,
                                "fileTypeCd": item.get("FILE_TYPE_CD"),
                                "fileSn": item.get("FILE_SN"),
                                "fileWkky": item.get("FILE_WKKY"),
                                "pdfTrsfYn": item.get("PDF_TRSF_YN"),
                                "pdfTrsfTrgtYn": item.get("PDF_TRSF_TRGT_YN"),
                                "prgrsSttsNm": item.get("PRGRS_STTS_NM"),
                                "pblcnYr": item.get("PBLCN_YR"),
                                "rschBgngYmd": item.get("RSCH_BGNG_YMD"),
                                "rschEndYmd": item.get("RSCH_END_YMD"),
                                "jrsdInstGrntNo": item.get("JRSD_INST_GRNT_NO"),
                                "hghrkFwkClsfSysId": item.get("HGHRK_FWK_CLSF_SYS_ID"),
                                "clsfSysNm": item.get("CLSF_SYS_NM"),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1
                    limit_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            # Infinite-loop guard: if the API silently re-sends the same items.
            if items and new_on_page == 0:
                print(
                    f"[{self.site_id}] page {page}: all items already seen. Stopping."
                )
                break

            time.sleep(self._delay)
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
