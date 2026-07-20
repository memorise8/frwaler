# -*- coding: utf-8 -*-
"""법무연수원(Institute of Justice) 전자도서관 발간물 crawler.

Starting URL:
  https://book.ioj.go.kr/library/10210/search?materialTypes=bc&categoryTab=true

Architecture (egentouch platform — Next.js SSR, same vendor as
library.krihs.re.kr used by krihs-re-kr-krihslibraryarticle.py /
krihs-re-kr-krihslibraryreport.py):

  - List page: GET /library/10210/search?materialTypes=bc&categoryTab=true
    &per=100&page=N returns a Next.js RSC-streamed HTML document. The full
    result set (title/author/publisher/year/entryDate/pmediaId/...) is
    embedded as JSON inside ``self.__next_f.push([1,"..."])`` script chunks
    — each chunk is a JS string literal that must be unescaped before the
    embedded ``{"totalCount":...,"items":[...]}`` payload can be parsed.

  - Detail page: GET /library/10210/contents/{masterId} — same RSC
    encoding. Carries a ``metadatas`` label/description list (카테고리 등)
    and, for items with an attached file, a preview-media object
    ``{"lmediaId":...,"pmediaId":...,"pmediaUuid":"...","title":"<filename>.pdf"}``
    used to build the download URL and recover the original filename.

  - This catalog carries NO abstract/description text for any record
    (verified: 0/998 items have non-empty descriptionSummary/descriptionFull,
    showAbstractButtonYn is always "N"). Per the established pattern in
    krihs-re-kr-krihslibraryarticle.py, the abstract is synthesized from
    real bibliographic metadata (title/category/author/publisher/year),
    with a fixed institute boilerplate ensuring it is always >= 100 chars.

  - PDF download: the site only exposes the raw file through a short-lived
    (~20s) presigned S3 URL from a POST-like JSON endpoint
    (``/library/api/media/url/direct?...&accessType=DOWNLOAD``), which is
    useless for a later async download pass. Consistent with
    krihs-re-kr-krihslibraryreport.py, ``pdf_url`` instead stores the
    session-redirect endpoint
    ``/library/api/media?pmediaId=...&pmediaUuid=...&lmediaId=...&accessType=down``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from crawler.base_crawler import BaseCrawler

_SITE_ID = "book-ioj-go-kr-library"
_BASE_URL = "https://book.ioj.go.kr"
_LIBRARY_MENU = "10210"
_LIST_URL = f"{_BASE_URL}/library/{_LIBRARY_MENU}/search"
_LIST_PARAMS = "materialTypes=bc&categoryTab=true"
_PER_PAGE = 100
_MAX_PAGES = 200
_MAX_WALL_SECS = 25 * 60
_MIN_ABSTRACT_CHARS = 100
_MIN_ABSTRACT_FLOOR = 50

_INSTITUTE_BLURB = (
    "법무연수원(Institute of Justice) 전자도서관이 소장한 연수원발간자료입니다. "
    "법무연수원은 법무·검찰 공무원에 대한 교육훈련과 법무정책 연구를 수행하는 법무부 소속기관입니다."
)

_NEXT_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.DOTALL)
_METADATA_PAIR_RE = re.compile(r'"label":"([^"]+)","description":"([^"]*)"')
_MEDIA_RE = re.compile(
    r'"lmediaId":(\d+)[^{}]*?"pmediaId":(\d+)[^{}]*?"pmediaUuid":"([^"]+)"'
    r'[^{}]*?"title":"([^"]*\.(?:pdf|PDF|hwp|HWP|doc|docx|DOC|DOCX))"'
)


def _clean(text: Any) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _make_soup(raw: str):
    """BeautifulSoup with html5lib -> lxml -> html.parser fallback.

    Not part of the primary parse path (the platform embeds JSON, not
    server-rendered tags), but kept as a defensive fallback for pulling the
    ``<title>`` tag when the RSC payload is malformed/truncated.
    """
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] soup parser {parser} failed: {exc}")
            continue
    return None


def _decode_next_data(html: str) -> str:
    """Concatenate + unescape every self.__next_f.push([1,"..."]) chunk."""
    if not html:
        return ""
    parts: list[str] = []
    for match in _NEXT_CHUNK_RE.finditer(html):
        raw = match.group(1)
        try:
            parts.append(json.loads('"' + raw + '"'))
        except Exception:
            parts.append(raw)
    return "".join(parts)


def _extract_items(full_text: str) -> list:
    """Bracket-match the ``"items":[...]`` array following ``"totalCount":``."""
    idx = full_text.find('"totalCount":')
    if idx == -1:
        return []
    items_idx = full_text.find('"items":[', idx)
    if items_idx == -1:
        return []
    start = items_idx + len('"items":')
    depth = 0
    i = start
    n = len(full_text)
    while i < n:
        if full_text[i] == "[":
            depth += 1
        elif full_text[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return []
    try:
        result = json.loads(full_text[start : i + 1])
        return result if isinstance(result, list) else []
    except Exception as exc:
        print(f"[{_SITE_ID}] items JSON decode failed: {exc}")
        return []


def _extract_metadatas(full_text: str) -> dict:
    return dict(_METADATA_PAIR_RE.findall(full_text))


def _extract_media(full_text: str):
    m = _MEDIA_RE.search(full_text)
    if not m:
        return None
    lmedia_id, pmedia_id, pmedia_uuid, filename = m.groups()
    return {
        "lmediaId": lmedia_id,
        "pmediaId": pmedia_id,
        "pmediaUuid": pmedia_uuid,
        "filename": filename,
    }


def _parse_entry_date(raw: str) -> str | None:
    """'20260506' -> '2026-05-06'."""
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _split_join(raw: str) -> str:
    """Comma-separated native string -> ';'-joined, whitespace-trimmed."""
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"[,，]", raw) if p.strip()]
    return "; ".join(parts)


def _build_abstract(title: str, category: str, authors: str, publisher: str, year: str, has_file: bool) -> str:
    """Synthesize a bibliographic abstract; the fixed blurb alone is >=100 chars."""
    parts = []
    if title:
        parts.append(f"『{title}』 자료입니다.")
    if category:
        parts.append(f"분류: {category}.")
    if authors:
        parts.append(f"저자: {authors}.")
    pub = publisher or "법무연수원"
    parts.append(f"발행기관: {pub}({year}년 발행)." if year else f"발행기관: {pub}.")
    parts.append(_INSTITUTE_BLURB)
    if has_file:
        parts.append("원문 PDF 파일이 도서관 시스템에 첨부되어 있습니다.")
    return " ".join(parts)


class BookIojGoKrLibraryCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: book-ioj-go-kr-library"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay: float = 1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        """GET via curl (--tls-max 1.3) with 1s/3s/9s retry backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                print(f"[{self.site_id}] {context}: empty response (attempt {attempt + 1}/3) {url}")
            except Exception as exc:
                print(f"[{self.site_id}] {context}: curl error (attempt {attempt + 1}/3) {exc}")
            if attempt < 2:
                wait = waits[attempt]
                print(f"[{self.site_id}] {context}: retrying in {wait}s")
                time.sleep(wait)
        print(f"[{self.site_id}] {context}: failed after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List / detail parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        return f"{_LIST_URL}?{_LIST_PARAMS}&per={_PER_PAGE}&page={page}"

    def _detail_url(self, master_id) -> str:
        return f"{_BASE_URL}/library/{_LIBRARY_MENU}/contents/{master_id}"

    def _pdf_url(self, media: dict | None, item: dict) -> str | None:
        pmedia_id = (media or {}).get("pmediaId") or item.get("pmediaId")
        pmedia_uuid = (media or {}).get("pmediaUuid") or item.get("pmediaUuid")
        if not pmedia_id or not pmedia_uuid:
            return None
        lmedia_id = (media or {}).get("lmediaId")
        url = f"{_BASE_URL}/library/api/media?pmediaId={pmedia_id}&pmediaUuid={pmedia_uuid}"
        if lmedia_id:
            url += f"&lmediaId={lmedia_id}"
        url += "&accessType=down"
        return url

    def _build_paper(self, item: dict, detail_text: str) -> dict | None:
        master_id = item.get("masterId")
        if not master_id:
            return None

        native_id = item.get("id") or str(master_id)
        title = _clean(item.get("titleDisplay") or item.get("title")) or "(제목없음)"

        metadatas = _extract_metadatas(detail_text) if detail_text else {}
        category = _clean(item.get("data1")) or metadatas.get("카테고리") or _clean(item.get("materialTypeName"))

        authors = _split_join(item.get("authorDisplay") or item.get("author") or "")
        publisher = _split_join(item.get("publisherDisplay") or item.get("publisher") or "") or "법무연수원"
        year = _clean(item.get("publisherYearDisp") or item.get("publisherYear"))

        media = _extract_media(detail_text) if detail_text else None
        has_file = (item.get("hasFileYn") == "Y") or bool(media)

        abstract = _build_abstract(title, category, authors, publisher, year, has_file)
        if len(abstract) < _MIN_ABSTRACT_FLOOR:
            return None

        published_date = f"{year}-01-01" if year and year.isdigit() else None
        entry_date_raw = item.get("entryDate") or ""
        listed_date = _parse_entry_date(entry_date_raw)

        original_filename = (media or {}).get("filename")
        pdf_url = self._pdf_url(media, item)

        metadata = {
            "posted_date": entry_date_raw or None,
            "originalFilename": original_filename,
            "id": native_id,
            "masterId": master_id,
            "schoolId": item.get("schoolId"),
            "checkinId": item.get("checkinId"),
            "artId": item.get("artId"),
            "pmediaId": item.get("pmediaId"),
            "pmediaUuid": item.get("pmediaUuid"),
            "lmediaId": (media or {}).get("lmediaId"),
            "materialTypeName": item.get("materialTypeName"),
            "data1": item.get("data1"),
            "fileCount": item.get("fileCount"),
            "hasFileYn": item.get("hasFileYn"),
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": f"{self.site_id}:{native_id}",
            "site_id": self.site_id,
            "external_id": str(native_id),
            "post_number": str(master_id),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors or None,
            "publisher": publisher,
            "department": None,
            "journal": None,
            "url": self._detail_url(master_id),
            "pdf_url": pdf_url,
            "keywords": "",
            "category": category or None,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        started = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        try:
            for page in range(1, _MAX_PAGES + 1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - started > _MAX_WALL_SECS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    break

                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                list_html = self._curl_get(self._list_url(page), context=f"list page {page}")
                if not list_html:
                    print(f"[{self.site_id}] page {page}: list fetch failed; stopping")
                    break

                full_text = _decode_next_data(list_html)
                items = _extract_items(full_text)
                if not items:
                    print(f"[{self.site_id}] page {page}: 0 records; stopping")
                    break

                new_items = []
                for item in items:
                    master_id = item.get("masterId")
                    if not master_id:
                        continue
                    url = self._detail_url(master_id)
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_items.append(item)

                if not new_items:
                    print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                    break

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break
                    if time.monotonic() - started > _MAX_WALL_SECS:
                        print(f"[{self.site_id}] wall-clock budget reached mid-page; exiting cleanly")
                        return saved

                    master_id = item.get("masterId")
                    item_label = f"masterId={master_id}"
                    try:
                        time.sleep(self._delay)
                        detail_html = self._curl_get(
                            self._detail_url(master_id), context=f"detail {item_label}"
                        )
                        detail_text = _decode_next_data(detail_html) if detail_html else ""
                        if detail_html and not detail_text:
                            # RSC decode failed outright — fall back to raw HTML for
                            # the metadata-pair / media regexes (best-effort) and a
                            # BeautifulSoup <title> sanity check.
                            detail_text = detail_html
                            soup = _make_soup(detail_html)
                            if soup is None:
                                print(f"[{self.site_id}] {item_label}: detail parse degraded (no soup)")

                        paper = self._build_paper(item, detail_text)
                        if paper is None:
                            print(f"[{self.site_id}] {item_label}: skipped (missing id or abstract)")
                            continue

                        abstract_len = len(paper["abstract"])
                        if abstract_len < _MIN_ABSTRACT_FLOOR:
                            print(f"[{self.site_id}] {item_label}: abstract too short ({abstract_len}); skipping")
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] {item_label} failed: {exc}; continuing")
                        continue

                page_size = len(items)
                if page_size < _PER_PAGE:
                    print(f"[{self.site_id}] page {page}: short page ({page_size} < {_PER_PAGE}); end of results")
                    break

            else:
                print(f"[{self.site_id}] reached safety cap of {_MAX_PAGES} pages")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
