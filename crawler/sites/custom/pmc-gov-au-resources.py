# -*- coding: utf-8 -*-
"""PM&C report resources crawler.

Starting URL:
https://www.pmc.gov.au/resources?f%5B0%5D=db_r_publication_category%3A334

The site is a Drupal resource listing. The real list/detail surfaces are the
HTML pages:
- /resources?f[0]=db_r_publication_category:334
- /resources?f[0]=db_r_publication_category:334&page=N
- /resources/<slug>

Some networks receive an Incapsula block for direct curl requests. Direct PM&C
HTML is always attempted first; the Jina reader is used only as a fallback to
read the same public PM&C URLs when direct HTML is blocked.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
from crawler.base_crawler import BaseCrawler


START_URL = (
    "https://www.pmc.gov.au/resources"
    "?f%5B0%5D=db_r_publication_category%3A334"
)
SITE_ID = "pmc-gov-au-resources"
BASE_URL = "https://www.pmc.gov.au"
MIN_ABSTRACT_CHARS = 50
MAX_PAGES = 200
MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
BACKOFF_SECONDS = (1, 3, 9)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class PmcGovAuResourcesCrawler(BaseCrawler):
    site_id = "pmc-gov-au-resources"
    site_name = "Custom: pmc-gov-au-resources"
    base_url = "https://www.pmc.gov.au"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page in range(MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time >= MAX_SECONDS:
                print(f"[{self.site_id}] wall-clock budget reached; stopping cleanly")
                break

            list_url = _list_url(page)
            try:
                raw = self._fetch_with_fallback(list_url, context=f"list page {page}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} fetch failed: {exc}")
                break

            if not raw:
                print(f"[{self.site_id}] page {page}: empty response; stopping")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[{self.site_id}] page {page}: no records; stopping")
                break

            new_on_page = 0
            for index, item in enumerate(items, 1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= MAX_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget reached; stopping cleanly")
                    return saved

                url = item.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(float(getattr(self, "_delay", 1.0)))
                    detail_raw = self._fetch_with_fallback(
                        url, context=f"item {page}:{index}"
                    )
                    if not detail_raw:
                        print(f"[{self.site_id}] item {url} failed: empty detail")
                        continue

                    detail = _parse_detail_page(detail_raw, item)
                    abstract = _clean_text(detail.get("abstract"))
                    if len(abstract) < MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = _build_paper(item, detail)
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break

            if not _has_next_page(raw, page):
                print(f"[{self.site_id}] page {page}: next page absent; stopping")
                break
        else:
            print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached")

        return saved

    def _fetch_with_fallback(self, url: str, *, context: str) -> str | None:
        raw = _curl_get(url, site_id=self.site_id, context=context)
        if raw and not _is_blocked_response(raw) and len(raw.strip()) > 200:
            return raw

        if raw and _is_blocked_response(raw):
            print(f"[{self.site_id}] direct PM&C request blocked for {context}; using reader fallback")

        reader_url = _reader_url(url)
        fallback = _curl_get(reader_url, site_id=self.site_id, context=f"{context} reader")
        if fallback and not _is_blocked_response(fallback) and len(fallback.strip()) > 80:
            return fallback
        return raw if raw and not _is_blocked_response(raw) else None


def _list_url(page: int) -> str:
    if page <= 0:
        return START_URL
    return f"{START_URL}&page={page}"


def _reader_url(url: str) -> str:
    # Encode the target query string into the path so reader-specific query
    # parameters such as "page" do not consume the target site's pagination.
    return "https://r.jina.ai/http://" + quote(unquote(url), safe=":/")


def _curl_get(url: str, *, site_id: str, context: str, timeout: int = 45) -> str | None:
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--compressed",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-AU,en;q=0.9",
        url,
    ]
    for attempt, wait in enumerate(BACKOFF_SECONDS, 1):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout + 5,
                check=False,
            )
            body = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and body.strip():
                return body
            err = result.stderr.decode("utf-8", errors="replace").strip()
            if attempt < len(BACKOFF_SECONDS):
                print(
                    f"[{site_id}] curl failed for {context} "
                    f"(attempt {attempt}/3): {err or 'empty response'}; retry in {wait}s"
                )
                time.sleep(wait)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt < len(BACKOFF_SECONDS):
                print(
                    f"[{site_id}] curl error for {context} "
                    f"(attempt {attempt}/3): {exc}; retry in {wait}s"
                )
                time.sleep(wait)
            else:
                print(f"[{site_id}] curl failed after 3 attempts for {context}: {exc}")
                return None

    print(f"[{site_id}] curl failed after 3 attempts for {context}")
    return None


def _is_blocked_response(raw: str) -> bool:
    sample = raw[:2000].lower()
    return (
        "incapsula" in sample
        or "_incapsula_resource" in sample
        or "request unsuccessful" in sample
        or "noindex,nofollow" in sample and "swjiylwa" in sample
    )


def _make_soup(raw: str):
    from bs4 import BeautifulSoup

    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"no usable BeautifulSoup parser: {last_exc}")


def _parse_list_page(raw: str) -> list[dict[str, Any]]:
    if _looks_like_reader_markdown(raw):
        return _parse_markdown_list(raw)
    return _parse_html_list(raw)


def _looks_like_reader_markdown(raw: str) -> bool:
    return raw.lstrip().startswith("Title:") and "Markdown Content:" in raw


def _parse_markdown_list(raw: str) -> list[dict[str, Any]]:
    lines = [line.rstrip() for line in raw.splitlines()]
    items: list[dict[str, Any]] = []
    seen = set()

    for idx, line in enumerate(lines):
        if not line.startswith("## ["):
            continue
        match = re.match(
            r"^## \[(?P<title>.+?)\]\((?P<url>https://www\.pmc\.gov\.au/resources/[^)\s]+)",
            line,
        )
        if not match:
            continue

        url = _clean_url(match.group("url"))
        if url in seen:
            continue
        seen.add(url)

        title = _clean_text(match.group("title"))
        prev_line = _nearest_nonempty(lines, idx, -1)
        next_line = _nearest_nonempty(lines, idx, 1)
        date_raw = next_line if _parse_date(next_line) else ""
        category = prev_line if prev_line and not prev_line.startswith("#") else ""

        items.append(
            {
                "title": title,
                "url": url,
                "category": category,
                "listed_date_raw": date_raw,
                "listed_date": _parse_date(date_raw),
                "source": "reader_markdown",
            }
        )

    return items


def _parse_html_list(raw: str) -> list[dict[str, Any]]:
    try:
        soup = _make_soup(raw)
    except Exception as exc:
        print(f"[{SITE_ID}] list HTML parse failed: {exc}")
        return []

    scope = soup.find("main") or soup
    rows = scope.select(".views-row")
    if not rows:
        rows = []
        for link in scope.find_all("a", href=True):
            href = link.get("href", "")
            if re.search(r"^/resources/[^?#]+|^https://www\.pmc\.gov\.au/resources/[^?#]+", href):
                container = link.find_parent(["article", "li", "div"]) or link.parent
                if container and container not in rows:
                    rows.append(container)

    items: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        link = row.find("a", href=re.compile(r"(/resources/|www\.pmc\.gov\.au/resources/)"))
        if not link:
            continue
        href = link.get("href") or ""
        url = _clean_url(urljoin(BASE_URL, href))
        if url.rstrip("/") == f"{BASE_URL}/resources" or url in seen:
            continue
        seen.add(url)

        title = _clean_text(link.get_text(" ", strip=True))
        if not title:
            continue

        text = _clean_text(row.get_text(" ", strip=True))
        date_raw = ""
        time_el = row.find("time")
        if time_el:
            date_raw = time_el.get_text(" ", strip=True) or time_el.get("datetime", "")
        if not date_raw:
            match = re.search(
                r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
                r"\d{1,2}\s+[A-Za-z]+\s+\d{4}\b",
                text,
            )
            date_raw = match.group(0) if match else ""

        category = ""
        if date_raw:
            before_title = text.split(title, 1)[0].strip()
            category = before_title.split("  ")[-1].strip() if before_title else ""

        items.append(
            {
                "title": title,
                "url": url,
                "category": category,
                "listed_date_raw": date_raw,
                "listed_date": _parse_date(date_raw),
                "source": "html",
            }
        )

    return items


def _nearest_nonempty(lines: list[str], idx: int, step: int) -> str:
    pos = idx + step
    while 0 <= pos < len(lines):
        value = lines[pos].strip()
        if value:
            return value
        pos += step
    return ""


def _parse_detail_page(raw: str, item: dict[str, Any]) -> dict[str, Any]:
    if _looks_like_reader_markdown(raw):
        return _parse_markdown_detail(raw, item)
    return _parse_html_detail(raw, item)


def _parse_markdown_detail(raw: str, item: dict[str, Any]) -> dict[str, Any]:
    title = item.get("title") or _metadata_line(raw, "Title")
    area = _markdown_field(raw, "Area/program/initiative") or item.get("category")
    release_raw = _markdown_field(raw, "Release date") or item.get("listed_date_raw")
    category = _markdown_field(raw, "Category") or "Report"
    authors = _markdown_field(raw, "Authors")
    pdf_url, original_filename = _first_pdf_from_markdown(raw)

    abstract = _extract_markdown_abstract(raw, title)
    metadata = {
        "posted_date": item.get("listed_date_raw") or release_raw,
        "listed_date": item.get("listed_date"),
        "release_date_raw": release_raw,
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "slug": _slug_from_url(item.get("url")),
        "node_id": None,
        "resource_category_id": "334",
        "resource_category_filter": "db_r_publication_category:334",
        "area_program_initiative": area,
        "detail_source": "reader_markdown",
        "list_source": item.get("source"),
    }

    return {
        "title": title,
        "abstract": abstract,
        "published_date": _parse_date(release_raw) or item.get("listed_date"),
        "published_date_raw": release_raw,
        "listed_date": item.get("listed_date") or _parse_date(release_raw),
        "listed_date_raw": item.get("listed_date_raw") or release_raw,
        "authors": authors,
        "publisher": authors or "Department of the Prime Minister and Cabinet",
        "department": area,
        "journal": None,
        "pdf_url": pdf_url,
        "keywords": _join_values([area, category]),
        "category": category,
        "doi": None,
        "original_filename": original_filename,
        "metadata": metadata,
    }


def _parse_html_detail(raw: str, item: dict[str, Any]) -> dict[str, Any]:
    try:
        soup = _make_soup(raw)
    except Exception as exc:
        print(f"[{SITE_ID}] detail HTML parse failed for {item.get('url')}: {exc}")
        return _parse_markdown_detail("", item)

    scope = soup.find("main") or soup
    h1 = scope.find("h1")
    title = _clean_text(h1.get_text(" ", strip=True)) if h1 else item.get("title", "")
    page_text = _clean_text(scope.get_text("\n", strip=True))
    area = _label_from_text(page_text, "Area/program/initiative") or item.get("category")
    release_raw = _label_from_text(page_text, "Release date") or item.get("listed_date_raw")
    category = _label_from_text(page_text, "Category") or "Report"
    authors = _label_from_text(page_text, "Authors")
    pdf_url = ""
    original_filename = ""

    for link in scope.find_all("a", href=True):
        href = link.get("href") or ""
        if ".pdf" not in href.lower():
            continue
        pdf_url = _clean_url(urljoin(BASE_URL, href))
        original_filename = _filename_from_url(pdf_url) or _clean_text(link.get("title", ""))
        break

    abstract = _extract_html_abstract(scope, title)
    metadata = {
        "posted_date": item.get("listed_date_raw") or release_raw,
        "listed_date": item.get("listed_date"),
        "release_date_raw": release_raw,
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "slug": _slug_from_url(item.get("url")),
        "node_id": _node_id_from_html(raw),
        "resource_category_id": "334",
        "resource_category_filter": "db_r_publication_category:334",
        "area_program_initiative": area,
        "detail_source": "html",
        "list_source": item.get("source"),
    }

    return {
        "title": title,
        "abstract": abstract,
        "published_date": _parse_date(release_raw) or item.get("listed_date"),
        "published_date_raw": release_raw,
        "listed_date": item.get("listed_date") or _parse_date(release_raw),
        "listed_date_raw": item.get("listed_date_raw") or release_raw,
        "authors": authors,
        "publisher": authors or "Department of the Prime Minister and Cabinet",
        "department": area,
        "journal": None,
        "pdf_url": pdf_url,
        "keywords": _join_values([area, category]),
        "category": category,
        "doi": None,
        "original_filename": original_filename,
        "metadata": metadata,
    }


def _extract_markdown_abstract(raw: str, title: str) -> str:
    lines = raw.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == f"# {title}":
            start = idx + 1
    if start is None:
        for idx, line in enumerate(lines):
            if line.strip().startswith("# ") and "| PM&C" in line:
                start = idx + 1
                break
    if start is None:
        start = 0

    collected = []
    stop_patterns = (
        "**Area/program/initiative**",
        "[Subscribe and stay up to date]",
        "Connect with us",
        "Footer menu",
        "PM&C acknowledges",
    )
    skip_patterns = (
        "[Skip to main content]",
        "Quicklinks",
        "Main navigation",
        "Type your keywords",
        "Search",
        "[Listen]",
    )
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in stop_patterns):
            break
        if any(stripped.startswith(p) for p in skip_patterns):
            continue
        if re.match(r"^\[\]\(https?://", stripped):
            continue
        if re.match(r"^\d+\.\s+", stripped):
            continue
        if stripped.startswith("*   [") and "pmc.gov.au" in stripped:
            continue
        text = _markdown_to_text(stripped)
        if text and text not in {"PDF", "DOCX"} and not re.match(r"^\d+(?:\.\d+)?\s+[KMG]B$", text):
            collected.append(text)

    return _clean_text(" ".join(collected))


def _extract_html_abstract(scope, title: str) -> str:
    clone = _make_soup(str(scope))
    for bad in clone(["script", "style", "nav", "form", "header", "footer"]):
        bad.decompose()
    text = clone.get_text("\n", strip=True)
    lines = []
    for line in text.splitlines():
        line = _clean_text(line)
        if not line:
            continue
        if line == title:
            continue
        if line in {"Listen", "PDF", "DOCX"}:
            continue
        if line.startswith("Area/program/initiative"):
            break
        if line.startswith("Subscribe and stay up to date"):
            break
        lines.append(line)
    return _clean_text(" ".join(lines))


def _markdown_field(raw: str, label: str) -> str:
    marker = f"**{label}**"
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == marker:
            return _nearest_nonempty(lines, idx, 1)
    return ""


def _metadata_line(raw: str, label: str) -> str:
    for line in raw.splitlines()[:10]:
        if line.startswith(f"{label}:"):
            return _clean_text(line.split(":", 1)[1])
    return ""


def _first_pdf_from_markdown(raw: str) -> tuple[str | None, str | None]:
    pattern = re.compile(
        r"\[[^\]]+\]\((?P<url>https://www\.pmc\.gov\.au/[^)\s]+?\.pdf)"
        r"(?:\s+\"(?P<title>[^\"]+)\")?\)",
        re.I,
    )
    match = pattern.search(raw)
    if not match:
        return None, None
    url = _clean_url(match.group("url"))
    title = match.group("title") or ""
    filename = _filename_from_url(url) or _clean_text(title)
    return url, filename


def _label_from_text(text: str, label: str) -> str:
    lines = [_clean_text(line) for line in text.splitlines() if _clean_text(line)]
    for idx, line in enumerate(lines):
        if line == label and idx + 1 < len(lines):
            return lines[idx + 1]
        if line.startswith(label):
            value = line[len(label):].strip(" :")
            if value:
                return value
    return ""


def _parse_date(raw: str | None) -> str:
    if not raw:
        return ""
    raw = _clean_text(raw)
    match = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if match:
        return match.group(1)
    raw = re.sub(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+", "", raw)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _build_paper(item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    url = item.get("url")
    slug = _slug_from_url(url)
    external_id = slug or hashlib.sha1((url or detail.get("title", "")).encode("utf-8")).hexdigest()
    post_number = _numeric_post_number(url) or external_id
    metadata = dict(detail.get("metadata") or {})
    metadata.update(
        {
            "posted_date": detail.get("listed_date_raw") or item.get("listed_date_raw"),
            "originalFilename": detail.get("original_filename"),
            "journal_raw": metadata.get("journal_raw"),
            "series": metadata.get("series"),
            "volume": metadata.get("volume"),
            "issue": metadata.get("issue"),
            "external_id": external_id,
            "post_number": post_number,
            "url": url,
            "pdf_url": detail.get("pdf_url"),
            "category": detail.get("category"),
            "published_date_raw": detail.get("published_date_raw"),
        }
    )

    return {
        "id": f"{SITE_ID}:{external_id}",
        "site_id": SITE_ID,
        "external_id": external_id,
        "post_number": post_number,
        "title": detail.get("title") or item.get("title"),
        "abstract": detail.get("abstract"),
        "published_date": detail.get("published_date"),
        "listed_date": detail.get("listed_date"),
        "posted_date": detail.get("listed_date"),
        "authors": detail.get("authors"),
        "publisher": detail.get("publisher"),
        "department": detail.get("department"),
        "journal": detail.get("journal"),
        "url": url,
        "pdf_url": detail.get("pdf_url"),
        "keywords": detail.get("keywords"),
        "category": detail.get("category"),
        "doi": detail.get("doi"),
        "original_filename": detail.get("original_filename"),
        "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    }


def _has_next_page(raw: str, page: int) -> bool:
    if _looks_like_reader_markdown(raw):
        if "Next page" in raw or "Last page" in raw:
            return True
        # Reader output can omit pager labels on filtered pages; if a full page
        # of records was parsed, let URL dedupe/empty pages detect the end.
        return len(_parse_markdown_list(raw)) >= 20

    try:
        soup = _make_soup(raw)
    except Exception:
        return True
    next_link = soup.find("a", attrs={"rel": "next"})
    if next_link:
        return True
    pager_text = soup.get_text(" ", strip=True).lower()
    return "next page" in pager_text or "next ›" in pager_text


def _numeric_post_number(url: str | None) -> str | None:
    if not url:
        return None
    matches = re.findall(r"\d{4,}", url)
    return matches[-1] if matches else None


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    return slug or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    filename = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return filename if "." in filename and len(filename) <= 240 else None


def _node_id_from_html(raw: str) -> str | None:
    for pattern in (r"\bnode-(\d+)\b", r'"nid"\s*:\s*"?(?P<nid>\d+)'):
        match = re.search(pattern, raw)
        if match:
            return match.group(1) if match.lastindex else match.group("nid")
    return None


def _clean_url(url: str) -> str:
    return url.replace("&amp;", "&").strip()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _markdown_to_text(value: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = text.replace("**", "")
    text = text.replace("__", "")
    return _clean_text(text)


def _join_values(values: list[str | None]) -> str | None:
    out = []
    for value in values:
        value = _clean_text(value)
        if value and value not in out:
            out.append(value)
    return ", ".join(out) if out else None
