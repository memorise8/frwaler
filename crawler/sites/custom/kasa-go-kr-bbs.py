# -*- coding: utf-8 -*-
"""Korea AeroSpace Administration BBS crawler.

Target: https://www.kasa.go.kr/bbs/BBSMSTR_000000000010/list.do

The site is an eGovFrame-style board:
  - list:   /bbs/BBSMSTR_000000000010/list.do?pageIndex=N
  - detail: /bbs/BBSMSTR_000000000010/view.do?nttId=<native id>
  - file:   /cmm/fms/FileDown.do?atchFileId=<file id>&fileSn=<n>

curl is used because the site rejects bare requests and Korean public sites
often have TLS/encoding quirks.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from typing import Any
from urllib.parse import unquote, urljoin

from crawler.base_crawler import BaseCrawler


_SITE_ID = "kasa-go-kr-bbs"
_BASE_URL = "https://www.kasa.go.kr"
_BBS_ID = "BBSMSTR_000000000010"
_LIST_URL = f"{_BASE_URL}/bbs/{_BBS_ID}/list.do"
_DETAIL_URL = f"{_BASE_URL}/bbs/{_BBS_ID}/view.do"
_FILE_URL = f"{_BASE_URL}/cmm/fms/FileDown.do"

_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_CLOCK_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_STOP_SOON_SECS = _WALL_CLOCK_SECS - 30
_MIN_ABSTRACT_CHARS = 50
_RICH_ABSTRACT_CHARS = 100

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, *, referer: str | None = None, timeout: int = 45) -> bytes | None:
    """Fetch a URL via curl with TLS cap and 1s/3s/9s retries."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-sk",
        "-L",
        "--http1.1",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H",
        "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
    ]
    if referer:
        cmd.extend(["-H", f"Referer: {referer}"])
    cmd.append(url)

    for attempt, wait in enumerate((1, 3, 9), start=1):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            stderr = _decode(result.stderr).strip()
            print(
                f"[{_SITE_ID}] curl failed attempt {attempt}/3 for {url}: "
                f"returncode={result.returncode} {stderr[:160]}"
            )
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt}/3 for {url}: {exc}")

        if attempt < 3:
            time.sleep(wait)

    return None


def _curl_head_filename(url: str, *, referer: str | None = None, timeout: int = 20) -> str | None:
    """Best-effort filename extraction from Content-Disposition."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skI",
        "--http1.1",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_USER_AGENT}",
    ]
    if referer:
        cmd.extend(["-H", f"Referer: {referer}"])
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except Exception:
        return None

    if result.returncode != 0 or not result.stdout:
        return None

    headers = _decode(result.stdout)
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^"\r\n;]+)"?', headers, re.I)
    if not match:
        return None
    filename = unquote(match.group(1).strip())
    return filename or None


def _decode(raw: bytes | str | None) -> str:
    """Decode possibly mixed Korean/UTF-8 bytes without raising."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _make_soup(raw: bytes | str):
    """BeautifulSoup parser fallback: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup

    text = _decode(raw)
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup parser {parser} failed: {exc}")
    return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", value)
    if match:
        return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return match.group(0)
    return value.strip() or None


def _meta_content(soup, key: str) -> str | None:
    tag = soup.find("meta", attrs={"name": key}) or soup.find("meta", attrs={"property": key})
    if not tag:
        return None
    value = tag.get("content")
    return _clean_text(value)


def _detail_url(ntt_id: str) -> str:
    return f"{_DETAIL_URL}?nttId={ntt_id}"


def _download_url(atch_file_id: str, file_sn: str) -> str:
    return f"{_FILE_URL}?atchFileId={atch_file_id}&fileSn={file_sn}"


def _parse_list_page(raw: bytes | str, page: int) -> tuple[list[dict[str, Any]], bool]:
    soup = _make_soup(raw)
    if soup is None:
        return [], False

    rows = soup.select(".program__board-section .program__board-row")
    items: list[dict[str, Any]] = []

    for row in rows:
        button = row.select_one("button.board__link[onclick*='fn_search_detail']")
        if button is None:
            continue

        onclick = button.get("onclick", "")
        id_match = re.search(r"fn_search_detail\(['\"]([^'\"]+)['\"]\)", onclick)
        if not id_match:
            continue
        ntt_id = id_match.group(1).strip()
        if not ntt_id:
            continue

        title_tag = row.select_one(".board__subject-text")
        title = _clean_text(title_tag.get_text(" ", strip=True) if title_tag else button.get_text(" ", strip=True))

        count_tag = row.select_one(".program__board-cell.count .td")
        post_number = _clean_text(count_tag.get_text(" ", strip=True) if count_tag else "")
        if not re.fullmatch(r"\d+", post_number or ""):
            post_number = ntt_id

        date_tag = row.select_one(".program__board-cell.regDate .td")
        listed_date_raw = _clean_text(date_tag.get_text(" ", strip=True) if date_tag else "")

        file_id = None
        file_match = re.search(r"fn_zipDownload\(['\"]([^'\"]+)['\"]\)", str(row))
        if file_match:
            file_id = file_match.group(1)

        items.append(
            {
                "nttId": ntt_id,
                "post_number": post_number,
                "title": title,
                "listed_date_raw": listed_date_raw,
                "listed_date": _normalize_date(listed_date_raw),
                "url": _detail_url(ntt_id),
                "list_file_id": file_id,
            }
        )

    has_next = False
    for anchor in soup.select(".pagination a[href], .pagination a[onclick]"):
        candidate = " ".join(filter(None, [anchor.get("href"), anchor.get("onclick")]))
        for page_no in re.findall(r"(?:pageIndex=|fn_egov_select_linkPage\()(\d+)", candidate):
            try:
                if int(page_no) > page:
                    has_next = True
                    break
            except ValueError:
                continue
        if has_next:
            break

    return items, has_next


def _extract_attachments(soup, detail_html: str, detail_url: str) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    attachments: list[dict[str, str]] = []

    for item in soup.select(".board-view__file .board-file__item"):
        link = item.select_one("a.board-file__link[href*='fn_egov_downFile']")
        if link is None:
            continue

        href = link.get("href", "")
        match = re.search(r"fn_egov_downFile\(['\"]([^'\"]+)['\"]\s*,\s*['\"]?(\d+)['\"]?\)", href)
        if not match:
            continue

        filename_tag = item.select_one(".board-file__text")
        filename = _clean_text(filename_tag.get_text(" ", strip=True) if filename_tag else "")
        atch_file_id, file_sn = match.group(1), match.group(2)
        download_url = _download_url(atch_file_id, file_sn)

        if not filename:
            filename = _curl_head_filename(download_url, referer=detail_url) or ""

        ext = ""
        ext_match = re.search(r"\.([A-Za-z0-9]{1,8})$", filename)
        if ext_match:
            ext = ext_match.group(1).lower()

        attachments.append(
            {
                "atchFileId": atch_file_id,
                "fileSn": file_sn,
                "filename": filename,
                "url": download_url,
                "ext": ext,
            }
        )

    if not attachments:
        file_calls = re.findall(
            r"fn_egov_downFile\(['\"]([^'\"]+)['\"]\s*,\s*['\"]?(\d+)['\"]?\)",
            detail_html,
        )
        filenames = [
            _clean_text(tag.get_text(" ", strip=True))
            for tag in soup.select(".board-file__text")
            if _clean_text(tag.get_text(" ", strip=True))
        ]
        seen = set()
        for idx, (atch_file_id, file_sn) in enumerate(file_calls):
            key = (atch_file_id, file_sn)
            if key in seen:
                continue
            seen.add(key)
            filename = filenames[idx] if idx < len(filenames) else ""
            url = _download_url(atch_file_id, file_sn)
            if not filename:
                filename = _curl_head_filename(url, referer=detail_url) or ""
            ext_match = re.search(r"\.([A-Za-z0-9]{1,8})$", filename)
            attachments.append(
                {
                    "atchFileId": atch_file_id,
                    "fileSn": file_sn,
                    "filename": filename,
                    "url": url,
                    "ext": ext_match.group(1).lower() if ext_match else "",
                }
            )

    chosen = next((att for att in attachments if att.get("ext") == "pdf"), None)
    if chosen is None and attachments:
        chosen = attachments[0]

    return attachments, chosen


def _parse_detail_page(raw: bytes | str, item: dict[str, Any]) -> dict[str, Any] | None:
    soup = _make_soup(raw)
    if soup is None:
        return None

    for bad in soup.select("script, style, noscript"):
        bad.decompose()

    detail_url = item["url"]
    detail_html = _decode(raw)

    title_tag = soup.select_one(".board-view__title")
    title = _clean_text(title_tag.get_text(" ", strip=True) if title_tag else "")
    if not title:
        title = _meta_content(soup, "title") or item.get("title") or ""

    date_raw = ""
    date_tag = soup.select_one(".board-view__info .info__date")
    if date_tag:
        date_raw = _clean_text(date_tag.get_text(" ", strip=True).replace("등록일", ""))
    if not date_raw:
        date_raw = item.get("listed_date_raw") or ""
    published_date = _normalize_date(date_raw)

    content_tag = soup.select_one(".board-view__contents-inner")
    abstract = ""
    if content_tag:
        abstract = _clean_text(content_tag.get_text("\n", strip=True))
    if not abstract:
        abstract = _meta_content(soup, "description") or ""

    publisher = _meta_content(soup, "author") or "우주항공청"
    department = ""
    author_text = ""

    attachments, chosen_attachment = _extract_attachments(soup, detail_html, detail_url)
    all_filenames = [att["filename"] for att in attachments if att.get("filename")]

    if len(abstract) < _RICH_ABSTRACT_CHARS:
        additions = []
        if title:
            additions.append(f"제목: {title}")
        if published_date:
            additions.append(f"등록일: {published_date}")
        if publisher:
            additions.append(f"발행기관: {publisher}")
        if all_filenames:
            additions.append("첨부파일: " + "; ".join(all_filenames))
        if additions:
            abstract = _clean_text("\n".join(additions + ([abstract] if abstract else [])))

    selected_filename = chosen_attachment.get("filename") if chosen_attachment else None
    selected_url = chosen_attachment.get("url") if chosen_attachment else None

    return {
        "title": title,
        "abstract": abstract,
        "published_date_raw": date_raw,
        "published_date": published_date,
        "publisher": publisher,
        "department": department,
        "authors": author_text,
        "attachments": attachments,
        "selected_attachment": chosen_attachment,
        "pdf_url": selected_url,
        "original_filename": selected_filename,
    }


class KasaGoKrBbsCrawler(BaseCrawler):
    site_id = "kasa-go-kr-bbs"
    site_name = "Custom: kasa-go-kr-bbs"
    base_url = "https://www.kasa.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        if limit is not None and limit <= 0:
            return 0

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed >= _STOP_SOON_SECS:
                print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; stopping cleanly.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; processing final capped page.")

            list_url = f"{_LIST_URL}?pageIndex={page}"
            list_raw = _curl_get(list_url, referer=_LIST_URL)
            if not list_raw:
                print(f"[{_SITE_ID}] list page {page} failed after retries; stopping.")
                break

            items, has_next = _parse_list_page(list_raw, page)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no records; done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                if time.time() - start_time >= _STOP_SOON_SECS:
                    print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget inside page {page}; stopping cleanly.")
                    return saved

                detail_url = item["url"]
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                ntt_id = item.get("nttId") or detail_url
                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(detail_url, referer=list_url)
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {ntt_id} failed after 3 network attempts; skipping.")
                        continue

                    detail = _parse_detail_page(detail_raw, item)
                    if detail is None:
                        print(f"[{_SITE_ID}] item {ntt_id} parse failed; skipping.")
                        continue

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(f"[{_SITE_ID}] item {ntt_id} abstract too short ({len(abstract)} chars); skipping.")
                        continue

                    title = detail.get("title") or item.get("title") or ntt_id
                    listed_date = item.get("listed_date") or detail.get("published_date")
                    listed_date_raw = item.get("listed_date_raw") or detail.get("published_date_raw")
                    published_date = detail.get("published_date") or listed_date

                    attachments = detail.get("attachments") or []
                    selected_attachment = detail.get("selected_attachment") or {}
                    metadata = {
                        "posted_date": listed_date_raw,
                        "originalFilename": detail.get("original_filename"),
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "bbsId": _BBS_ID,
                        "nttId": ntt_id,
                        "node_id": _BBS_ID,
                        "post_number": item.get("post_number"),
                        "listed_date": listed_date,
                        "listed_date_raw": listed_date_raw,
                        "published_date_raw": detail.get("published_date_raw"),
                        "list_file_id": item.get("list_file_id"),
                        "attachments": attachments,
                        "selected_attachment": selected_attachment,
                        "pdf_url_is_pdf": (selected_attachment.get("ext") == "pdf") if selected_attachment else False,
                    }

                    self._save_paper(
                        {
                            "id": f"{self.site_id}:{ntt_id}",
                            "site_id": self.site_id,
                            "external_id": ntt_id,
                            "post_number": item.get("post_number"),
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "posted_date": listed_date,
                            "authors": detail.get("authors") or None,
                            "publisher": detail.get("publisher") or "우주항공청",
                            "department": detail.get("department") or None,
                            "journal": None,
                            "url": detail_url,
                            "pdf_url": detail.get("pdf_url"),
                            "keywords": "",
                            "category": "보도자료",
                            "doi": None,
                            "original_filename": detail.get("original_filename"),
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }
                    )
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {ntt_id} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: all URLs already seen; done.")
                break

            if not has_next:
                print(f"[{_SITE_ID}] page {page}: next page link absent; done.")
                break

        print(f"[{_SITE_ID}] Done. Saved {saved} items.")
        return saved
