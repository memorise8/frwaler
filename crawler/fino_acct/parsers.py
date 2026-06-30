from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .models import AttachmentLink, ExtractedLinks, Target


ATTACHMENT_EXTENSIONS: Final[tuple[str, ...]] = (
    ".pdf",
    ".hwp",
    ".hwpx",
    ".xls",
    ".xlsx",
    ".doc",
    ".docx",
    ".zip",
)
KASB_FILE_DOWNLOAD_RE: Final[re.Pattern[str]] = re.compile(
    r"fileDownload\('([^']+)'\s*,\s*'([^']+)'\)"
)
KASB_FN_DETAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"fn_Detail\('([^']+)'\s*,\s*'([^']+)'\)"
)
FSC_DETAIL_RE: Final[re.Pattern[str]] = re.compile(r"/no010101/\d+")
FSS_DETAIL_RE: Final[re.Pattern[str]] = re.compile(r"view\.do\?[^\"'>]*\bnttId=\d+")
ACCOUNTING_KEYWORDS: Final[tuple[str, ...]] = (
    "회계",
    "감리",
    "감사",
    "외부감사",
    "재무제표",
    "사업보고서",
    "공인회계사",
    "K-IFRS",
    "IFRS",
)


def clean_text(value: str) -> str:
    return " ".join(value.split())


def filename_from_text(text: str, fallback: str) -> str:
    cleaned = clean_text(text)
    if cleaned:
        return cleaned
    parsed_name = urlparse(fallback).path.rsplit("/", 1)[-1]
    if parsed_name:
        return parsed_name
    return "download.bin"


def looks_like_attachment(href: str, text: str) -> bool:
    lowered = f"{href} {text}".lower()
    return any(extension in lowered for extension in ATTACHMENT_EXTENSIONS) or "filedown" in lowered or "getfile" in lowered


def kasb_attachment(base_url: str, onclick: str, text: str) -> AttachmentLink | None:
    match = KASB_FILE_DOWNLOAD_RE.search(onclick)
    if match is None:
        return None
    file_no = match.group(1)
    file_seq = match.group(2)
    download_url = urljoin(base_url, "/commonFile/fileDownload.do")
    return AttachmentLink(
        url=_with_query(download_url, {"fileNo": file_no, "fileSeq": file_seq}),
        filename=filename_from_text(text, file_no),
        method="POST",
        post_file_no=file_no,
        post_file_seq=file_seq,
    )


def extract_links(base_url: str, soup: BeautifulSoup) -> ExtractedLinks:
    details: list[str] = []
    attachments: list[AttachmentLink] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        text = clean_text(anchor.get_text(" ", strip=True))
        onclick = str(anchor.get("onclick", ""))
        kasb_link = kasb_attachment(base_url, onclick, text)
        if kasb_link is not None:
            attachments.append(kasb_link)
            continue
        absolute_url = urljoin(base_url, href)
        if looks_like_attachment(href, text):
            attachments.append(AttachmentLink(url=absolute_url, filename=filename_from_text(text, absolute_url)))
            continue
        if FSC_DETAIL_RE.search(href):
            details.append(absolute_url)
    return ExtractedLinks(details=tuple(dict.fromkeys(details)), attachments=tuple(_dedupe_attachments(attachments)))


def extract_fss_details(base_url: str, soup: BeautifulSoup) -> tuple[str, ...]:
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        if FSS_DETAIL_RE.search(href):
            out.append(urljoin(base_url, href))
    return tuple(dict.fromkeys(out))


def _is_fss_board(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.endswith("fss.or.kr") and "/fss/bbs/" in parsed.path


def extract_links_for_target(target: Target, base_url: str, soup: BeautifulSoup) -> ExtractedLinks:
    if target.priority == 1:
        return _extract_kasb_list_links(base_url, soup)
    if _is_fss_board(target.url):
        return ExtractedLinks(
            details=extract_fss_details(base_url, soup),
            attachments=extract_links(base_url, soup).attachments,
        )
    if target.priority != 8:
        return extract_links(base_url, soup)
    details: list[str] = []
    attachments: list[AttachmentLink] = []
    for row in soup.find_all("tr"):
        row_text = clean_text(row.get_text(" ", strip=True))
        if not any(keyword in row_text for keyword in ACCOUNTING_KEYWORDS):
            continue
        row_links = extract_links(base_url, BeautifulSoup(str(row), "html.parser"))
        details.extend(row_links.details)
        attachments.extend(row_links.attachments)
    return ExtractedLinks(details=tuple(dict.fromkeys(details)), attachments=tuple(_dedupe_attachments(attachments)))


def _extract_kasb_list_links(base_url: str, soup: BeautifulSoup) -> ExtractedLinks:
    attachments: list[AttachmentLink] = []
    items: list[tuple[str, str]] = []
    for row in soup.select("tbody tr"):
        row_soup = BeautifulSoup(str(row), "html.parser")
        row_links = extract_links(base_url, row_soup)
        attachments.extend(row_links.attachments)
        for anchor in row_soup.find_all("a"):
            match = KASB_FN_DETAIL_RE.search(str(anchor.get("onclick", "")))
            if match is not None:
                items.append((match.group(1), match.group(2)))
    return ExtractedLinks(
        details=(),
        attachments=tuple(_dedupe_attachments(attachments)),
        kasb_items=tuple(dict.fromkeys(items)),
    )


def _dedupe_attachments(attachments: list[AttachmentLink]) -> list[AttachmentLink]:
    seen: set[tuple[str, str, str]] = set()
    output: list[AttachmentLink] = []
    for link in attachments:
        key = (link.url, link.post_file_no, link.post_file_seq)
        if key not in seen:
            seen.add(key)
            output.append(link)
    return output


def _with_query(url: str, pairs: dict[str, str]) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def page_title(soup: BeautifulSoup, fallback: str) -> str:
    heading = soup.find(["h1", "h2", "h3"])
    if heading is not None:
        text = clean_text(heading.get_text(" ", strip=True))
        if text:
            return text
    if soup.title is not None:
        text = clean_text(soup.title.get_text(" ", strip=True))
        if text:
            return text
    return fallback


def page_body(soup: BeautifulSoup) -> str:
    return clean_text(soup.get_text(" ", strip=True))
