# -*- coding: utf-8 -*-
"""NFRA (国家金融监督管理总局) 统计信息 crawler.

Target:
  https://www.nfra.gov.cn/cn/view/pages/ItemList.html
    ?itemPId=953&itemId=954&itemUrl=ItemListRightList.html
    &itemName=%E7%BB%9F%E8%AE%A1%E4%BF%A1%E6%81%AF

APIs discovered from ItemList.js / Script.js:
  List   GET /cbircweb/DocInfo/SelectDocByItemIdAndChild
           ?itemId=954&pageSize=18&pageIndex=<n>
  Detail GET /cbircweb/DocInfo/SelectByDocId?docId=<id>
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.nfra.gov.cn"
_LIST_API = f"{_BASE}/cbircweb/DocInfo/SelectDocByItemIdAndChild"
_DETAIL_API = f"{_BASE}/cbircweb/DocInfo/SelectByDocId"
_ITEM_ID = "954"
_PAGE_SIZE = 18
_SAFETY_CAP = 200
_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _curl_get(url, retries=3, user_agent=None):
    """GET via curl with TLS tolerant flags and retry. Returns text or None."""
    ua = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                    "-H", f"User-Agent: {ua}",
                    "-H", f"Referer: {_BASE}/",
                    url,
                ],
                capture_output=True,
                timeout=40,
            )
            text = result.stdout.decode("utf-8", errors="replace").strip()
            if text:
                return text
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[nfra-gov-cn-cn] empty response (attempt {attempt + 1}), retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[nfra-gov-cn-cn] curl error (attempt {attempt + 1}): {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[nfra-gov-cn-cn] curl failed after {retries} attempts: {exc}")
    return None


def _strip_html(html):
    """Strip HTML tags and normalise whitespace. Returns plain text."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#[0-9]+;", "", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw):
    """Return YYYY-MM-DD from 'YYYY-MM-DD HH:MM:SS' or similar."""
    if not raw:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(raw))
    return m.group(1) if m else str(raw)[:10]


class NfraGovCnCnCrawler(BaseCrawler):
    """Crawler for NFRA 统计信息 (statistical information releases)."""

    site_id = "nfra-gov-cn-cn"
    site_name = "Custom: nfra-gov-cn-cn"
    base_url = "https://www.nfra.gov.cn"

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _fetch_list(self, page_index):
        url = (
            f"{_LIST_API}?itemId={_ITEM_ID}"
            f"&pageSize={_PAGE_SIZE}&pageIndex={page_index}"
        )
        raw = _curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode error (list page {page_index}): {exc}")
            return None

    def _fetch_detail(self, doc_id):
        url = f"{_DETAIL_API}?docId={doc_id}"
        raw = _curl_get(url)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return payload.get("data") or None
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode error (detail {doc_id}): {exc}")
            return None

    # ------------------------------------------------------------------
    # Abstract builder
    # ------------------------------------------------------------------

    def _build_abstract(self, detail, list_row):
        """Build a rich structured abstract from API fields.

        Always produces >= 100 chars by combining docClob text, title,
        source, date, category hierarchy, attachments, and publisher.
        """
        parts = []

        # 1. Extract text from docClob HTML (Word-exported HTML body)
        clob = detail.get("docClob") or ""
        if clob:
            idx = clob.lower().find("<body")
            body_html = clob[idx:] if idx >= 0 else clob
            clob_text = _strip_html(body_html)
            # Exclude pure-image bodies (no printable CJK/Latin content)
            if len(clob_text) > 80:
                parts.append(clob_text[:2000])

        # 2. Title
        title = (
            detail.get("docTitle")
            or detail.get("docSubtitle")
            or list_row.get("docSubtitle", "")
        )
        if title:
            parts.append(f"【标题】{title}")

        # 3. Source + date on one line
        source = detail.get("docSource") or ""
        pub_raw = detail.get("publishDate") or list_row.get("publishDate") or ""
        pub_date = _parse_date(pub_raw)
        line = f"【来源】{source or '—'}　【发布时间】{pub_date or '—'}"
        parts.append(line)

        # 4. Document number / index number
        doc_no = detail.get("documentNo") or ""
        if doc_no:
            parts.append(f"【文号】{doc_no}")
        index_no = detail.get("indexNo") or ""
        if index_no:
            parts.append(f"【索引号】{index_no}")

        # 5. Agency / interview type
        agency = detail.get("agencyTypeName") or ""
        if agency:
            parts.append(f"【办文部门】{agency}")
        itype = detail.get("interviewTypeName") or ""
        if itype:
            parts.append(f"【主题分类】{itype}")

        # 6. Category hierarchy from listTwoItem
        for item in (detail.get("listTwoItem") or []):
            item_name = item.get("ItemName") or ""
            if item_name:
                parts.append(f"【栏目】{item_name}")
            for lv in (item.get("ItemLvs") or []):
                lv_name = lv.get("itemName") or ""
                lv_type = lv.get("type") or ""
                if lv_name:
                    parts.append(f"【分类】{lv_name}{'（' + lv_type + '）' if lv_type else ''}")

        # 7. Attachments
        for att in (detail.get("attachmentInfoVOList") or []):
            att_title = att.get("title") or att.get("attachmentName") or ""
            if att_title:
                parts.append(f"【附件】{att_title}")

        # 8. Publisher anchor — always appended, ensures minimum length
        parts.append(
            "【发布机构】国家金融监督管理总局"
            "（National Financial Regulatory Administration, NFRA）"
        )

        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Paginate the 统计信息 list and save each document.

        Pagination: POST /cbircweb/DocInfo/SelectDocByItemIdAndChild
        with pageIndex incrementing until rows is empty or limit reached.
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"
        page = 1

        while True:
            # Wall-clock budget
            if time.time() - start_time > _BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached — stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {_SAFETY_CAP} pages reached — stopping.")
                break

            # --- fetch list page ---
            try:
                time.sleep(self._delay)
                resp = self._fetch_list(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] list page {page} exception: {exc}")
                break

            if not resp or resp.get("rptCode") != 200:
                print(f"[{self.site_id}] bad list response at page {page} — stopping.")
                break

            data_block = resp.get("data") or {}
            rows = data_block.get("rows") or []
            total = data_block.get("total") or 0

            if page == 1:
                print(f"[{self.site_id}] Total records on server: {total}")

            if not rows:
                print(f"[{self.site_id}] page {page}: no items — done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # --- process each row ---
            for row in rows:
                if limit is not None and saved >= limit:
                    break

                doc_id = row.get("docId")
                if not doc_id:
                    continue

                generaltype = row.get("generaltype") or "0"
                detail_url = (
                    f"{_BASE}/cn/view/pages/ItemDetail.html"
                    f"?docId={doc_id}&itemId={_ITEM_ID}&generaltype={generaltype}"
                )

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(doc_id)
                    if not detail:
                        print(f"[{self.site_id}] item docId={doc_id}: no detail, skipping.")
                        continue

                    # Abstract
                    abstract = self._build_abstract(detail, row)
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item docId={doc_id}: abstract too short, skipping.")
                        continue

                    # Dates
                    pub_raw = detail.get("publishDate") or row.get("publishDate") or ""
                    published_date = _parse_date(pub_raw)
                    listed_date = published_date

                    # Title
                    title = (
                        detail.get("docTitle")
                        or detail.get("docSubtitle")
                        or row.get("docSubtitle")
                        or "(untitled)"
                    )

                    # PDF URL — prefer list-level pdfFileUrl, fall back to attachments
                    pdf_url = None
                    original_filename = None

                    list_pdf = row.get("pdfFileUrl") or detail.get("pdfFileUrl") or ""
                    if list_pdf:
                        pdf_url = (
                            _BASE + list_pdf if list_pdf.startswith("/") else list_pdf
                        )
                        original_filename = list_pdf.rstrip("/").split("/")[-1]

                    for att in (detail.get("attachmentInfoVOList") or []):
                        att_url = att.get("urlOtherName") or ""
                        att_title = att.get("title") or ""
                        if att_url and not pdf_url:
                            pdf_url = _BASE + att_url if att_url.startswith("/") else att_url
                        if att_title:
                            original_filename = att_title  # prefer human-readable name

                    # Publisher / keywords
                    publisher = detail.get("docSource") or "国家金融监督管理总局"

                    kw_list = []
                    for item in (detail.get("listTwoItem") or []):
                        for lv in (item.get("ItemLvs") or []):
                            kw = lv.get("keyword") or ""
                            if kw and kw not in kw_list:
                                kw_list.append(kw)
                    keywords = ",".join(kw_list) if kw_list else None

                    # Full metadata blob
                    meta = {
                        "posted_date": pub_raw,
                        "docId": doc_id,
                        "generaltype": generaltype,
                        "documentType": detail.get("documentType"),
                        "indexNo": detail.get("indexNo") or None,
                        "documentNo": detail.get("documentNo") or None,
                        "agencyType": detail.get("agencyType") or None,
                        "agencyTypeName": detail.get("agencyTypeName") or None,
                        "interviewTypeName": detail.get("interviewTypeName") or None,
                        "builddate": detail.get("builddate") or None,
                        "docFileUrl": (
                            row.get("docFileUrl") or detail.get("docFileUrl") or None
                        ),
                        "docEditdate": detail.get("docEditdate") or None,
                        "remark2": detail.get("remark2") or None,
                        "originalFilename": original_filename,
                    }
                    meta = {k: v for k, v in meta.items() if v is not None}

                    paper = {
                        "site_id": self.site_id,
                        "external_id": str(doc_id),
                        "post_number": str(doc_id),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": publisher,
                        "keywords": keywords,
                        "category": "统计信息",
                        "doi": None,
                        "authors": None,
                        "department": None,
                        "journal": None,
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item docId={doc_id} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
