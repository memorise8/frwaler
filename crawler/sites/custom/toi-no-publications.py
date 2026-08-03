# -*- coding: utf-8 -*-
"""Crawler for TØI academic publications.

The TØI publication listing is rendered as HTML by CorePublish.  Academic
article rows link directly to DOI/publisher pages, so detail metadata is
resolved through DOI content negotiation first, then bibliographic APIs and
publisher HTML as fallbacks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from email.message import Message
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class ToiNoPublicationsCrawler(BaseCrawler):
    site_id = "toi-no-publications"
    site_name = "Custom: toi-no-publications"
    base_url = "https://www.toi.no"

    START_URL = (
        "https://www.toi.no/publications/category29.html?"
        "types=article_peer_review_scientific&area=&author=&searchstring="
    )
    PAGE_SIZE = 30
    MAX_PAGES = 200
    MAX_RUNTIME_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    MIN_ABSTRACT_CHARS = 100

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, headers: dict[str, str] | None = None,
              method: str = "GET", max_time: int = 30) -> tuple[str, str]:
        """Fetch a URL with curl and return (body, headers).

        CorePublish rejects plain curl, but accepts browser-like headers and
        an enabled cookie engine.  ``-b ""`` enables in-memory cookies without
        writing a cookie jar.
        """
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", str(max_time), "-b", "",
            "-A", self.USER_AGENT,
            "-H", "Accept-Language: en-US,en;q=0.9,nb;q=0.8",
            "-D", "-",
        ]
        request_headers = headers or {}
        has_accept = any(k.lower() == "accept" for k in request_headers)

        if method == "HEAD":
            cmd.append("-I")
        elif not has_accept:
            cmd.extend([
                "-H",
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8",
            ])
        for key, value in request_headers.items():
            cmd.extend(["-H", f"{key}: {value}"])
        cmd.append(url)

        last_exc: Exception | None = None
        for attempt, wait in enumerate((1, 3, 9), start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=max_time + 10
                )
                raw = result.stdout or b""
                text = raw.decode("utf-8", errors="replace")
                header_text, body = self._split_curl_headers(text)
                if result.returncode == 0 and body.strip():
                    return body, header_text
                if result.returncode == 0 and method == "HEAD":
                    return "", header_text
                stderr = (result.stderr or b"").decode("utf-8", errors="replace")
                raise RuntimeError(stderr.strip() or f"curl exit {result.returncode}")
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    print(
                        f"[{self.site_id}] network error for {url} "
                        f"(attempt {attempt}/3): {exc}; retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(
                        f"[{self.site_id}] network failed after 3 attempts for "
                        f"{url}: {exc}"
                    )
        if last_exc:
            raise last_exc
        raise RuntimeError(f"curl failed for {url}")

    @staticmethod
    def _split_curl_headers(raw: str) -> tuple[str, str]:
        """Split ``curl -D -`` output into final response headers and body."""
        marker = "\r\n\r\n" if "\r\n\r\n" in raw else "\n\n"
        parts = raw.split(marker)
        header_blocks: list[str] = []
        body_start = 0
        for idx, part in enumerate(parts):
            if part.startswith("HTTP/"):
                header_blocks.append(part)
                body_start = idx + 1
            elif header_blocks:
                body_start = idx
                break
        headers = header_blocks[-1] if header_blocks else ""
        body = marker.join(parts[body_start:]) if body_start < len(parts) else ""
        return headers, body

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _soup(raw: str) -> BeautifulSoup:
        last_exc: Exception | None = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                continue
        print(f"[toi-no-publications] BeautifulSoup failed: {last_exc}")
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = value.get_text(" ", strip=True) if hasattr(value, "get_text") else str(value or "")
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _strip_markup(cls, value: str | None) -> str:
        if not value:
            return ""
        text = cls._soup(value).get_text(" ", strip=True)
        return cls._clean_text(text)

    @staticmethod
    def _first(value: Any) -> str:
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return str(value or "")

    @staticmethod
    def _date_from_parts(parts_obj: Any) -> str | None:
        try:
            parts = parts_obj.get("date-parts", [[]])[0]
        except AttributeError:
            return None
        if not parts:
            return None
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        return f"{year:04d}-{month:02d}-{day:02d}"

    @classmethod
    def _date_from_crossref(cls, rec: dict[str, Any]) -> str | None:
        for key in (
            "published-online", "published-print", "published",
            "issued", "created",
        ):
            parsed = cls._date_from_parts(rec.get(key))
            if parsed:
                return parsed
        return None

    @staticmethod
    def _date_from_publication_text(text: str) -> str | None:
        month_map = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11",
            "december": "12",
        }
        m = re.search(
            r"\b(" + "|".join(month_map) + r")\s+((?:19|20)\d{2})\b",
            text or "",
            flags=re.I,
        )
        if m:
            return f"{m.group(2)}-{month_map[m.group(1).lower()]}-01"
        m = re.search(r"\b((?:19|20)\d{2})\b", text or "")
        if m:
            return f"{m.group(1)}-01-01"
        return None

    @staticmethod
    def _parse_publication_text(text: str) -> dict[str, str | None]:
        result: dict[str, str | None] = {
            "journal_raw": text or None,
            "journal": None,
            "volume": None,
            "issue": None,
            "page": None,
        }
        m = re.match(r"^(?P<journal>.*?),\s*(?P<year>(?:19|20)\d{2})(?:,\s*(?P<rest>.*))?$", text or "")
        if not m:
            return result
        result["journal"] = m.group("journal").strip() or None
        rest = (m.group("rest") or "").strip()
        if rest:
            volume_match = re.match(r"([^(:]+)", rest)
            if volume_match:
                result["volume"] = volume_match.group(1).strip() or None
            issue_match = re.search(r"\((.*?)\)", rest)
            if issue_match:
                result["issue"] = issue_match.group(1).strip() or None
            page_match = re.search(r":\s*([0-9ivxlcdmIVXLCDM]+(?:\s*-\s*[0-9ivxlcdmIVXLCDM]+)?)\s*$", rest)
            if page_match:
                result["page"] = page_match.group(1).replace(" ", "") or None
        return result

    @staticmethod
    def _extract_doi(*values: str | None) -> str | None:
        for value in values:
            if not value:
                continue
            decoded = unquote(value)
            m = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", decoded, flags=re.I)
            if m:
                doi = m.group(1).rstrip(".,;)")
                return doi
        return None

    @staticmethod
    def _post_number_from_url(url: str, doi: str | None) -> str | None:
        parsed = urlparse(url or "")
        m = re.search(r"article(\d+)", parsed.path)
        if m:
            return m.group(1)
        return doi or parsed.path.rstrip("/").rsplit("/", 1)[-1] or None

    @staticmethod
    def _authors_from_crossref(authors: Any) -> str | None:
        names: list[str] = []
        for author in authors or []:
            if not isinstance(author, dict):
                continue
            name = " ".join(
                p for p in (author.get("given"), author.get("family")) if p
            ).strip()
            if not name:
                name = author.get("name") or ""
            if name:
                names.append(name)
        return "; ".join(names) if names else None

    @staticmethod
    def _authors_from_openalex(authorships: Any) -> str | None:
        names: list[str] = []
        for authorship in authorships or []:
            author = (authorship or {}).get("author") or {}
            name = author.get("display_name")
            if name:
                names.append(str(name))
        return "; ".join(names) if names else None

    @staticmethod
    def _institutions_from_openalex(authorships: Any) -> str | None:
        institutions: list[str] = []
        for authorship in authorships or []:
            for inst in (authorship or {}).get("institutions") or []:
                name = inst.get("display_name")
                if name and name not in institutions:
                    institutions.append(str(name))
        return "; ".join(institutions) if institutions else None

    @staticmethod
    def _abstract_from_openalex(index: Any) -> str | None:
        if not isinstance(index, dict) or not index:
            return None
        words: list[str | None] = []
        for word, positions in index.items():
            for pos in positions or []:
                if not isinstance(pos, int):
                    continue
                while len(words) <= pos:
                    words.append(None)
                words[pos] = word
        text = " ".join(w for w in words if w)
        return text or None

    @staticmethod
    def _keywords_from_openalex(rec: dict[str, Any]) -> str | None:
        values: list[str] = []
        for key in ("keywords", "concepts", "topics"):
            for item in rec.get(key) or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("display_name") or item.get("keyword")
                if name and name not in values:
                    values.append(str(name))
        return ", ".join(values[:20]) if values else None

    @staticmethod
    def _pdf_from_openalex(rec: dict[str, Any]) -> str | None:
        for location_key in ("best_oa_location", "primary_location"):
            loc = rec.get(location_key) or {}
            pdf_url = loc.get("pdf_url")
            if pdf_url:
                return str(pdf_url)
        oa_url = (rec.get("open_access") or {}).get("oa_url")
        if oa_url and str(oa_url).lower().endswith(".pdf"):
            return str(oa_url)
        return None

    @staticmethod
    def _filename_from_content_disposition(headers: str) -> str | None:
        for line in (headers or "").splitlines():
            if not line.lower().startswith("content-disposition:"):
                continue
            msg = Message()
            msg["content-disposition"] = line.split(":", 1)[1].strip()
            filename = msg.get_filename()
            if filename:
                return unquote(filename)
            m = re.search(r"filename\*=UTF-8''([^;\s]+)", line, flags=re.I)
            if m:
                return unquote(m.group(1))
        return None

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        if "." in tail and len(tail) <= 200:
            return tail
        return None

    def _pdf_filename(self, pdf_url: str | None) -> str | None:
        filename = self._filename_from_url(pdf_url)
        if filename or not pdf_url:
            return filename
        try:
            _, headers = self._curl(pdf_url, method="HEAD", max_time=20)
            return self._filename_from_content_disposition(headers)
        except Exception as exc:
            print(f"[{self.site_id}] PDF filename lookup failed for {pdf_url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # List and detail extraction
    # ------------------------------------------------------------------

    def _list_url(self, offset: int) -> str:
        sep = "&" if "?" in self.START_URL else "?"
        return f"{self.START_URL}{sep}offset={offset}"

    def _parse_list(self, html: str, list_url: str, page: int, offset: int) -> tuple[list[dict[str, Any]], str | None]:
        soup = self._soup(html)
        rows = soup.select("table.report-search-result tbody tr")
        items: list[dict[str, Any]] = []
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            link = cells[0].find("a", href=True)
            if not link:
                continue
            title = self._clean_text(link)
            url = urljoin(self.base_url, link.get("href", ""))
            authors_raw = self._clean_text(cells[1])
            authors = "; ".join(
                part.strip() for part in re.split(r"\s*,\s*", authors_raw) if part.strip()
            )
            publication_raw = self._clean_text(cells[2])
            pub_meta = self._parse_publication_text(publication_raw)
            doi = self._extract_doi(url)
            post_number = self._post_number_from_url(url, doi)
            items.append({
                "title": title,
                "authors": authors,
                "url": url,
                "doi": doi,
                "post_number": post_number,
                "publication_raw": publication_raw,
                "journal": pub_meta.get("journal"),
                "journal_raw": pub_meta.get("journal_raw"),
                "volume": pub_meta.get("volume"),
                "issue": pub_meta.get("issue"),
                "page": pub_meta.get("page"),
                "listed_date": self._date_from_publication_text(publication_raw),
                "list_url": list_url,
                "list_page": page,
                "list_offset": offset,
            })

        next_href = None
        next_link = soup.select_one(".paging li.jump-forward a[href]")
        if next_link:
            next_href = urljoin(self.base_url, next_link.get("href", ""))
        return items, next_href

    def _fetch_crossref(self, doi: str) -> dict[str, Any] | None:
        headers = {"Accept": "application/vnd.citationstyles.csl+json"}
        body, _ = self._curl(f"https://doi.org/{quote(doi, safe='/')}", headers=headers)
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return data["message"]
        return data if isinstance(data, dict) and not data.get("error") else None

    def _fetch_openalex(self, doi: str) -> dict[str, Any] | None:
        body, _ = self._curl(
            f"https://api.openalex.org/works/doi:{quote(doi, safe='/:.')}",
            headers={"Accept": "application/json"},
        )
        data = json.loads(body)
        return data if isinstance(data, dict) and data.get("id") else None

    def _fetch_semantic_scholar(self, doi: str) -> dict[str, Any] | None:
        fields = (
            "title,abstract,year,venue,publicationDate,authors,url,"
            "externalIds,openAccessPdf"
        )
        body, _ = self._curl(
            "https://api.semanticscholar.org/graph/v1/paper/"
            f"DOI:{quote(doi, safe='/:.')}?fields={fields}",
            headers={"Accept": "application/json"},
        )
        data = json.loads(body)
        return data if isinstance(data, dict) and data.get("paperId") else None

    def _fetch_publisher_html(self, url: str) -> dict[str, Any]:
        body, _ = self._curl(url)
        soup = self._soup(body)
        meta: dict[str, Any] = {}
        for tag in soup.find_all("meta"):
            key = (tag.get("name") or tag.get("property") or "").strip().lower()
            val = (tag.get("content") or "").strip()
            if key and val:
                meta[key] = val

        descriptions = [
            meta.get("citation_abstract"),
            meta.get("dc.description"),
            meta.get("description"),
            meta.get("og:description"),
        ]
        title = meta.get("citation_title") or meta.get("og:title")
        pdf_url = meta.get("citation_pdf_url")
        published = (
            meta.get("citation_publication_date")
            or meta.get("article:published_time")
            or meta.get("dc.date")
        )
        return {
            "title": title,
            "abstract": next((self._clean_text(d) for d in descriptions if d), None),
            "published_date": (published or "")[:10] or None,
            "pdf_url": pdf_url,
            "meta": meta,
        }

    def _resolve_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "title": item.get("title"),
            "authors": item.get("authors"),
            "abstract": "",
            "published_date": item.get("listed_date"),
            "listed_date": item.get("listed_date"),
            "publisher": None,
            "department": None,
            "journal": item.get("journal"),
            "keywords": None,
            "doi": item.get("doi"),
            "pdf_url": None,
            "original_filename": None,
            "metadata": {},
        }

        crossref: dict[str, Any] | None = None
        openalex: dict[str, Any] | None = None
        semantic: dict[str, Any] | None = None
        publisher_html: dict[str, Any] | None = None

        doi = item.get("doi")
        if doi:
            try:
                crossref = self._fetch_crossref(doi)
            except Exception as exc:
                print(f"[{self.site_id}] DOI metadata failed for {doi}: {exc}")

            if crossref:
                detail["title"] = self._first(crossref.get("title")) or detail["title"]
                detail["abstract"] = self._strip_markup(crossref.get("abstract")) or detail["abstract"]
                detail["published_date"] = self._date_from_crossref(crossref) or detail["published_date"]
                detail["publisher"] = crossref.get("publisher") or detail["publisher"]
                detail["authors"] = self._authors_from_crossref(crossref.get("author")) or detail["authors"]
                detail["journal"] = (
                    self._first(crossref.get("container-title"))
                    or detail["journal"]
                )
                subjects = crossref.get("subject") or []
                if isinstance(subjects, list) and subjects:
                    detail["keywords"] = ", ".join(str(s) for s in subjects if s)
                for link in crossref.get("link") or []:
                    link_url = str(link.get("URL") or "")
                    ctype = str(link.get("content-type") or "").lower()
                    if "pdf" in ctype or link_url.lower().endswith(".pdf"):
                        detail["pdf_url"] = link_url
                        break

            if not detail.get("abstract") or not detail.get("pdf_url"):
                try:
                    openalex = self._fetch_openalex(doi)
                except Exception as exc:
                    print(f"[{self.site_id}] OpenAlex metadata failed for {doi}: {exc}")

            if openalex:
                detail["abstract"] = (
                    detail.get("abstract")
                    or self._abstract_from_openalex(openalex.get("abstract_inverted_index"))
                    or ""
                )
                detail["published_date"] = (
                    detail.get("published_date")
                    or openalex.get("publication_date")
                )
                detail["authors"] = (
                    detail.get("authors")
                    or self._authors_from_openalex(openalex.get("authorships"))
                )
                detail["department"] = self._institutions_from_openalex(openalex.get("authorships"))
                source = ((openalex.get("primary_location") or {}).get("source") or {})
                detail["journal"] = detail.get("journal") or source.get("display_name")
                detail["keywords"] = detail.get("keywords") or self._keywords_from_openalex(openalex)
                detail["pdf_url"] = detail.get("pdf_url") or self._pdf_from_openalex(openalex)

            if not detail.get("abstract"):
                try:
                    semantic = self._fetch_semantic_scholar(doi)
                except Exception as exc:
                    print(f"[{self.site_id}] Semantic Scholar metadata failed for {doi}: {exc}")

            if semantic:
                detail["abstract"] = detail.get("abstract") or semantic.get("abstract") or ""
                detail["published_date"] = (
                    detail.get("published_date")
                    or semantic.get("publicationDate")
                    or (f"{semantic.get('year')}-01-01" if semantic.get("year") else None)
                )
                authors = [
                    a.get("name") for a in semantic.get("authors") or []
                    if isinstance(a, dict) and a.get("name")
                ]
                if authors and not detail.get("authors"):
                    detail["authors"] = "; ".join(authors)
                detail["journal"] = detail.get("journal") or semantic.get("venue")
                pdf = (semantic.get("openAccessPdf") or {}).get("url")
                detail["pdf_url"] = detail.get("pdf_url") or pdf

        if not detail.get("abstract"):
            try:
                publisher_html = self._fetch_publisher_html(item.get("url", ""))
            except Exception as exc:
                print(f"[{self.site_id}] publisher detail failed for {item.get('url')}: {exc}")

        if publisher_html:
            detail["title"] = publisher_html.get("title") or detail.get("title")
            detail["abstract"] = publisher_html.get("abstract") or detail.get("abstract") or ""
            detail["published_date"] = (
                detail.get("published_date")
                or publisher_html.get("published_date")
            )
            detail["pdf_url"] = detail.get("pdf_url") or publisher_html.get("pdf_url")

        detail["listed_date"] = detail.get("listed_date") or detail.get("published_date")
        detail["original_filename"] = self._pdf_filename(detail.get("pdf_url"))

        metadata = {
            "posted_date": item.get("publication_raw"),
            "listed_date": detail.get("listed_date"),
            "originalFilename": detail.get("original_filename"),
            "journal_raw": item.get("journal_raw"),
            "series": None,
            "volume": (
                (crossref or {}).get("volume")
                or item.get("volume")
                or ((openalex or {}).get("biblio") or {}).get("volume")
            ),
            "issue": (
                (crossref or {}).get("issue")
                or item.get("issue")
                or ((openalex or {}).get("biblio") or {}).get("issue")
            ),
            "page": (crossref or {}).get("page") or item.get("page"),
            "article_number": (crossref or {}).get("article-number"),
            "doi": detail.get("doi"),
            "post_number": item.get("post_number"),
            "source_url": item.get("url"),
            "toi_list_url": item.get("list_url"),
            "toi_list_page": item.get("list_page"),
            "toi_list_offset": item.get("list_offset"),
            "publication_raw": item.get("publication_raw"),
            "crossref": crossref,
            "openalex_id": (openalex or {}).get("id"),
            "openalex_raw": openalex,
            "semantic_scholar_id": (semantic or {}).get("paperId"),
            "semantic_scholar_raw": semantic,
            "publisher_meta": (publisher_html or {}).get("meta") if publisher_html else None,
        }
        detail["metadata"] = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}
        return detail

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        start = time.monotonic()
        saved = 0
        page = 1
        offset = 0
        seen_urls: set[str] = set()
        next_url: str | None = self._list_url(offset)
        limit_or_inf = limit if limit is not None else "inf"

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start > self.MAX_RUNTIME_SECONDS - 60:
                print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                break
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            try:
                list_url = next_url or self._list_url(offset)
                html, _ = self._curl(list_url)
                items, parsed_next_url = self._parse_list(html, list_url, page, offset)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} failed: {exc}")
                break

            if not items:
                print(f"[{self.site_id}] no records on page {page}; stopping")
                break

            new_items = [item for item in items if item.get("url") not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page} has 0 new records; stopping")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start > self.MAX_RUNTIME_SECONDS - 60:
                    print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                    return saved

                item_label = item.get("post_number") or item.get("title") or item.get("url")
                try:
                    seen_urls.add(item["url"])
                    time.sleep(self._delay)
                    detail = self._resolve_detail(item)
                    abstract = self._clean_text(detail.get("abstract") or "")
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping short abstract "
                            f"({len(abstract)} chars): {item.get('title', '')[:80]}"
                        )
                        continue

                    external_id = item.get("post_number") or detail.get("doi") or item.get("url")
                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": item.get("post_number"),
                        "title": detail.get("title") or item.get("title"),
                        "abstract": abstract,
                        "published_date": detail.get("published_date") or item.get("listed_date"),
                        "listed_date": detail.get("listed_date") or item.get("listed_date"),
                        "posted_date": detail.get("listed_date") or item.get("listed_date"),
                        "authors": detail.get("authors") or item.get("authors"),
                        "publisher": detail.get("publisher"),
                        "department": detail.get("department"),
                        "journal": detail.get("journal") or item.get("journal"),
                        "url": item.get("url"),
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords"),
                        "category": "Academic articles",
                        "doi": detail.get("doi"),
                        "original_filename": detail.get("original_filename"),
                        "metadata": json.dumps(detail.get("metadata") or {}, ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                        f"{paper['title'][:80]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if not parsed_next_url:
                print(f"[{self.site_id}] next page link absent at page {page}; stopping")
                break
            next_url = parsed_next_url
            next_offset = parse_qs(urlparse(parsed_next_url).query).get("offset", [None])[0]
            try:
                offset = int(next_offset) if next_offset is not None else offset + self.PAGE_SIZE
            except ValueError:
                offset += self.PAGE_SIZE
            page += 1
        else:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
