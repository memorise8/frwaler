# -*- coding: utf-8 -*-
"""Crawler for ED Infrastructure and Sustainability pages.

The ed.gov Drupal JSON:API endpoint is disabled publicly, but the site search
view is a real paginated list endpoint:

    /search?search_api_fulltext=infrastructure+sustainability&page=N

Rows from that view link to detail pages whose HTML carries node ids, page
summaries, body content, governing office, and last-reviewed dates.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from email.message import Message
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


_SITE_ID = "ed-gov-about"
_BASE_URL = "https://www.ed.gov"
_START_URL = (
    "https://www.ed.gov/about/initiatives/"
    "infrastructure-and-sustainability"
)
_SEARCH_QUERY = "infrastructure sustainability"
_PARSERS = ("html5lib", "lxml", "html.parser")
_BACKOFF_SECONDS = (1, 3, 9)
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_CHARS = 100


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _curl_text(
    url: str,
    *,
    user_agent: str,
    referer: str | None = None,
    method: str = "GET",
    retries: int = 3,
    timeout: int = 45,
) -> tuple[str | None, dict[str, str], str]:
    """Fetch a URL with curl, returning (body, headers, effective_url)."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--max-time",
        str(timeout),
        "-D",
        "-",
        "-w",
        "\n__ED_CRAWLER_EFFECTIVE_URL__:%{url_effective}",
        "-H",
        f"User-Agent: {user_agent}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if method.upper() == "HEAD":
        cmd += ["-I"]
    cmd.append(url)

    last_error = ""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout + 10,
            )
            raw = result.stdout.decode("utf-8", errors="replace")
            body, headers, effective_url = _split_curl_response(raw, url)
            if result.returncode == 0 and (body.strip() or method.upper() == "HEAD"):
                return body, headers, effective_url
            last_error = (
                result.stderr.decode("utf-8", errors="replace").strip()
                or f"curl exit {result.returncode}; empty body"
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {exc.timeout}s"
        except Exception as exc:
            last_error = str(exc)

        if attempt < retries - 1:
            wait = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
            print(
                f"[{_SITE_ID}] curl attempt {attempt + 1}/{retries} failed "
                f"for {url}: {last_error}; retry in {wait}s"
            )
            time.sleep(wait)

    print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {last_error}")
    return None, {}, url


def _split_curl_response(raw: str, fallback_url: str) -> tuple[str, dict[str, str], str]:
    marker = "\n__ED_CRAWLER_EFFECTIVE_URL__:"
    effective_url = fallback_url
    if marker in raw:
        raw, effective_url = raw.rsplit(marker, 1)
        effective_url = effective_url.strip() or fallback_url

    # curl -D - with -L emits one header block per redirect. The body follows
    # the final blank-line-separated header block.
    sep = "\r\n\r\n" if "\r\n\r\n" in raw else "\n\n"
    if sep not in raw:
        return raw, {}, effective_url

    parts = raw.split(sep)
    body_index = 0
    header_blocks: list[str] = []
    for idx, part in enumerate(parts):
        stripped = part.strip()
        if stripped.startswith("HTTP/"):
            header_blocks.append(part)
            body_index = idx + 1
        elif header_blocks:
            break
    body = sep.join(parts[body_index:]) if body_index < len(parts) else ""
    headers = _parse_headers(header_blocks[-1] if header_blocks else "")
    return body, headers, effective_url


def _parse_headers(block: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line or line.lower().startswith("http/"):
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _make_soup(raw: str | bytes | None):
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup import failed: {exc}")
        return None

    for parser in _PARSERS:
        try:
            return BeautifulSoup(text, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
    return None


def _meta_content(soup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name})
    if not tag:
        tag = soup.find("meta", attrs={"property": name})
    return _clean_text(tag.get("content")) if tag and tag.get("content") else ""


def _parse_date(raw: str | None) -> str | None:
    text = _clean_text(raw)
    if not text:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        m = re.search(r"([A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})", text)
        if not m:
            continue
        try:
            return datetime.strptime(m.group(1).replace(".", ""), fmt).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            continue
    m = re.search(r"\b(20\d{2}|19\d{2})\b", text)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").split("/")[-1]
    tail = unquote(tail)
    return tail if "." in tail and len(tail) <= 200 else None


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    msg = Message()
    msg["content-disposition"] = value
    filename = msg.get_param("filename", header="content-disposition")
    if not filename:
        filename = msg.get_param("filename*", header="content-disposition")
    if isinstance(filename, tuple):
        filename = filename[-1]
    if filename:
        filename = unquote(str(filename).strip("\"'"))
    return filename or None


def _is_probable_pdf(url: str, headers: dict[str, str] | None = None) -> bool:
    content_type = (headers or {}).get("content-type", "").lower()
    path = urlparse(url).path.lower()
    return (
        "application/pdf" in content_type
        or path.endswith(".pdf")
        or "/media/document/" in path
        or "/sites/ed/files/" in path
    )


def _extract_node_id(raw: str, soup) -> str | None:
    for pattern in (
        r'"currentPath"\s*:\s*"node/(\d+)"',
        r"\bpage-node-(\d+)\b",
        r"\bnode--id-(\d+)\b",
        r"data-drupal-link-system-path=[\"']node/(\d+)[\"']",
    ):
        m = re.search(pattern, raw)
        if m:
            return m.group(1)
    if soup and soup.body:
        classes = soup.body.get("class") or []
        for cls in classes:
            m = re.search(r"page-node-(\d+)", cls)
            if m:
                return m.group(1)
    return None


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] if path else ""
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")
    return slug[:160] or url[:160]


def _numeric_suffix(text: str | None) -> str | None:
    if not text:
        return None
    matches = re.findall(r"\b(\d{3,})\b", text)
    return matches[-1] if matches else None


def _remove_noise(root) -> None:
    for selector in (
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "form",
        ".usa-banner",
        ".usa-overlay",
        ".usa-skipnav",
        ".breadcrumb",
        ".ed-breadcrumb",
        ".ed-tags-section",
        ".usa-footer",
        ".usa-nav",
    ):
        for tag in root.select(selector):
            tag.decompose()


class EdGovAboutCrawler(BaseCrawler):
    site_id = "ed-gov-about"
    site_name = "Custom: ed-gov-about"
    base_url = "https://www.ed.gov"

    _detail_delay = 1.0

    def crawl(self, limit=None):
        """Crawl ed.gov Infrastructure and Sustainability search results."""
        saved = 0
        start_time = time.time()
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for p in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if p % 10 == 0:
                print(f"[ed-gov-about] page {p}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(p)
            raw, _, effective_list_url = _curl_text(
                list_url,
                user_agent=self.USER_AGENT,
                referer=_START_URL,
            )
            if not raw:
                print(f"[{self.site_id}] empty list response at page {p}; stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page parse failed at page {p}; stopping")
                break

            rows = self._parse_list_rows(soup, effective_list_url)
            if not rows:
                print(f"[{self.site_id}] no rows at page {p}; stopping")
                break

            page_new_urls = 0
            for idx, item in enumerate(rows, 1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    return saved

                detail_url = item.get("url") or ""
                if not detail_url:
                    continue
                dedup_url = detail_url.split("#", 1)[0].rstrip("/")
                if dedup_url in seen_urls:
                    continue
                seen_urls.add(dedup_url)
                page_new_urls += 1

                try:
                    time.sleep(max(self._detail_delay, float(getattr(self, "_delay", 1.0))))
                    detail = self._fetch_and_parse_detail(item)
                    if not detail:
                        continue

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipped {detail_url}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    external_id = (
                        detail.get("node_id")
                        or _numeric_suffix(detail.get("pdf_url") or detail_url)
                        or _slug_from_url(detail_url)
                    )
                    post_number = detail.get("node_id") or _numeric_suffix(
                        detail.get("pdf_url") or detail_url
                    )
                    if not post_number:
                        post_number = _slug_from_url(detail_url)

                    listed_date_raw = (
                        item.get("listed_date_raw")
                        or detail.get("listed_date_raw")
                        or detail.get("date_raw")
                    )
                    listed_date = item.get("listed_date") or detail.get("listed_date")
                    published_date = detail.get("published_date") or listed_date

                    original_filename = detail.get("original_filename")
                    metadata = {
                        "posted_date": listed_date_raw,
                        "originalFilename": original_filename,
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "node_id": detail.get("node_id"),
                        "content_type": detail.get("content_type"),
                        "native_slug": _slug_from_url(detail_url),
                        "list_endpoint": "/search",
                        "list_page": p,
                        "list_url": list_url,
                        "detail_endpoint": detail_url,
                        "search_query": _SEARCH_QUERY,
                        "list_category": item.get("category"),
                        "list_snippet": item.get("snippet"),
                        "office_meta": detail.get("office_meta"),
                        "last_reviewed_raw": detail.get("last_reviewed_raw"),
                        "effective_url": detail.get("effective_url"),
                    }

                    paper = {
                        "id": f"{self.site_id}:{external_id}",
                        "site_id": self.site_id,
                        "external_id": str(external_id),
                        "post_number": str(post_number) if post_number else None,
                        "title": detail.get("title") or item.get("title") or "",
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": "",
                        "publisher": "U.S. Department of Education",
                        "department": detail.get("department") or "",
                        "journal": "",
                        "url": detail.get("url") or detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords") or "infrastructure,sustainability,education",
                        "category": detail.get("category") or item.get("category") or "",
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ed-gov-about] item {detail_url} failed: {exc}")
                    continue

            if page_new_urls == 0:
                print(f"[{self.site_id}] page {p} had no new URLs; stopping")
                break
            if not self._has_next_page(soup, p):
                print(f"[{self.site_id}] next page link absent at page {p}; stopping")
                break
        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved

    def _list_url(self, page_number: int) -> str:
        # Drupal search uses zero-based page query parameters.
        page_param = max(0, page_number - 1)
        return (
            f"{self.base_url}/search?"
            f"search_api_fulltext={quote_plus(_SEARCH_QUERY)}&page={page_param}"
        )

    def _parse_list_rows(self, soup, list_url: str) -> list[dict]:
        rows = []
        for row in soup.select(".views-row"):
            title_link = row.select_one(".results-list-title a[href]")
            if not title_link:
                title_link = row.find("a", href=True)
            if not title_link:
                continue

            href = title_link.get("href") or ""
            if not href or href.startswith(("mailto:", "#")):
                continue
            full_url = urljoin(self.base_url, href)

            category = ""
            cat_tag = row.select_one(".results-list-tag .field-content")
            if cat_tag:
                category = _clean_text(cat_tag.get_text(" ", strip=True))

            snippet = ""
            snippet_tag = row.select_one(".results-list-body .field-content")
            if snippet_tag:
                snippet = _clean_text(snippet_tag.get_text(" ", strip=True))

            listed_date_raw = ""
            for selector in (".views-field-created", ".views-field-changed", "time"):
                tag = row.select_one(selector)
                if tag:
                    listed_date_raw = (
                        tag.get("datetime")
                        or _clean_text(tag.get_text(" ", strip=True))
                    )
                    break

            rows.append(
                {
                    "title": _clean_text(title_link.get_text(" ", strip=True)),
                    "url": full_url,
                    "category": category,
                    "snippet": snippet,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": _parse_date(listed_date_raw),
                    "list_url": list_url,
                }
            )
        return rows

    def _has_next_page(self, soup, current_page: int) -> bool:
        next_link = soup.select_one('a[aria-label="Next page"][href]')
        if next_link:
            return True
        for link in soup.select(".usa-pagination a[href]"):
            href = link.get("href") or ""
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            try:
                page_value = int((qs.get("page") or ["-1"])[0])
            except ValueError:
                continue
            if page_value >= current_page:
                return True
        return False

    def _fetch_and_parse_detail(self, item: dict) -> dict | None:
        url = item["url"]
        raw, headers, effective_url = _curl_text(
            url,
            user_agent=self.USER_AGENT,
            referer=item.get("list_url") or _START_URL,
        )
        if raw is None:
            return None

        if _is_probable_pdf(effective_url, headers):
            filename = (
                _filename_from_content_disposition(headers.get("content-disposition"))
                or _filename_from_url(effective_url)
            )
            snippet = item.get("snippet") or ""
            return {
                "title": item.get("title") or filename or _slug_from_url(effective_url),
                "abstract": snippet,
                "published_date": item.get("listed_date"),
                "listed_date": item.get("listed_date"),
                "listed_date_raw": item.get("listed_date_raw"),
                "date_raw": item.get("listed_date_raw"),
                "department": "",
                "category": item.get("category") or "Document",
                "pdf_url": effective_url,
                "original_filename": filename,
                "node_id": _numeric_suffix(effective_url),
                "content_type": "Document",
                "office_meta": "",
                "url": effective_url,
                "effective_url": effective_url,
                "keywords": "infrastructure,sustainability,education",
            }

        soup = _make_soup(raw)
        if soup is None:
            return None

        main = soup.find("main") or soup.find(id="main-content") or soup.body or soup
        article = main.find("article") or main
        content_root = article.__copy__() if hasattr(article, "__copy__") else article
        _remove_noise(content_root)

        title = self._extract_title(soup, main, item)
        node_id = _extract_node_id(raw, soup)
        content_type = self._extract_content_type(soup, raw, item)
        department = self._extract_department(main, soup)
        date_raw = self._extract_date_raw(main, soup)
        parsed_date = _parse_date(date_raw)
        pdf_url = self._extract_pdf_url(article, effective_url)
        original_filename = _filename_from_url(pdf_url)
        keywords = self._extract_keywords(soup)

        abstract = self._extract_abstract(soup, content_root, item)
        category = item.get("category") or content_type

        return {
            "title": title,
            "abstract": abstract,
            "published_date": parsed_date,
            "listed_date": parsed_date,
            "listed_date_raw": date_raw,
            "date_raw": date_raw,
            "last_reviewed_raw": date_raw,
            "department": department,
            "category": category,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "node_id": node_id,
            "content_type": content_type,
            "office_meta": _meta_content(soup, "office"),
            "url": effective_url,
            "effective_url": effective_url,
            "keywords": keywords,
        }

    def _extract_title(self, soup, main, item: dict) -> str:
        for candidate in (
            _meta_content(soup, "og:title"),
            _clean_text(main.find("h1").get_text(" ", strip=True)) if main.find("h1") else "",
            item.get("title") or "",
        ):
            title = _clean_text(candidate)
            if title:
                suffix = " | U.S. Department of Education"
                if title.endswith(suffix):
                    title = title[: -len(suffix)].strip()
                return title
        if soup.title:
            return _clean_text(soup.title.get_text(" ", strip=True))
        return item.get("title") or ""

    def _extract_content_type(self, soup, raw: str, item: dict) -> str:
        if soup.body:
            classes = " ".join(soup.body.get("class") or [])
            m = re.search(r"page-node-type-([a-z0-9-]+)", classes)
            if m:
                return m.group(1).replace("-", " ").title()
        m = re.search(r"\bnode--type-([a-z0-9-]+)\b", raw)
        if m:
            return m.group(1).replace("-", " ").title()
        return item.get("category") or ""

    def _extract_department(self, main, soup) -> str:
        for selector in (
            ".views-field-field-ed-governing-organization .field-content",
            ".field--name-field-ed-offices",
        ):
            tag = main.select_one(selector)
            if tag:
                text = _clean_text(tag.get_text(" ", strip=True))
                if text:
                    return text
        office = _meta_content(soup, "office")
        return office

    def _extract_date_raw(self, main, soup) -> str:
        for selector in (
            ".views-field-field-ed-last-reviewed",
            ".field--name-field-ed-date",
            ".field--name-field-date",
            "time[datetime]",
        ):
            tag = main.select_one(selector)
            if not tag:
                continue
            raw = tag.get("datetime") or _clean_text(tag.get_text(" ", strip=True))
            if raw:
                return raw

        text = _clean_text(main.get_text(" ", strip=True))
        m = re.search(r"Page Last Reviewed:\s*([A-Za-z]+ \d{1,2}, \d{4})", text)
        if m:
            return m.group(1)

        for meta_name in ("article:published_time", "article:modified_time"):
            raw = _meta_content(soup, meta_name)
            if raw:
                return raw
        return ""

    def _extract_pdf_url(self, article, base_url: str) -> str | None:
        for a in article.find_all("a", href=True):
            href = a.get("href") or ""
            if _is_probable_pdf(href):
                return urljoin(base_url, href)
        return None

    def _extract_keywords(self, soup) -> str:
        values: list[str] = []
        for name in ("keywords", "sitetopic"):
            value = _meta_content(soup, name)
            if value and value.lower() != "null":
                values.extend([v.strip() for v in re.split(r"[,;]", value) if v.strip()])
        for tag in soup.select(".ed-tags-section a, .field--name-field-topics a"):
            text = _clean_text(tag.get_text(" ", strip=True))
            if text:
                values.append(text)
        values.extend(["infrastructure", "sustainability", "education"])

        seen: set[str] = set()
        deduped = []
        for value in values:
            key = value.lower()
            if key not in seen:
                seen.add(key)
                deduped.append(value)
        return ",".join(deduped)

    def _extract_abstract(self, soup, content_root, item: dict) -> str:
        meta_desc = _meta_content(soup, "description") or _meta_content(
            soup, "og:description"
        )
        body_text = _clean_text(content_root.get_text(" ", strip=True))

        parts = []
        if meta_desc:
            parts.append(meta_desc)
        if item.get("snippet") and item["snippet"] not in parts:
            parts.append(item["snippet"])
        if body_text and body_text not in parts:
            parts.append(body_text)

        abstract = _clean_text(" ".join(parts))
        return abstract[:5000] if len(abstract) > 5000 else abstract
