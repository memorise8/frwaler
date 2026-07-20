# -*- coding: utf-8 -*-
"""Crawler for Transportministeriet news.

Starting URL: https://www.trm.dk/nyheder

The visible list is populated by an ASP.NET/Umbraco surface endpoint:
POST /surface/FilterListe/GetFilterlisteContent
with a CSRF token from the start page.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# Absolute import: this file is often loaded with spec_from_file_location.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402


_DA_MONTHS = {
    "jan": "01",
    "januar": "01",
    "feb": "02",
    "februar": "02",
    "mar": "03",
    "marts": "03",
    "apr": "04",
    "april": "04",
    "maj": "05",
    "jun": "06",
    "juni": "06",
    "jul": "07",
    "juli": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "sept": "09",
    "september": "09",
    "okt": "10",
    "oktober": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}

_PUBLISHER = "Transportministeriet"
_PARSERS = ("html5lib", "lxml", "html.parser")


def _make_soup(raw):
    """Parse malformed HTML with a tolerant fallback chain."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for parser in _PARSERS:
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception:
            continue
    return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _meta_content(soup, key: str) -> str:
    tag = soup.find("meta", attrs={"name": key})
    if tag and tag.get("content"):
        return _clean_text(tag.get("content"))
    tag = soup.find("meta", attrs={"property": key})
    if tag and tag.get("content"):
        return _clean_text(tag.get("content"))
    return ""


def _parse_date(raw: str | None) -> str | None:
    """Parse Danish dates and ISO-ish metadata into YYYY-MM-DD."""
    text = _clean_text(raw)
    if not text:
        return None

    iso = re.search(r"(\d{4})[-/.](\d{2})[-/.](\d{2})", text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"

    dotted = re.search(r"(\d{1,2})\.\s*([A-Za-zÆØÅæøå]+)\.?\s+(\d{4})", text)
    if dotted:
        day = int(dotted.group(1))
        month_key = dotted.group(2).lower().rstrip(".")
        month = _DA_MONTHS.get(month_key)
        if month:
            return f"{dotted.group(3)}-{month}-{day:02d}"

    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        name = Path(urlparse(url).path).name
    except Exception:
        return None
    name = unquote(name)
    if name and "." in name and len(name) <= 200:
        return name
    return None


class TrmDkNyhederCrawler(BaseCrawler):
    site_id = "trm-dk-nyheder"
    site_name = "Custom: trm-dk-nyheder"
    base_url = "https://www.trm.dk"

    _START_URL = "https://www.trm.dk/nyheder"
    _LIST_ENDPOINT = "https://www.trm.dk/surface/FilterListe/GetFilterlisteContent"
    _PAGE_ID = "1724"
    _PAGE_SIZE = 50
    _MAX_PAGES = 200
    _MAX_SECONDS = 25 * 60
    _CURL_TIMEOUT = 60
    _CURL_META_MARKER = "__TRM_DK_CURL_META__:"
    _BACKOFFS = (1, 3, 9)

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_text(
        self,
        url: str,
        *,
        context: str,
        method: str = "GET",
        payload: str | None = None,
        headers: list[str] | None = None,
        referer: str | None = None,
        cookie_jar: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Fetch text through curl with TLS cap, retries, and UTF-8 replacement."""
        request_headers = [
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language: da-DK,da;q=0.9,en-US;q=0.8,en;q=0.7",
            f"User-Agent: {self.USER_AGENT}",
        ]
        if headers:
            request_headers.extend(headers)
        if referer:
            request_headers.append(f"Referer: {referer}")

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(self._CURL_TIMEOUT),
        ]
        for header in request_headers:
            cmd.extend(["-H", header])
        if cookie_jar:
            cmd.extend(["-b", cookie_jar, "-c", cookie_jar])
        if method.upper() == "POST":
            cmd.extend(["-X", "POST"])
            if payload is not None:
                cmd.extend(["--data-binary", payload])
        cmd.extend(
            [
                "-w",
                "\n"
                + self._CURL_META_MARKER
                + "%{http_code}\t%{url_effective}\t%{content_type}",
                url,
            ]
        )

        last_error = ""
        for attempt, wait in enumerate(self._BACKOFFS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self._CURL_TIMEOUT + 10,
                    check=False,
                )
                text = (result.stdout or b"").decode("utf-8", errors="replace")
                body, status, effective_url = self._split_curl_output(text)
                stderr = (result.stderr or b"").decode("utf-8", errors="replace")

                if result.returncode == 0 and 200 <= status < 400 and body.strip():
                    return body, effective_url or url

                last_error = (
                    f"curl exit={result.returncode} http={status} "
                    f"stderr={stderr.strip()[:200]}"
                )
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self._BACKOFFS):
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None, None

    def _split_curl_output(self, text: str) -> tuple[str, int, str]:
        if self._CURL_META_MARKER not in text:
            return text, 0, ""
        body, meta = text.rsplit(self._CURL_META_MARKER, 1)
        parts = meta.strip().split("\t")
        try:
            status = int(parts[0])
        except (ValueError, IndexError):
            status = 0
        effective_url = parts[1] if len(parts) > 1 else ""
        return body.rstrip("\n"), status, effective_url

    def _fetch_start_context(self, cookie_jar: str) -> tuple[str | None, str | None]:
        raw, _ = self._curl_text(
            self._START_URL,
            context="start page",
            referer=self.base_url,
            cookie_jar=cookie_jar,
        )
        if not raw:
            return None, None
        soup = _make_soup(raw)
        if soup is None:
            return raw, None
        token_input = soup.find("input", attrs={"name": "__RequestVerificationToken"})
        token = (token_input.get("value") or "").strip() if token_input else None
        return raw, token

    def _fetch_list_page(
        self,
        page: int,
        token: str,
        cookie_jar: str,
    ) -> tuple[str | None, str | None]:
        payload = json.dumps(
            {
                "gridView": "",
                "imageCrop": "",
                "currentPageType": "listPage",
                "hideImage": False,
                "hideDescription": False,
                "hideDate": False,
                "dateFormat": "dd. MMM yyyy",
                "alignImageLeft": False,
                "lang": "da",
                "pagesize": str(self._PAGE_SIZE),
                "page": page,
                "currentPageId": self._PAGE_ID,
                "parentPageId": self._PAGE_ID,
                "tags": "",
                "defaultTags": "",
                "startLetter": "",
                "query": "",
                "loadMore": page > 1,
                "year": "",
                "month": "",
                "customTags": "",
                "dontSortByDate": False,
                "sortBy": "",
                "hasCustomTags": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._curl_text(
            self._LIST_ENDPOINT,
            context=f"list page {page}",
            method="POST",
            payload=payload,
            headers=["Content-Type: application/json", f"X-CSRF-Token: {token}"],
            referer=self._START_URL,
            cookie_jar=cookie_jar,
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str, effective_url: str | None) -> tuple[list[dict], bool]:
        soup = _make_soup(raw)
        if soup is None:
            return [], True

        count_node = soup.select_one("#count-data")
        total_count = count_node.get("data-count") if count_node else None
        records: list[dict] = []
        is_last = False

        for card in soup.select(".card.card__list"):
            if (card.get("data-islast") or "").lower() == "true":
                is_last = True

            link = card.find("a", href=True)
            if not link:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            detail_url = urljoin(self.base_url, href)

            title_node = card.find(["h2", "h3"])
            title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "")
            if not title:
                continue

            label_node = card.select_one(".label span")
            listed_raw = _clean_text(label_node.get_text(" ", strip=True) if label_node else "")
            listed_date = _parse_date(listed_raw)

            text_node = card.select_one(".card__list__content p") or card.find("p")
            list_abstract = _clean_text(
                text_node.get_text(" ", strip=True) if text_node else ""
            )
            tags = [_clean_text(tag.get_text(" ", strip=True)) for tag in card.select(".tag")]
            tags = [tag for tag in tags if tag]

            image_node = card.find("img")
            image_url = ""
            image_alt = ""
            if image_node:
                image_url = image_node.get("data-src") or image_node.get("src") or ""
                image_url = urljoin(self.base_url, image_url) if image_url else ""
                image_alt = _clean_text(image_node.get("alt") or "")

            slug = urlparse(detail_url).path.rstrip("/").split("/")[-1]
            records.append(
                {
                    "url": detail_url,
                    "title": title,
                    "listed_date": listed_date,
                    "listed_date_raw": listed_raw,
                    "list_abstract": list_abstract,
                    "tags": tags,
                    "slug": slug,
                    "image_url": image_url,
                    "image_alt": image_alt,
                    "total_count": total_count,
                    "list_endpoint": effective_url or self._LIST_ENDPOINT,
                }
            )

        return records, is_last

    def _parse_detail_page(
        self,
        raw: str,
        record: dict,
        effective_url: str | None,
    ) -> dict:
        soup = _make_soup(raw)
        if soup is None:
            raise RuntimeError("detail HTML could not be parsed")

        page_id = _meta_content(soup, "pageId")
        page_date_raw = _meta_content(soup, "page_date")
        og_page_date_raw = _meta_content(soup, "og:pageDate")
        og_updated_raw = _meta_content(soup, "og:updated_time")
        canonical_url = _meta_content(soup, "og:url") or effective_url or record["url"]
        title = _meta_content(soup, "og:title") or record.get("title") or ""

        h1 = soup.select_one(".hero__article__text h1") or soup.find("h1")
        if h1:
            title = _clean_text(h1.get_text(" ", strip=True)) or title

        lead_node = soup.select_one(".hero__article__text p")
        lead = _clean_text(lead_node.get_text(" ", strip=True) if lead_node else "")
        meta_description = _meta_content(soup, "description") or _meta_content(
            soup, "og:description"
        )
        if len(lead) < 50:
            lead = meta_description or record.get("list_abstract") or lead

        content_node = soup.select_one("#page-content .rich-text")
        body = ""
        if content_node:
            paragraphs = [
                _clean_text(node.get_text(" ", strip=True))
                for node in content_node.find_all(["p", "li", "h2", "h3"])
            ]
            paragraphs = [p for p in paragraphs if p]
            body = "\n\n".join(paragraphs) if paragraphs else _clean_text(
                content_node.get_text(" ", strip=True)
            )

        abstract_parts = []
        for value in (lead, body, meta_description, record.get("list_abstract")):
            value = _clean_text(value)
            if value and value not in abstract_parts:
                abstract_parts.append(value)
        abstract = "\n\n".join(abstract_parts)

        date_label = soup.select_one(".hero__article__text .label span")
        detail_date_raw = _clean_text(date_label.get_text(" ", strip=True) if date_label else "")
        published_date = (
            _parse_date(page_date_raw)
            or _parse_date(og_page_date_raw)
            or _parse_date(detail_date_raw)
            or record.get("listed_date")
        )

        tag_values = [
            _clean_text(tag.get_text(" ", strip=True))
            for tag in soup.select(".hero__article__text .tag")
        ]
        tag_values.extend(record.get("tags") or [])
        keywords_meta = _meta_content(soup, "keywords")
        if keywords_meta:
            tag_values.extend(part.strip() for part in keywords_meta.split(","))
        tags = []
        for tag in tag_values:
            if tag and tag not in tags:
                tags.append(tag)

        pdf_url = None
        original_filename = None
        attachments = []
        for link in soup.select('a[href]'):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            href_l = href.lower()
            text = _clean_text(link.get_text(" ", strip=True))
            if ".pdf" not in href_l and "pdf" not in text.lower():
                continue
            resolved = urljoin(self.base_url, href)
            filename = _filename_from_url(resolved)
            attachments.append({"url": resolved, "text": text, "filename": filename})
            if pdf_url is None:
                pdf_url = resolved
                original_filename = filename

        image_url = _meta_content(soup, "og:image") or record.get("image_url") or ""
        breadcrumb = _meta_content(soup, "page_breadcrumb")

        external_id = page_id or record.get("slug") or canonical_url
        post_number = page_id if page_id and page_id.isdigit() else (record.get("slug") or None)
        category = "; ".join(tags) if tags else "Nyhed"

        metadata = {
            "posted_date": record.get("listed_date_raw") or detail_date_raw or None,
            "listed_date": record.get("listed_date"),
            "listed_date_raw": record.get("listed_date_raw"),
            "originalFilename": original_filename,
            "page_id": page_id or None,
            "node_id": page_id or None,
            "post_number": post_number,
            "slug": record.get("slug"),
            "page_date_raw": page_date_raw or None,
            "detail_date_raw": detail_date_raw or None,
            "og_pageDate": og_page_date_raw or None,
            "og_updated_time": og_updated_raw or None,
            "page_breadcrumb": breadcrumb or None,
            "tags": tags,
            "list_record": record,
            "attachments": attachments,
            "image_url": image_url or None,
            "image_alt": record.get("image_alt") or None,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

        return {
            "external_id": str(external_id),
            "post_number": str(post_number) if post_number else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": record.get("listed_date"),
            "listed_date_raw": record.get("listed_date_raw"),
            "publisher": _PUBLISHER,
            "authors": "",
            "department": "",
            "journal": "",
            "url": canonical_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(tags) if tags else "",
            "category": category,
            "doi": "",
            "original_filename": original_filename,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        item_index = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        with tempfile.NamedTemporaryFile(prefix="trm_dk_cookies_", suffix=".txt") as cookies:
            _, token = self._fetch_start_context(cookies.name)
            if not token:
                print(f"[{self.site_id}] CSRF token not found on start page")
                return 0

            while True:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= self._MAX_SECONDS:
                    print(
                        f"[{self.site_id}] 25-minute budget reached at page {page}; "
                        f"saved {saved}/{limit_or_inf}. Exiting cleanly."
                    )
                    break
                if page > self._MAX_PAGES:
                    print(
                        f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached; "
                        f"saved {saved}/{limit_or_inf}."
                    )
                    break
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                raw, effective_list_url = self._fetch_list_page(page, token, cookies.name)
                if not raw:
                    print(f"[{self.site_id}] list page {page} failed. Stopping.")
                    break

                records, is_last = self._parse_list_page(raw, effective_list_url)
                if not records:
                    print(f"[{self.site_id}] page {page} returned 0 records. Done.")
                    break

                new_records = []
                for record in records:
                    url = record.get("url")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_records.append(record)

                if not new_records:
                    print(
                        f"[{self.site_id}] page {page} had no new URLs "
                        "(pagination loop detected). Stopping."
                    )
                    break

                for record in new_records:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - start_time >= self._MAX_SECONDS:
                        print(
                            f"[{self.site_id}] 25-minute budget reached during page {page}; "
                            f"saved {saved}/{limit_or_inf}. Exiting cleanly."
                        )
                        return saved

                    item_index += 1
                    try:
                        time.sleep(self.detail_delay)
                        detail_raw, effective_detail_url = self._curl_text(
                            record["url"],
                            context=f"item {item_index} detail",
                            referer=effective_list_url or self._START_URL,
                            cookie_jar=cookies.name,
                        )
                        if not detail_raw:
                            print(
                                f"[{self.site_id}] item {item_index} skipped: "
                                "detail fetch failed"
                            )
                            continue

                        detail = self._parse_detail_page(
                            detail_raw,
                            record,
                            effective_detail_url,
                        )
                        abstract = detail.get("abstract") or ""
                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] item {item_index} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        external_id = detail["external_id"]
                        paper = {
                            "id": f"{self.site_id}:{external_id}",
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": detail.get("post_number"),
                            "title": detail.get("title") or record.get("title") or "",
                            "abstract": abstract,
                            "published_date": detail.get("published_date"),
                            "posted_date": detail.get("listed_date"),
                            "listed_date": detail.get("listed_date"),
                            "authors": detail.get("authors") or "",
                            "publisher": detail.get("publisher") or _PUBLISHER,
                            "department": detail.get("department") or "",
                            "journal": detail.get("journal") or "",
                            "url": detail.get("url") or record["url"],
                            "pdf_url": detail.get("pdf_url"),
                            "keywords": detail.get("keywords") or "",
                            "category": detail.get("category") or "",
                            "doi": detail.get("doi") or "",
                            "original_filename": detail.get("original_filename"),
                            "metadata": json.dumps(
                                detail.get("metadata") or {},
                                ensure_ascii=False,
                            ),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{self.site_id}] Saved {saved}/{limit_or_inf}: "
                            f"{paper['title'][:80]}"
                        )
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_index} failed: {exc}")
                        continue

                if is_last:
                    print(f"[{self.site_id}] page {page} marked as last page. Done.")
                    break

                page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
