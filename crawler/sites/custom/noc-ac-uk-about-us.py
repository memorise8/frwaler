# -*- coding: utf-8 -*-
"""Crawler for NOC literature and brochures.

Starting URL:
https://noc.ac.uk/about-us/literature-brochures

The current Drupal page redirects to the canonical HTML list endpoint at
/who-we-are/our-purpose/literature-and-brochures. The document detail/resource
endpoints are the linked PDF files; item metadata is derived from the list
HTML, PDF response headers, and text extracted from each PDF.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_START_URL = "https://noc.ac.uk/about-us/literature-brochures"
_CANONICAL_LIST_URL = "https://www.noc.ac.uk/who-we-are/our-purpose/literature-and-brochures"
_WAITS = (1, 3, 9)
_SAFETY_PAGE_CAP = 200
_WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MAX_ABSTRACT_CHARS = 1800


class NocAcUkAboutUsCrawler(BaseCrawler):
    site_id = "noc-ac-uk-about-us"
    site_name = "Custom: noc-ac-uk-about-us"
    base_url = "https://noc.ac.uk"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        timeout: int = 90,
        accept: str = "*/*",
        log_label: str | None = None,
    ) -> bytes | None:
        label = log_label or url
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: en-GB,en;q=0.9",
        ]
        if method == "HEAD":
            cmd.append("-I")
        cmd.append(url)

        last_err = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 15,
                    check=False,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_err = str(exc)
            else:
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_err = f"exit={result.returncode} {stderr[:240]}"

            wait = _WAITS[attempt]
            print(
                f"[{self.site_id}] curl attempt {attempt + 1}/3 failed for "
                f"{label}: {last_err}; retry in {wait}s"
            )
            time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {label}: {last_err}")
        return None

    def _curl_text(self, url: str, *, timeout: int = 90, accept: str = "text/html,*/*;q=0.8") -> str | None:
        raw = self._curl_bytes(url, timeout=timeout, accept=accept)
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw: str | None) -> BeautifulSoup | None:
        if not raw:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
                continue
        return None

    @staticmethod
    def _clean(text: str | None) -> str:
        if not text:
            return ""
        text = unescape(text)
        text = text.replace("\x00", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _iso_date_from_http_date(raw: str | None) -> str | None:
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        return dt.date().isoformat()

    @staticmethod
    def _iso_date_from_text(raw: str | None) -> str | None:
        if not raw:
            return None
        m = re.search(r"\b(\d{4})[-/](0[1-9]|1[0-2])(?:[-/](0[1-9]|[12]\d|3[01]))?\b", raw)
        if m:
            day = m.group(3) or "01"
            return f"{m.group(1)}-{m.group(2)}-{day}"
        m = re.search(r"\b(20\d{2}|19\d{2})\b", raw)
        if m:
            return f"{m.group(1)}-01-01"
        return None

    @staticmethod
    def _filename_from_url(url: str) -> str | None:
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if tail and "." in tail and len(tail) <= 240:
            return tail
        return None

    @staticmethod
    def _slug_from_filename(filename: str | None, fallback_url: str) -> str:
        source = filename or unquote(urlparse(fallback_url).path.rstrip("/").split("/")[-1])
        source = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", source)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-").lower()
        if slug:
            return slug[:180]
        digest = hashlib.sha1(fallback_url.encode("utf-8")).hexdigest()
        return f"pdf-{digest}"

    @staticmethod
    def _parse_headers(raw_headers: bytes | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if not raw_headers:
            return headers
        text = raw_headers.decode("utf-8", errors="replace")
        # curl -I -L can include multiple response blocks; the last one is final.
        blocks = [b for b in re.split(r"\r?\n\r?\n", text) if b.strip()]
        final = blocks[-1] if blocks else text
        for line in final.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return headers

    @staticmethod
    def _filename_from_content_disposition(header_value: str | None) -> str | None:
        if not header_value:
            return None
        m = re.search(r"filename\*=UTF-8''([^;]+)", header_value, re.IGNORECASE)
        if m:
            return unquote(m.group(1).strip().strip('"'))
        m = re.search(r'filename="?([^";]+)"?', header_value, re.IGNORECASE)
        if m:
            return unquote(m.group(1).strip())
        return None

    def _extract_pdf_text(self, pdf_bytes: bytes, pdf_url: str) -> str:
        if not pdf_bytes:
            return ""
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"],
                input=pdf_bytes,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except FileNotFoundError:
            pass
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext failed for {pdf_url}: {exc}")

        try:
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(pdf_bytes))
            pages = []
            for page in reader.pages[:8]:
                pages.append(page.extract_text() or "")
            return "\n".join(pages)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{self.site_id}] pypdf fallback failed for {pdf_url}: {exc}")
            return ""

    def _make_abstract(self, text: str, title: str, list_description: str) -> str:
        clean_text = self._clean(text)
        if not clean_text:
            return self._clean(list_description)

        lowered = clean_text.lower()
        if lowered.startswith("2 contents ") and "executive summary" in lowered:
            idx = lowered.find("executive summary")
            clean_text = clean_text[idx:]

        if title:
            title_norm = self._clean(title).lower()
            if clean_text.lower().startswith(title_norm):
                clean_text = self._clean(clean_text[len(title_norm):])

        if len(clean_text) > _MAX_ABSTRACT_CHARS:
            clean_text = clean_text[:_MAX_ABSTRACT_CHARS].rsplit(" ", 1)[0].rstrip()
        return clean_text

    # ------------------------------------------------------------------
    # List/detail extraction
    # ------------------------------------------------------------------

    def _list_url(self, page_index: int) -> str:
        if page_index <= 0:
            return _CANONICAL_LIST_URL
        return f"{_CANONICAL_LIST_URL}?page={page_index}"

    def _extract_page_meta(self, soup: BeautifulSoup) -> dict[str, str | None]:
        def meta_value(*selectors: tuple[str, str]) -> str | None:
            for attr, value in selectors:
                tag = soup.find("meta", attrs={attr: value})
                if tag and tag.get("content"):
                    return self._clean(tag.get("content"))
            return None

        return {
            "node_id": None,
            "page_published_time": meta_value(("property", "article:published_time")),
            "page_modified_time": meta_value(("property", "article:modified_time")),
            "page_description": meta_value(("name", "description"), ("property", "og:description")),
            "canonical_url": None,
        }

    def _extract_list_items(self, soup: BeautifulSoup) -> tuple[list[dict], bool, dict]:
        page_meta = self._extract_page_meta(soup)
        article = soup.find("article", attrs={"data-history-node-id": True})
        if article:
            page_meta["node_id"] = article.get("data-history-node-id")
        canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
        if canonical and canonical.get("href"):
            page_meta["canonical_url"] = canonical.get("href")

        list_description = page_meta.get("page_description") or ""
        intro = soup.select_one(".paragraph--type--left-right-wysiwyg")
        if intro:
            intro_text = self._clean(intro.get_text(" ", strip=True))
            if intro_text:
                list_description = intro_text

        items: list[dict] = []
        seen: set[str] = set()

        featured = soup.select_one(".featured-download")
        if featured:
            link = featured.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.IGNORECASE))
            if link and link.get("href"):
                pdf_url = urljoin(_CANONICAL_LIST_URL, link.get("href"))
                title_tag = featured.find(["h1", "h2", "h3"])
                desc_tag = featured.select_one(".description")
                title = self._clean(title_tag.get_text(" ", strip=True) if title_tag else "")
                description = self._clean(desc_tag.get_text(" ", strip=True) if desc_tag else "")
                if pdf_url not in seen:
                    seen.add(pdf_url)
                    items.append(
                        {
                            "title": title or self._filename_from_url(pdf_url) or pdf_url,
                            "pdf_url": pdf_url,
                            "category": "Literature and Brochures",
                            "list_description": description or list_description,
                            "list_index": len(items) + 1,
                            "source": "featured-download",
                        }
                    )

        for node in soup.select(".document-item"):
            link = node.find("a", href=re.compile(r"\.pdf(?:$|\?)", re.IGNORECASE))
            if not link or not link.get("href"):
                continue
            pdf_url = urljoin(_CANONICAL_LIST_URL, link.get("href"))
            if pdf_url in seen:
                continue
            seen.add(pdf_url)
            title_tag = node.find(["h1", "h2", "h3"])
            term = node.select_one(".term")
            title = self._clean(title_tag.get_text(" ", strip=True) if title_tag else "")
            category = self._clean(term.get_text(" ", strip=True) if term else "") or "Literature and Brochures"
            items.append(
                {
                    "title": title or self._filename_from_url(pdf_url) or pdf_url,
                    "pdf_url": pdf_url,
                    "category": category,
                    "list_description": list_description,
                    "list_index": len(items) + 1,
                    "source": "document-item",
                }
            )

        has_next = bool(
            soup.select_one('a[rel="next"], .pager__item--next a, li.next a, a[aria-label*="Next"]')
        )
        return items, has_next, page_meta

    def _parse_pdf_detail(self, item: dict, page_meta: dict) -> dict | None:
        pdf_url = item["pdf_url"]
        header_bytes = self._curl_bytes(
            pdf_url,
            method="HEAD",
            timeout=45,
            accept="application/pdf,*/*;q=0.8",
            log_label=f"HEAD {pdf_url}",
        )
        headers = self._parse_headers(header_bytes)

        pdf_bytes = self._curl_bytes(
            pdf_url,
            timeout=150,
            accept="application/pdf,*/*;q=0.8",
            log_label=pdf_url,
        )
        if not pdf_bytes:
            return None

        title = self._clean(item.get("title"))
        original_filename = (
            self._filename_from_content_disposition(headers.get("content-disposition"))
            or self._filename_from_url(pdf_url)
        )
        external_id = self._slug_from_filename(original_filename, pdf_url)
        pdf_text = self._extract_pdf_text(pdf_bytes, pdf_url)
        abstract = self._make_abstract(pdf_text, title, item.get("list_description") or "")

        raw_last_modified = headers.get("last-modified")
        listed_date = self._iso_date_from_http_date(raw_last_modified)
        if not listed_date:
            listed_date = self._iso_date_from_text(urlparse(pdf_url).path)

        published_date = self._iso_date_from_text(title) or self._iso_date_from_text(original_filename)
        if not published_date:
            published_date = listed_date

        keywords = ", ".join(
            part
            for part in [
                "literature",
                "brochures",
                "National Oceanography Centre",
                re.sub(r"[^A-Za-z0-9 ]+", " ", title).strip(),
            ]
            if part
        )

        metadata = {
            "node_id": page_meta.get("node_id"),
            "posted_date": raw_last_modified,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": "Literature and Brochures",
            "volume": None,
            "issue": None,
            "source_start_url": _START_URL,
            "source_list_url": page_meta.get("canonical_url") or _CANONICAL_LIST_URL,
            "source_endpoint_type": "Drupal HTML list; linked PDF detail/resource",
            "detail_endpoint": pdf_url,
            "file_path": urlparse(pdf_url).path,
            "list_title": title,
            "list_category": item.get("category"),
            "list_index": item.get("list_index"),
            "list_source": item.get("source"),
            "page_published_time": page_meta.get("page_published_time"),
            "page_modified_time": page_meta.get("page_modified_time"),
            "last_modified": raw_last_modified,
            "etag": headers.get("etag"),
            "content_type": headers.get("content-type"),
            "content_length": headers.get("content-length") or len(pdf_bytes),
            "pdf_text_chars": len(self._clean(pdf_text)),
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, pdf_url)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": "National Oceanography Centre",
            "department": "National Oceanography Centre",
            "journal": None,
            "url": pdf_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": item.get("category") or "Literature and Brochures",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        page_index = 0
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page_index < _SAFETY_PAGE_CAP:
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time >= _WALL_CLOCK_BUDGET:
                print(f"[{self.site_id}] wall-clock budget reached; stopping cleanly")
                break

            page_number = page_index + 1
            if page_number == 1 or page_number % 10 == 0:
                print(f"[{self.site_id}] page {page_number}: saved {saved}/{limit_or_inf}")

            raw = self._curl_text(self._list_url(page_index))
            if raw is None:
                print(f"[{self.site_id}] page {page_number}: fetch failed; stopping")
                break

            soup = self._parse_html(raw)
            if soup is None:
                print(f"[{self.site_id}] page {page_number}: HTML parse failed; stopping")
                break

            items, has_next, page_meta = self._extract_list_items(soup)
            new_items = [item for item in items if item.get("pdf_url") not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page_number}: 0 new records; stopping")
                break

            for idx, item in enumerate(new_items, start=1):
                detail_url = item.get("pdf_url")
                if limit is not None and saved >= limit:
                    break
                if not detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                if time.time() - start_time >= _WALL_CLOCK_BUDGET:
                    print(f"[{self.site_id}] wall-clock budget reached during details; stopping cleanly")
                    return saved

                try:
                    paper = self._parse_pdf_detail(item, page_meta)
                    if paper is None:
                        print(f"[{self.site_id}] item {detail_url} failed: could not fetch or parse detail")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper.get('title', '')[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

            if not has_next:
                break

            page_index += 1

        if page_index >= _SAFETY_PAGE_CAP:
            print(f"[{self.site_id}] safety cap of {_SAFETY_PAGE_CAP} pages reached; stopping")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
