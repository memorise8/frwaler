# -*- coding: utf-8 -*-
"""Crawler for NIDI English publications.

Discovery notes:
  - The WordPress REST API is restricted by Solid Security.
  - The first list page is the rendered HTML at /en/publications/.
  - Additional pages are loaded by the theme through:
    /wp-admin/admin-ajax.php
    action=be_ajax_load_more, post_type=publicatie, paged=N,
    query[pagename]=publications, query[lang]=en.
  - Internal Demos detail pages carry the full article text, post id, date,
    and a specific article PDF link. Direct PDF and external links are handled
    as best-effort records from the list metadata.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

# Absolute import - spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_START_URL = "https://nidi.nl/en/publications/"
_AJAX_URL = "https://nidi.nl/wp-admin/admin-ajax.php"
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = 25 * 60
_RETRY_WAITS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 100

_MONTHS = {
    "jan": 1,
    "january": 1,
    "januari": 1,
    "feb": 2,
    "february": 2,
    "februari": 2,
    "mar": 3,
    "march": 3,
    "maart": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mei": 5,
    "jun": 6,
    "june": 6,
    "juni": 6,
    "jul": 7,
    "july": 7,
    "juli": 7,
    "aug": 8,
    "august": 8,
    "augustus": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "okt": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = text.replace("\u2010", "-").replace("\u2011", "-")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;")


def _make_soup(raw: str | None):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[nidi-nl-en] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _strip_tags(html: str | None) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", html or ""))


def _parse_date(raw: str | None) -> str | None:
    text = _clean_text(raw)
    if not text:
        return None

    match = re.search(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    match = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)\d{2})\b", text)
    if match:
        return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"

    match = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+((?:19|20)\d{2})\b", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return f"{match.group(3)}-{month:02d}-{int(match.group(1)):02d}"

    match = re.search(r"\b([A-Za-zÀ-ÿ]+)\s+((?:19|20)\d{2})\b", text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            return f"{match.group(2)}-{month:02d}-01"

    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    if match:
        return f"{match.group(1)}-01-01"

    return None


def _date_only(value: str | None) -> str | None:
    if not value:
        return None
    match = re.match(r"((?:19|20)\d{2}-\d{2}-\d{2})", value.strip())
    if match:
        return match.group(1)
    return _parse_date(value)


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("filename", "file"):
        if qs.get(key) and qs[key][0]:
            return unquote(qs[key][0]).strip()[:240] or None
    tail = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    if tail and "." in tail:
        return tail[:240]
    return None


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", value, flags=re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))[:240] or None
    match = re.search(r'filename="?([^";\r\n]+)"?', value, flags=re.I)
    if match:
        return unquote(match.group(1).strip())[:240] or None
    return None


def _is_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    return urlparse(url).path.lower().endswith(".pdf")


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    tail = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    if tail:
        return tail
    digest = hashlib_fallback(url)
    return digest


def hashlib_fallback(value: str) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _doi_from_text(value: str | None) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.netloc.lower().endswith("doi.org") and parsed.path:
        return parsed.path.strip("/")
    match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", text, flags=re.I)
    if match:
        return match.group(1).rstrip(".,);")
    return None


def _dedupe_join(values: list[str], sep: str = "; ") -> str | None:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return sep.join(out) if out else None


def _authors_to_semicolon(raw: str | None) -> str | None:
    text = _clean_text(raw)
    if not text:
        return None
    text = re.sub(r"\s+\b(?:and|en)\b\s+", ", ", text, flags=re.I)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        return text

    authors: list[str] = []
    i = 0
    while i < len(parts):
        if i + 1 < len(parts):
            authors.append(f"{parts[i]}, {parts[i + 1]}")
            i += 2
        else:
            authors.append(parts[i])
            i += 1
    return _dedupe_join(authors)


def _category_from_link(link) -> str | None:
    classes = " ".join(link.get("class", [])) if link else ""
    match = re.search(r"publication-title__type-([A-Za-z0-9_-]+)", classes)
    return match.group(1) if match else None


def _extract_volume_issue(text: str | None) -> tuple[str | None, str | None]:
    value = _clean_text(text)
    if not value:
        return None, None
    match = re.search(r"\bjaargang\s+(\d{1,4})\s*,\s*nummer\s+(\d{1,4})\b", value, re.I)
    if match:
        return match.group(1), match.group(2)
    match = re.search(r"\b(\d{1,4})\s*\((\d{1,4})\)", value)
    if match:
        return match.group(1), match.group(2)
    match = re.search(r"\b(\d{1,4})\s*:\s*(?:\d|[A-Za-z])", value)
    if match:
        return match.group(1), None
    return None, None


def _publisher_from_reference(text: str | None) -> str | None:
    value = _clean_text(text)
    if not value:
        return None
    known = [
        "NIDI-KNAW",
        "NIDI",
        "University of Groningen",
        "Tilburg University",
        "Centraal Planbureau",
        "Oxford Academic",
        "Edward Elgar Publishing",
        "Routledge",
    ]
    found = [name for name in known if name.lower() in value.lower()]
    return _dedupe_join(found)


class NidiNlEnCrawler(BaseCrawler):
    site_id = "nidi-nl-en"
    site_name = "Custom: nidi-nl-en"
    base_url = "https://nidi.nl"

    def _curl(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        referer: str | None = None,
        headers_only: bool = False,
        max_time: int = 45,
    ) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(max_time),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9,nl;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if headers_only:
            cmd.append("-I")
        if method.upper() == "POST":
            cmd.extend(
                [
                    "-X",
                    "POST",
                    "-H",
                    "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                    "-H",
                    "X-Requested-With: XMLHttpRequest",
                ]
            )
            for key, value in (data or {}).items():
                cmd.extend(["--data-urlencode", f"{key}={value}"])
        cmd.append(url)

        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout:
                    return stdout
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: "
                    f"returncode={result.returncode} {stderr}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")
            if attempt < 3:
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts: {url}")
        return None

    def _fetch_list_page(self, page: int) -> tuple[list[dict], bool]:
        if page <= 1:
            raw = self._curl(_START_URL, referer=_START_URL)
            if not raw:
                return [], False
            soup = _make_soup(raw)
            if soup is None:
                return self._parse_list_fragment(raw, page), False
            container = soup.select_one("ul.related-list__publications") or soup
            has_next = bool(soup.select_one("a.js-load-more-posts"))
            return self._parse_list_soup(container, page), has_next

        payload = {
            "action": "be_ajax_load_more",
            "paged": str(page),
            "post_type": "publicatie",
            "query[page]": "",
            "query[pagename]": "publications",
            "query[lang]": "en",
        }
        raw = self._curl(_AJAX_URL, method="POST", data=payload, referer=_START_URL)
        if not raw:
            return [], False
        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] page {page}: AJAX JSON parse failed: {exc}")
            return self._parse_list_fragment(raw, page), False

        body = data.get("data") if isinstance(data, dict) else {}
        html = body.get("html", "") if isinstance(body, dict) else ""
        lastpage = str(body.get("lastpage", "0")).lower() in {"1", "true", "yes"}
        if not _clean_text(html):
            return [], False
        return self._parse_list_fragment(html, page), not lastpage

    def _parse_list_fragment(self, html: str, page: int) -> list[dict]:
        soup = _make_soup(f"<ul>{html}</ul>")
        if soup is None:
            return []
        return self._parse_list_soup(soup, page)

    def _parse_list_soup(self, soup, page: int) -> list[dict]:
        records: list[dict] = []
        for position, row in enumerate(soup.select("li.related-list--item"), start=1):
            link = row.select_one("a.publication-title")
            if not link:
                continue
            href = _clean_text(link.get("href"))
            if not href or href.lower().startswith("javascript:"):
                continue
            url = urljoin(self.base_url, href)
            full_text = _clean_text(link.get_text(" ", strip=True))

            match = re.search(r"^(?P<authors>.+?)\s*\((?P<year>(?:19|20)\d{2})\)\s*,?\s*(?P<rest>.*)$", full_text)
            authors_raw = match.group("authors") if match else None
            year_raw = match.group("year") if match else None

            indent = link.select_one("span.indent")
            title, journal, journal_raw = self._title_and_journal(indent, full_text)
            if not title and match:
                title = _clean_text(match.group("rest"))
            if not title:
                title = full_text[:180]

            category = _category_from_link(link)
            doi = _doi_from_text(url) or _doi_from_text(full_text)
            pdf_url = url if _is_pdf_url(url) else None
            original_filename = _filename_from_url(pdf_url)
            volume, issue = _extract_volume_issue(full_text)
            series = "Demos" if journal and "demos" in journal.lower() else None
            fallback_id = doi or original_filename or _slug_from_url(url) or hashlib_fallback(url)

            records.append(
                {
                    "detail_url": url,
                    "title": title,
                    "authors": _authors_to_semicolon(authors_raw),
                    "year_raw": year_raw,
                    "listed_date": _parse_date(year_raw),
                    "posted_date_raw": year_raw,
                    "reference": full_text,
                    "journal": journal,
                    "journal_raw": journal_raw,
                    "series": series,
                    "volume": volume,
                    "issue": issue,
                    "category": category,
                    "doi": doi,
                    "pdf_url": pdf_url,
                    "original_filename": original_filename,
                    "publisher": _publisher_from_reference(full_text),
                    "external_id": fallback_id,
                    "post_number": fallback_id,
                    "source_list_page": page,
                    "source_list_position": position,
                }
            )
        return records

    @staticmethod
    def _title_and_journal(indent, full_text: str) -> tuple[str | None, str | None, str | None]:
        if indent is None:
            return None, None, None

        ems = [_clean_text(em.get_text(" ", strip=True)) for em in indent.find_all("em")]
        first_meaningful = None
        for child in indent.children:
            if getattr(child, "name", None) is None:
                text = _clean_text(str(child))
                if text:
                    first_meaningful = "text"
                    break
            else:
                first_meaningful = child.name
                break

        html = indent.decode_contents()
        before_em = _strip_tags(html.split("<em", 1)[0]) if "<em" in html else _clean_text(indent.get_text(" ", strip=True))
        if before_em:
            title = before_em.rstrip(" .")
            journal = ems[0] if ems else None
        elif ems:
            title = ems[0].rstrip(" .")
            journal = ems[1] if len(ems) > 1 else None
        else:
            title = _clean_text(indent.get_text(" ", strip=True)).rstrip(" .")
            journal = None

        if first_meaningful == "em" and ems:
            title = ems[0].rstrip(" .")
            journal = ems[1] if len(ems) > 1 else None

        rest = full_text
        if title and title in rest:
            rest = rest.split(title, 1)[-1]
        return title or None, journal or None, _clean_text(rest) or None

    def _parse_detail(self, item: dict) -> dict | None:
        url = item.get("detail_url")
        if not url:
            return None
        host = urlparse(url).netloc.lower()
        if _is_pdf_url(url):
            return self._parse_pdf_record(item)
        if host.endswith("nidi.nl"):
            return self._parse_nidi_detail(item)
        return self._parse_external_detail(item)

    def _parse_pdf_record(self, item: dict) -> dict | None:
        url = item["detail_url"]
        headers = self._curl(url, referer=_START_URL, headers_only=True, max_time=30) or ""
        cd = None
        content_type = None
        for line in headers.splitlines():
            if line.lower().startswith("content-disposition:"):
                cd = line.split(":", 1)[1].strip()
            elif line.lower().startswith("content-type:"):
                content_type = line.split(":", 1)[1].strip()

        original_filename = (
            _filename_from_content_disposition(cd)
            or item.get("original_filename")
            or _filename_from_url(url)
        )
        abstract = self._fallback_abstract(item)
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return None

        metadata = self._base_metadata(item)
        metadata.update(
            {
                "originalFilename": original_filename,
                "content_disposition": cd,
                "content_type": content_type,
                "source_detail_endpoint": "direct PDF URL",
                "headers": headers[:2000],
            }
        )
        return self._paper_from_item(
            item,
            abstract=abstract,
            url=url,
            pdf_url=url,
            original_filename=original_filename,
            metadata=metadata,
        )

    def _parse_nidi_detail(self, item: dict) -> dict | None:
        url = item["detail_url"]
        raw = self._curl(url, referer=_START_URL)
        if not raw:
            return None
        soup = _make_soup(raw)
        if soup is None:
            return None

        title_el = soup.select_one("main h1") or soup.find("h1")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else item.get("title")
        abstract = self._extract_article_text(soup) or self._meta_content(
            soup,
            [
                ("meta", {"property": "og:description"}),
                ("meta", {"name": "description"}),
            ],
        )
        abstract = _clean_text(abstract)
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return None

        node_id = self._extract_node_id(soup, raw)
        canonical = self._canonical_url(soup) or url
        slug = _slug_from_url(canonical)
        info_text = _clean_text(soup.select_one(".article-info").get_text(" ", strip=True)) if soup.select_one(".article-info") else ""
        credits = _clean_text(soup.select_one(".infobar__credits").get_text(" ", strip=True)) if soup.select_one(".infobar__credits") else ""
        yoast_dates = self._yoast_dates(soup)
        credit_date_raw = self._date_from_credits_raw(credits)
        published_date = (
            _date_only(yoast_dates.get("datePublished"))
            or _parse_date(credit_date_raw)
            or item.get("listed_date")
        )
        listed_date = item.get("listed_date") or published_date

        pdf_url = self._extract_nidi_pdf(soup)
        original_filename = _filename_from_url(pdf_url)
        info_volume, info_issue = _extract_volume_issue(info_text or credits)
        volume = info_volume or item.get("volume")
        issue = info_issue or item.get("issue")
        series = item.get("series") or ("Demos" if "demos" in (info_text + credits).lower() else None)
        department = self._extract_department(soup)
        metadata = self._base_metadata(item)
        metadata.update(
            {
                "posted_date": item.get("posted_date_raw") or credit_date_raw or published_date,
                "listed_date": listed_date,
                "published_date_raw": credit_date_raw or yoast_dates.get("datePublished"),
                "originalFilename": original_filename,
                "journal_raw": info_text or item.get("journal_raw"),
                "series": series,
                "volume": volume,
                "issue": issue,
                "node_id": node_id,
                "post_id": node_id,
                "slug": slug,
                "canonical_url": canonical,
                "shortlink": self._shortlink(soup),
                "infobar_credits": credits,
                "article_info": info_text,
                "yoast": yoast_dates,
                "source_detail_endpoint": "/demos/{slug}/",
            }
        )

        external_id = node_id or item.get("external_id") or slug or hashlib_fallback(canonical)
        post_number = node_id if node_id and str(node_id).isdigit() else (slug or item.get("post_number"))
        return self._paper_from_item(
            item,
            abstract=abstract,
            title=title,
            url=canonical,
            pdf_url=pdf_url,
            original_filename=original_filename,
            published_date=published_date,
            listed_date=listed_date,
            external_id=external_id,
            post_number=post_number,
            publisher=item.get("publisher") or "NIDI-KNAW",
            department=department,
            series=series,
            volume=volume,
            issue=issue,
            metadata=metadata,
        )

    def _parse_external_detail(self, item: dict) -> dict | None:
        url = item["detail_url"]
        raw = self._curl(url, referer=_START_URL, max_time=30)
        soup = _make_soup(raw) if raw else None

        title = item.get("title")
        abstract = ""
        authors = item.get("authors")
        journal = item.get("journal")
        published_date = item.get("listed_date")
        pdf_url = item.get("pdf_url")
        original_filename = item.get("original_filename")

        if soup is not None:
            title = (
                self._meta_content(soup, [("meta", {"name": "citation_title"})])
                or self._meta_content(soup, [("meta", {"property": "og:title"})])
                or title
            )
            abstract = (
                self._meta_content(soup, [("meta", {"name": "citation_abstract"})])
                or self._meta_content(soup, [("meta", {"name": "dc.description"})])
                or self._meta_content(soup, [("meta", {"property": "og:description"})])
                or ""
            )
            citation_authors = [
                _clean_text(meta.get("content"))
                for meta in soup.find_all("meta", attrs={"name": "citation_author"})
                if _clean_text(meta.get("content"))
            ]
            if citation_authors:
                authors = _dedupe_join(citation_authors)
            journal = (
                self._meta_content(soup, [("meta", {"name": "citation_journal_title"})])
                or journal
            )
            published_date = (
                _parse_date(self._meta_content(soup, [("meta", {"name": "citation_publication_date"})]))
                or published_date
            )
            pdf_url = self._meta_content(soup, [("meta", {"name": "citation_pdf_url"})]) or pdf_url
            if not pdf_url:
                pdf_link = soup.find("a", href=re.compile(r"\.pdf(?:$|[?#])", re.I))
                if pdf_link:
                    pdf_url = urljoin(url, pdf_link.get("href", ""))
            original_filename = _filename_from_url(pdf_url) or original_filename

        abstract = _clean_text(abstract)
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return None

        metadata = self._base_metadata(item)
        metadata.update(
            {
                "originalFilename": original_filename,
                "journal_raw": journal or item.get("journal_raw"),
                "series": item.get("series"),
                "volume": item.get("volume"),
                "issue": item.get("issue"),
                "node_id": None,
                "source_detail_endpoint": "external HTML metadata",
                "external_host": urlparse(url).netloc,
            }
        )
        return self._paper_from_item(
            item,
            abstract=abstract,
            title=title,
            authors=authors,
            journal=journal,
            published_date=published_date,
            pdf_url=pdf_url,
            original_filename=original_filename,
            metadata=metadata,
        )

    @staticmethod
    def _meta_content(soup, selectors: list[tuple[str, dict]]) -> str | None:
        for name, attrs in selectors:
            tag = soup.find(name, attrs=attrs)
            if tag and tag.get("content"):
                value = _clean_text(tag.get("content"))
                if value:
                    return value
        return None

    @staticmethod
    def _extract_article_text(soup) -> str | None:
        article = soup.select_one("article.l-reading-plane.demos") or soup.select_one("article")
        if not article:
            return None
        parts: list[str] = []
        for child in article.children:
            name = getattr(child, "name", None)
            if not name:
                continue
            if name in {"h2", "h3"} and "reference" in _clean_text(child.get_text(" ", strip=True)).lower():
                break
            if name == "div" and "article-footer" in child.get("class", []):
                break
            if name == "div" and "article-intro" in child.get("class", []):
                parts.append(_clean_text(child.get_text(" ", strip=True)))
            elif name == "p":
                classes = set(child.get("class", []))
                if "has-background" in classes:
                    continue
                parts.append(_clean_text(child.get_text(" ", strip=True)))
        parts = [p for p in parts if p]
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def _extract_node_id(soup, raw: str) -> str | None:
        body = soup.find("body")
        if body:
            classes = " ".join(body.get("class", []))
            match = re.search(r"\bpostid-(\d+)\b", classes)
            if match:
                return match.group(1)
        match = re.search(r"shortlink[^>]+href=['\"]https://nidi\.nl/\?p=(\d+)['\"]", raw)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _canonical_url(soup) -> str | None:
        link = soup.find("link", attrs={"rel": "canonical"})
        return _clean_text(link.get("href")) if link and link.get("href") else None

    @staticmethod
    def _shortlink(soup) -> str | None:
        link = soup.find("link", attrs={"rel": "shortlink"})
        return _clean_text(link.get("href")) if link and link.get("href") else None

    @staticmethod
    def _yoast_dates(soup) -> dict:
        dates: dict[str, str] = {}
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            graph = data.get("@graph") if isinstance(data, dict) else None
            if not isinstance(graph, list):
                continue
            for node in graph:
                if not isinstance(node, dict):
                    continue
                if node.get("datePublished"):
                    dates["datePublished"] = node.get("datePublished")
                if node.get("dateModified"):
                    dates["dateModified"] = node.get("dateModified")
        return dates

    @staticmethod
    def _date_from_credits_raw(credits: str | None) -> str | None:
        text = _clean_text(credits)
        if not text:
            return None
        parts = [_clean_text(part) for part in text.split("|")]
        for part in parts:
            if _parse_date(part):
                return part
        return None

    def _extract_nidi_pdf(self, soup) -> str | None:
        selectors = [
            ".article-info a[href*='.pdf']",
            ".article-info a[href*='.PDF']",
            ".demos_curedition a[href*='.pdf']",
            "a[href*='.pdf']",
        ]
        for selector in selectors:
            link = soup.select_one(selector)
            if link and link.get("href"):
                return urljoin(self.base_url, link.get("href"))
        return None

    @staticmethod
    def _extract_department(soup) -> str | None:
        block = soup.select_one("article p.has-background")
        if not block:
            return None
        orgs: list[str] = []
        text = _clean_text(block.get_text(" ", strip=True))
        for org in ("NIDI-KNAW", "University of Groningen"):
            if org.lower() in text.lower():
                orgs.append(org)
        return _dedupe_join(orgs)

    def _fallback_abstract(self, item: dict) -> str:
        title = _clean_text(item.get("title"))
        reference = _clean_text(item.get("reference"))
        if reference and title and title not in reference:
            return _clean_text(f"{title}. {reference}")
        return reference or title

    def _base_metadata(self, item: dict) -> dict:
        return {
            "posted_date": item.get("posted_date_raw") or item.get("listed_date"),
            "listed_date": item.get("listed_date"),
            "originalFilename": item.get("original_filename"),
            "journal_raw": item.get("journal_raw"),
            "series": item.get("series"),
            "volume": item.get("volume"),
            "issue": item.get("issue"),
            "node_id": None,
            "post_type": "publicatie",
            "ajax_action": "be_ajax_load_more",
            "source_list_endpoint": _START_URL if item.get("source_list_page") == 1 else _AJAX_URL,
            "source_list_page": item.get("source_list_page"),
            "source_list_position": item.get("source_list_position"),
            "list_reference": item.get("reference"),
            "native_external_id": item.get("external_id"),
            "native_post_number": item.get("post_number"),
        }

    def _paper_from_item(
        self,
        item: dict,
        *,
        abstract: str,
        metadata: dict,
        title: str | None = None,
        authors: str | None = None,
        publisher: str | None = None,
        department: str | None = None,
        journal: str | None = None,
        url: str | None = None,
        pdf_url: str | None = None,
        original_filename: str | None = None,
        published_date: str | None = None,
        listed_date: str | None = None,
        external_id: str | None = None,
        post_number: str | None = None,
        series: str | None = None,
        volume: str | None = None,
        issue: str | None = None,
    ) -> dict:
        final_url = url or item.get("detail_url")
        final_pdf = pdf_url if pdf_url is not None else item.get("pdf_url")
        final_original_filename = original_filename or item.get("original_filename") or _filename_from_url(final_pdf)
        final_published_date = published_date or item.get("listed_date")
        final_listed_date = listed_date or item.get("listed_date") or final_published_date
        final_external_id = str(external_id or item.get("external_id") or hashlib_fallback(final_url or ""))
        final_post_number = str(post_number or item.get("post_number") or final_external_id)

        metadata.setdefault("posted_date", item.get("posted_date_raw") or final_listed_date)
        metadata.setdefault("originalFilename", final_original_filename)
        metadata.setdefault("journal_raw", item.get("journal_raw") or journal or item.get("journal"))
        metadata.setdefault("series", series or item.get("series"))
        metadata.setdefault("volume", volume or item.get("volume"))
        metadata.setdefault("issue", issue or item.get("issue"))

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": final_external_id,
            "post_number": final_post_number,
            "title": _clean_text(title or item.get("title")) or "(untitled)",
            "abstract": _clean_text(abstract),
            "published_date": final_published_date,
            "listed_date": final_listed_date,
            "posted_date": final_listed_date,
            "authors": authors or item.get("authors"),
            "publisher": publisher or item.get("publisher"),
            "department": department,
            "journal": journal or item.get("journal"),
            "url": final_url,
            "pdf_url": final_pdf,
            "keywords": None,
            "category": item.get("category"),
            "doi": item.get("doi") or _doi_from_text(final_url),
            "original_filename": final_original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        page = 1
        started = time.time()
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= _PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly.")
                break
            if page == _PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {_PAGE_CAP} pages reached.")

            items, has_next = self._fetch_list_page(page)
            if not items:
                print(f"[{self.site_id}] page {page}: no records found. Done.")
                break

            new_items: list[dict] = []
            for item in items:
                detail_url = item.get("detail_url")
                if not detail_url:
                    continue
                dedupe_key = detail_url.rstrip("/")
                if dedupe_key in seen_urls:
                    continue
                seen_urls.add(dedupe_key)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: all records already seen. Done.")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly.")
                    return saved

                detail_url = item.get("detail_url") or f"page {page} item {idx}"
                try:
                    time.sleep(self._delay)
                    paper = self._parse_detail(item)
                    if not paper:
                        print(f"[{self.site_id}] item {detail_url} failed: no detail data")
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {paper.get('title', '')[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[nidi-nl-en] page {page}: saved {saved}/{limit_or_inf}")

            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent or last page reached. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
