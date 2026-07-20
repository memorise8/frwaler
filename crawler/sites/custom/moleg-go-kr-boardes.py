# -*- coding: utf-8 -*-
"""법제처 간행물 board.es crawler.

Target: https://www.moleg.go.kr/board.es?mid=a10404000000&bid=0007
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler


_SITE_ID = "moleg-go-kr-boardes"
_BASE_URL = "https://www.moleg.go.kr"
_MID = "a10404000000"
_BID = "0007"
_LIST_URL = f"{_BASE_URL}/board.es?mid={_MID}&bid={_BID}"
_PUBLISHER = "법제처"
_CATEGORY = "간행물"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 100


def _make_soup(raw: str):
    """Build BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup import failed: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _normalize_text(value: str) -> str:
    text = unescape(value or "")
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _node_text(node) -> str:
    if node is None:
        return ""
    try:
        return _normalize_text(node.get_text(" ", strip=True))
    except Exception:
        return _normalize_text(str(node))


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", raw)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _clean_filename(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _normalize_text(raw)
    text = re.sub(r"\s*(바로보기|내려받기)\s*(\(새창\))?$", "", text).strip()
    return text if text else None


def _curl_get(url: str, user_agent: str, *, max_time: int = 30, retries: int = 3) -> str | None:
    """Fetch text with curl, TLS capped at 1.3, and exponential backoff."""
    waits = [1, 3, 9]
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--connect-timeout",
        "15",
        "--max-time",
        str(max_time),
        "-H",
        f"User-Agent: {user_agent}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H",
        "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        url,
    ]

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
            raw = result.stdout or b""
            text = raw.decode("utf-8", errors="replace")
            if text.strip():
                return text
            detail = f"empty response, curl exit {result.returncode}"
        except Exception as exc:
            detail = str(exc)

        if attempt < retries - 1:
            wait = waits[attempt] if attempt < len(waits) else waits[-1]
            print(f"[{_SITE_ID}] fetch failed for {url} (attempt {attempt + 1}/{retries}): {detail}; retrying in {wait}s")
            time.sleep(wait)
        else:
            print(f"[{_SITE_ID}] fetch failed after {retries} attempts for {url}: {detail}")
    return None


def _parse_list_page(raw: str, page: int) -> list[dict]:
    soup = _make_soup(raw)
    if soup is None:
        return []

    rows = soup.select("tbody#listView tr")
    if not rows:
        table = soup.find("table", class_=re.compile(r"tstyle|board", re.I))
        rows = table.find_all("tr") if table else []

    items: list[dict] = []
    for row in rows:
        try:
            anchor = row.find("a", href=re.compile(r"list_no=\d+"))
            if not anchor:
                continue

            href = unescape(anchor.get("href", ""))
            match = re.search(r"list_no=(\d+)", href)
            if not match:
                continue

            list_no = match.group(1)
            detail_url = urljoin(_BASE_URL, href)
            cells = row.find_all("td")
            cell_texts = [_node_text(cell) for cell in cells]
            post_number = cell_texts[0] if cell_texts and cell_texts[0].isdigit() else None
            listed_raw = next((text for text in cell_texts if _parse_date(text)), None)
            listed_date = _parse_date(listed_raw)

            list_author_raw = None
            author_match = re.search(r"<!--\s*<li[^>]*>(.*?)</li>\s*-->", str(row), re.S)
            if author_match:
                list_author_raw = _normalize_text(re.sub(r"<[^>]+>", " ", author_match.group(1)))

            items.append({
                "external_id": list_no,
                "list_no": list_no,
                "post_number": post_number,
                "title": _node_text(anchor) or anchor.get("title") or "",
                "listed_date": listed_date,
                "listed_date_raw": listed_raw,
                "url": detail_url,
                "page": page,
                "cell_texts": cell_texts,
                "list_author_raw": list_author_raw,
                "list_has_attachment": bool(row.find("img", alt=re.compile(r"첨부|pdf|file", re.I))),
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] row parse failed on page {page}: {exc}")
            continue

    return items


def _parse_detail_page(raw: str, detail_url: str, list_no: str) -> dict:
    soup = _make_soup(raw)
    if soup is None:
        return {}

    view = soup.find("div", class_="tstyle_view") or soup.find(class_="board_view")
    if view is None:
        return {}

    title_node = view.find("div", class_="title") or view.find(["h1", "h2", "h3", "h4"], class_="title")
    title = _node_text(title_node)

    detail_fields: dict[str, str] = {}
    head = view.find("ul", class_="head")
    if head:
        for li in head.find_all("li"):
            strong = li.find("strong")
            if not strong:
                continue
            label = _node_text(strong).rstrip(":")
            value_node = li.find("span")
            value = _node_text(value_node) if value_node else _node_text(li).replace(label, "", 1).strip()
            if label and value:
                detail_fields[label] = value

    published_date_raw = detail_fields.get("등록일") or detail_fields.get("작성일")
    published_date = _parse_date(published_date_raw)
    department = (
        detail_fields.get("담당 부서")
        or detail_fields.get("담당부서")
        or detail_fields.get("부서")
    )
    contact = detail_fields.get("연락처")
    contact_person = detail_fields.get("담당자")
    view_count = detail_fields.get("조회수")

    body_parts = []
    for body in view.find_all("div", class_="tb_contents"):
        text = _node_text(body)
        if text:
            body_parts.append(text)
    body_text = _normalize_text(" ".join(body_parts))

    attachments: list[dict] = []
    seen_file_urls: set[str] = set()
    file_section = view.find("div", class_="add_file_list") or view
    links = file_section.find_all("a", class_="file_down")
    if not links:
        links = file_section.find_all("a", href=re.compile(r"boardDownload\.es"))

    for link in links:
        href = unescape(link.get("href", ""))
        if "boardDownload.es" not in href:
            continue
        full_url = urljoin(_BASE_URL, href)
        if full_url in seen_file_urls:
            continue
        seen_file_urls.add(full_url)

        filename = (
            _clean_filename(link.get("title"))
            or _clean_filename(_node_text(link))
            or _filename_from_url(full_url)
        )
        seq_match = re.search(r"[?&]seq=(\d+)", href)
        parent = link.find_parent("li")
        size_node = parent.find(class_="fileSize") if parent else None
        attachments.append({
            "seq": seq_match.group(1) if seq_match else None,
            "url": full_url,
            "filename": filename,
            "size": _node_text(size_node) if size_node else None,
        })

    preferred_file = None
    for attachment in attachments:
        filename = (attachment.get("filename") or "").lower()
        if filename.endswith(".pdf"):
            preferred_file = attachment
            break
    if preferred_file is None and attachments:
        preferred_file = attachments[0]

    filenames = [a["filename"] for a in attachments if a.get("filename")]
    context_parts = []
    if title:
        context_parts.append(f"제목: {title}")
    if published_date:
        context_parts.append(f"등록일: {published_date}")
    if department:
        context_parts.append(f"담당 부서: {department}")
    if contact_person:
        context_parts.append(f"담당자: {contact_person}")
    if filenames:
        context_parts.append("첨부파일: " + "; ".join(filenames[:5]))

    abstract_parts = [part for part in (body_text, " | ".join(context_parts)) if part]
    abstract = _normalize_text(" ".join(abstract_parts))

    return {
        "title": title,
        "abstract": abstract,
        "body_text": body_text,
        "published_date": published_date,
        "published_date_raw": published_date_raw,
        "department": department,
        "contact": contact,
        "contact_person": contact_person,
        "view_count": view_count,
        "pdf_url": preferred_file.get("url") if preferred_file else None,
        "original_filename": preferred_file.get("filename") if preferred_file else None,
        "attachments": attachments,
        "detail_fields": detail_fields,
        "detail_url": detail_url,
        "list_no": list_no,
    }


class MolegGoKrBoardesCrawler(BaseCrawler):
    site_id = "moleg-go-kr-boardes"
    site_name = "Custom: moleg-go-kr-boardes"
    base_url = "https://www.moleg.go.kr"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        while page <= _MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed >= _MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget at page {page}; exiting cleanly")
                break

            list_url = f"{_LIST_URL}&nPage={page}"
            raw = _curl_get(list_url, self.USER_AGENT)
            if not raw:
                print(f"[{self.site_id}] list page {page} fetch failed; stopping pagination")
                break

            items = _parse_list_page(raw, page)
            if not items:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping pagination")
                break

            unseen_items = [item for item in items if item.get("url") not in seen_urls]
            if not unseen_items:
                print(f"[{self.site_id}] page {page} has no unseen URLs; stopping pagination")
                break

            for item in unseen_items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= _MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget during page {page}; exiting cleanly")
                    return saved

                detail_url = item.get("url")
                seen_urls.add(detail_url)
                try:
                    detail_raw = _curl_get(detail_url, self.USER_AGENT)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item.get('external_id')} failed: detail fetch returned empty")
                        continue

                    detail = _parse_detail_page(detail_raw, detail_url, item.get("list_no") or "")
                    if not detail:
                        print(f"[{self.site_id}] item {item.get('external_id')} failed: detail parse returned empty")
                        continue

                    title = detail.get("title") or item.get("title") or "(untitled)"
                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{self.site_id}] item {item.get('external_id')} abstract too short ({len(abstract)} chars), skipping")
                        continue

                    listed_date = item.get("listed_date")
                    published_date = detail.get("published_date") or listed_date
                    original_filename = detail.get("original_filename")
                    metadata = {
                        "mid": _MID,
                        "bid": _BID,
                        "list_no": item.get("list_no"),
                        "node_id": item.get("list_no"),
                        "post_number": item.get("post_number"),
                        "display_post_number": item.get("post_number"),
                        "posted_date": item.get("listed_date_raw") or listed_date,
                        "listed_date": listed_date,
                        "originalFilename": original_filename,
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "list_page": item.get("page"),
                        "list_url": f"{_LIST_URL}&nPage={page}",
                        "detail_url": detail_url,
                        "list_title": item.get("title"),
                        "list_cells": item.get("cell_texts"),
                        "list_author_raw": item.get("list_author_raw"),
                        "list_has_attachment": item.get("list_has_attachment"),
                        "published_date_raw": detail.get("published_date_raw"),
                        "body_text_raw": detail.get("body_text"),
                        "department": detail.get("department"),
                        "contact": detail.get("contact"),
                        "contact_person": detail.get("contact_person"),
                        "view_count": detail.get("view_count"),
                        "detail_fields": detail.get("detail_fields"),
                        "attachments": detail.get("attachments"),
                    }

                    paper = {
                        "id": f"{self.site_id}:{item.get('external_id')}",
                        "site_id": self.site_id,
                        "external_id": item.get("external_id"),
                        "post_number": item.get("post_number"),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": detail.get("contact_person") or item.get("list_author_raw") or "",
                        "publisher": _PUBLISHER,
                        "department": detail.get("department") or "",
                        "journal": "",
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": "",
                        "category": _CATEGORY,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('external_id') or detail_url} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            page += 1

        if page > _MAX_PAGES:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
