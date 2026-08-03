# -*- coding: utf-8 -*-
"""Crawler for culture.gouv.fr Documentation publications.

Verified endpoints:
  List:   https://www.culture.gouv.fr/espace-documentation?resource_type%5B0%5D=50
          with page=N for subsequent pages.
  Detail: absolute culture.gouv.fr publication URLs exposed by the list cards.

The site is Ibexa/DSFR-rendered HTML. The list page exposes title, URL, and
listed date; detail pages expose a numeric locationId, lead text, body text,
tags, publisher/producer taxonomy, and PDF download cards.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - runtime environment should provide it.
    BeautifulSoup = None


class CultureGouvFrEspaceDocumentationCrawler(BaseCrawler):
    site_id = "culture-gouv-fr-espace-documentation"
    site_name = "Custom: culture-gouv-fr-espace-documentation"
    base_url = "https://www.culture.gouv.fr"

    START_URL = (
        "https://www.culture.gouv.fr/espace-documentation"
        "?resource_type%5B0%5D=50"
    )
    MAX_PAGES = 200
    CURL_TIMEOUT = 60
    DETAIL_SLEEP = 1.0
    WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    WALL_CLOCK_GRACE_SECONDS = 30
    MIN_ABSTRACT_CHARS = 50

    _MONTHS_FR = {
        "janvier": "01",
        "fevrier": "02",
        "mars": "03",
        "avril": "04",
        "mai": "05",
        "juin": "06",
        "juillet": "07",
        "aout": "08",
        "septembre": "09",
        "octobre": "10",
        "novembre": "11",
        "decembre": "12",
    }

    def __init__(self, db_conn, delay=1.0, detail_sleep=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_sleep = self.DETAIL_SLEEP if detail_sleep is None else detail_sleep

    # ------------------------------------------------------------------
    # Network and HTML helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, referer: str | None = None) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "20",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            "-H",
            "Accept-Encoding: gzip, deflate, br",
            "-H",
            "Connection: keep-alive",
            "-w",
            "\n__CULTURE_HTTP_STATUS__:%{http_code}",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                    check=False,
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                body, status = self._split_curl_status(raw)
                if result.returncode == 0 and status and 200 <= status < 400 and body.strip():
                    return body
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} status={status} {stderr[:180]}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 3:
                wait = waits[attempt - 1]
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _split_curl_status(raw: str) -> tuple[str, int | None]:
        marker = "\n__CULTURE_HTTP_STATUS__:"
        if marker not in raw:
            return raw, None
        body, status_raw = raw.rsplit(marker, 1)
        try:
            return body, int(status_raw.strip()[:3])
        except ValueError:
            return body, None

    def _make_soup(self, raw: str | bytes | None, context: str):
        if BeautifulSoup is None:
            print(f"[{self.site_id}] beautifulsoup4 is unavailable for {context}")
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    @staticmethod
    def _clean_text(value) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            try:
                value = value.get_text(" ", strip=True)
            except Exception:
                value = str(value)
        value = unescape(value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    @staticmethod
    def _strip_accents(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value or "")
        return "".join(ch for ch in decomposed if not unicodedata.combining(ch))

    @staticmethod
    def _strip_site_suffix(title: str) -> str:
        parts = title.rsplit("|", 1)
        if len(parts) == 2:
            suffix = unicodedata.normalize("NFKD", parts[1])
            suffix = "".join(ch for ch in suffix if not unicodedata.combining(ch))
            if suffix.strip().casefold() == "ministere de la culture":
                return parts[0].strip()
        return title.strip()

    @staticmethod
    def _dedupe(values) -> list[str]:
        seen = set()
        out = []
        for value in values or []:
            cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
        return out

    # ------------------------------------------------------------------
    # Date and URL helpers
    # ------------------------------------------------------------------

    def _parse_french_date(self, raw: str | None) -> str | None:
        text = self._strip_accents(self._clean_text(raw).lower())
        if not text:
            return None
        m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text)
        if m:
            day, month_name, year = m.groups()
            month = self._MONTHS_FR.get(month_name)
            if month:
                return f"{year}-{month}-{day.zfill(2)}"
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None

    @staticmethod
    def _date_only(value: str | None) -> str | None:
        if not value:
            return None
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None

    @staticmethod
    def _slug_from_url(url: str | None) -> str | None:
        if not url:
            return None
        path = urlparse(url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1] if path else ""
        return unquote(slug) or None

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if tail and "." in tail and len(tail) <= 255:
            return tail
        return None

    @staticmethod
    def _content_download_id(url: str | None) -> str | None:
        if not url:
            return None
        m = re.search(r"/content/download/(\d+)/", url)
        return m.group(1) if m else None

    def _build_list_url(self, page: int) -> str:
        if page <= 1:
            return self.START_URL
        return f"{self.base_url}/espace-documentation?resource_type%5B0%5D=50&page={page}"

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str, page_url: str) -> tuple[list[dict], bool]:
        soup = self._make_soup(html, f"list page {page_url}")
        if soup is None:
            return [], False

        items = []
        for card in soup.select("div.fr-card[data-internalsearch]"):
            try:
                parsed = self._parse_card(card, page_url)
                if parsed:
                    items.append(parsed)
            except Exception as exc:
                print(f"[{self.site_id}] list card parse failed: {exc}")
                continue

        return items, self._has_next_page(soup)

    def _parse_card(self, card, page_url: str) -> dict | None:
        link = card.select_one(".fr-card__title a[href]") or card.find("a", href=True)
        if link is None:
            return None
        href = (link.get("href") or "").strip()
        if not href:
            return None
        url = urljoin(self.base_url, href)
        title = self._clean_text(link)
        if not title:
            return None

        time_el = card.find("time")
        listed_date_raw = self._clean_text(time_el)
        listed_date = None
        if time_el is not None:
            listed_date = self._date_only(time_el.get("datetime")) or self._parse_french_date(listed_date_raw)

        data_raw = card.get("data-internalsearch") or ""
        data_internalsearch = {}
        if data_raw:
            try:
                data_internalsearch = json.loads(unescape(data_raw))
            except Exception:
                data_internalsearch = {"raw": data_raw}

        return {
            "title": title,
            "url": url,
            "slug": self._slug_from_url(url),
            "listed_date": listed_date,
            "listed_date_raw": listed_date_raw,
            "list_page_url": page_url,
            "data_internalsearch": data_internalsearch,
        }

    def _has_next_page(self, soup) -> bool:
        for link in soup.find_all("a", href=True):
            classes = link.get("class") or []
            title = self._clean_text(link.get("title")).lower()
            if "fr-pagination__link" not in classes:
                continue
            if "fr-icon-arrow-right-line" not in classes and "page suivante" not in title:
                continue
            if (link.get("aria-disabled") or "").lower() == "true":
                continue
            return True
        return False

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str) -> dict:
        soup = self._make_soup(html, f"detail {url}")
        if soup is None:
            return {}

        meta_tags = self._extract_meta_tags(soup)
        datalayer = self._extract_mc_datalayer(html)
        tags = self._extract_tags(soup)
        downloads = self._extract_downloads(soup)
        primary_download = self._pick_primary_download(downloads)

        title = self._clean_text(soup.select_one("h1.intro-title"))
        if not title:
            title = self._meta_content(soup, "og:title") or self._meta_content(soup, "twitter:title") or ""
        title = self._strip_site_suffix(self._clean_text(title))

        lead = self._clean_text(soup.select_one(".intro .fr-text--lead"))
        meta_description = (
            self._meta_content(soup, "description")
            or self._meta_content(soup, "og:description")
            or self._meta_content(soup, "twitter:description")
            or ""
        )
        article_text = self._extract_article_text(soup)

        date_el = soup.select_one("p.intro-date time[datetime]") or soup.select_one(".intro time[datetime]")
        detail_date_raw = self._clean_text(date_el.parent if date_el is not None else "")
        published_date = None
        if date_el is not None:
            published_date = self._date_only(date_el.get("datetime")) or self._parse_french_date(self._clean_text(date_el))

        location_id = self._extract_location_id(html)
        canonical = self._meta_content(soup, "url") or self._meta_content(soup, "og:url") or url
        canonical = urljoin(self.base_url, canonical)

        producers = [tag["label"] for tag in tags if tag.get("taxonomy") == "taxonomy_producer"]
        collections = [tag["label"] for tag in tags if tag.get("taxonomy") == "taxonomy_collection"]
        resource_types = [tag["label"] for tag in tags if tag.get("taxonomy") == "taxonomy_resource_type"]
        content_types = [tag["label"] for tag in tags if tag.get("taxonomy") == "content_type_name"]

        journal_raw, volume, issue = self._extract_journal_fields(title, collections)

        return {
            "title": title,
            "lead": lead,
            "meta_description": self._clean_text(meta_description),
            "article_text": article_text,
            "abstract": self._build_abstract(lead, article_text, meta_description),
            "published_date": published_date,
            "detail_date_raw": detail_date_raw,
            "location_id": location_id,
            "canonical": canonical,
            "slug": self._slug_from_url(canonical),
            "tags": tags,
            "keywords": self._keyword_labels(tags),
            "category": (
                resource_types[0]
                if resource_types
                else (content_types[0] if content_types else "Publications")
            ),
            "publisher": "; ".join(self._dedupe(producers)) or "Ministere de la Culture",
            "department": self._clean_text(datalayer.get("pagegroup")) or self._clean_text(datalayer.get("mini_site")),
            "collection": collections[0] if collections else None,
            "journal": journal_raw,
            "journal_raw": journal_raw,
            "series": collections[0] if collections else None,
            "volume": volume,
            "issue": issue,
            "downloads": downloads,
            "pdf_url": primary_download.get("url") if primary_download else None,
            "original_filename": primary_download.get("originalFilename") if primary_download else None,
            "download_attr": primary_download.get("download") if primary_download else None,
            "content_download_id": primary_download.get("content_download_id") if primary_download else None,
            "mc_datalayer": datalayer,
            "meta_tags": meta_tags,
        }

    def _extract_location_id(self, html: str) -> str | None:
        for pattern in (
            r"\blocationId\s*=\s*(\d+)",
            r"/switchToLanguage/(\d+)/",
        ):
            m = re.search(pattern, html)
            if m:
                return m.group(1)
        return None

    def _extract_mc_datalayer(self, html: str) -> dict:
        m = re.search(r"window\.MC_datalayer\.params\s*=\s*(\{.*?\});", html, re.S)
        if not m:
            return {}
        try:
            parsed = json.loads(m.group(1))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _extract_tags(self, soup) -> list[dict]:
        tags = []
        for link in soup.select("ul.fr-tags-group a.fr-tag[href]"):
            label = self._clean_text(link)
            href = link.get("href") or ""
            if not label:
                continue
            tags.append({
                "label": label,
                "href": href,
                "taxonomy": self._taxonomy_from_href(href),
            })
        return tags

    @staticmethod
    def _taxonomy_from_href(href: str) -> str | None:
        decoded = unquote(href or "")
        if "refinementList[content_type_name]" in decoded:
            return "content_type_name"
        m = re.search(r"refinementList\[(taxonomy_[a-z_]+)_id\]", decoded)
        if m:
            return m.group(1)
        return None

    def _keyword_labels(self, tags: list[dict]) -> list[str]:
        excluded = {"content_type_name", "taxonomy_resource_type", "taxonomy_producer"}
        return self._dedupe(
            tag["label"] for tag in tags if tag.get("taxonomy") not in excluded
        )

    def _extract_downloads(self, soup) -> list[dict]:
        candidates = list(soup.select(".fr-card--document a[href]"))
        if not candidates:
            candidates = [
                link for link in soup.find_all("a", href=True)
                if self._looks_like_pdf_url(link.get("href"))
            ]

        downloads = []
        seen = set()
        for link in candidates:
            href = (link.get("href") or "").strip()
            if not href:
                continue
            full_url = urljoin(self.base_url, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            title = self._clean_text(link)
            detail = self._clean_text(link.find(class_=lambda c: c and "fr-card__detail" in c))
            filename = self._filename_from_url(full_url) or link.get("download")
            downloads.append({
                "url": full_url,
                "title": title,
                "detail": detail,
                "download": link.get("download"),
                "originalFilename": filename,
                "content_download_id": self._content_download_id(full_url),
            })
        return downloads

    @staticmethod
    def _looks_like_pdf_url(href: str | None) -> bool:
        if not href:
            return False
        lowered = href.lower()
        return ".pdf" in lowered or "/pdf_file/" in lowered

    def _pick_primary_download(self, downloads: list[dict]) -> dict | None:
        for download in downloads:
            if self._looks_like_pdf_url(download.get("url")):
                return download
        return downloads[0] if downloads else None

    def _extract_article_text(self, soup) -> str:
        field = soup.select_one("article.main-article .ezrichtext-field")
        if field is None:
            field = soup.select_one("article.main-article")
        if field is None:
            return ""
        return self._clean_text(field)

    def _build_abstract(self, lead: str, article_text: str, meta_description: str) -> str:
        parts = []
        for value in (lead, meta_description, article_text):
            text = self._clean_text(value)
            if text:
                parts.append(text)
        abstract = self._clean_text(" ".join(self._dedupe(parts)))
        if len(abstract) > 4000:
            abstract = abstract[:4000].rsplit(" ", 1)[0].strip()
        return abstract

    def _extract_journal_fields(self, title: str, collections: list[str]) -> tuple[str | None, str | None, str | None]:
        m = re.search(r"^(In Situ\.\s*Revue des patrimoines)\s+n\W*\s*([0-9]+)", title, re.I)
        if m:
            return self._clean_text(m.group(1)), m.group(2), None
        for collection in collections or []:
            if "revue" in self._strip_accents(collection).lower():
                return collection, None, None
        return None, None, None

    @staticmethod
    def _meta_content(soup, key: str) -> str | None:
        tag = soup.find("meta", attrs={"name": key})
        if tag is None:
            tag = soup.find("meta", attrs={"property": key})
        if tag is None:
            tag = soup.find("meta", attrs={"itemprop": key})
        if tag is None:
            return None
        value = tag.get("content")
        return unescape(value).strip() if value else None

    @staticmethod
    def _extract_meta_tags(soup) -> dict:
        out = {}
        for tag in soup.find_all("meta"):
            key = tag.get("name") or tag.get("property") or tag.get("itemprop")
            value = tag.get("content")
            if key and value and key not in out:
                out[key] = unescape(value)
        return out

    # ------------------------------------------------------------------
    # Record construction
    # ------------------------------------------------------------------

    def _to_paper(self, list_item: dict, detail: dict) -> dict:
        url = detail.get("canonical") or list_item["url"]
        slug = detail.get("slug") or self._slug_from_url(url) or list_item.get("slug")
        native_id = detail.get("location_id") or slug or list_item["url"]
        external_id = str(native_id)
        post_number = external_id if external_id.isdigit() else (slug or external_id)

        listed_date = list_item.get("listed_date") or detail.get("published_date")
        published_date = detail.get("published_date") or listed_date
        original_filename = detail.get("original_filename") or self._filename_from_url(detail.get("pdf_url"))
        category = detail.get("category") or "Publications"
        keywords = ", ".join(detail.get("keywords") or [])
        abstract = detail.get("abstract") or ""

        metadata = {
            "posted_date": list_item.get("listed_date_raw") or listed_date,
            "listed_date": listed_date,
            "originalFilename": original_filename or detail.get("download_attr"),
            "journal_raw": detail.get("journal_raw"),
            "series": detail.get("series"),
            "volume": detail.get("volume"),
            "issue": detail.get("issue"),
            "location_id": detail.get("location_id"),
            "content_id": detail.get("location_id"),
            "node_id": detail.get("location_id"),
            "post_number": post_number,
            "slug": slug,
            "resource_type": "50",
            "content_download_id": detail.get("content_download_id"),
            "canonical": url,
            "list_url": self.START_URL,
            "list_page_url": list_item.get("list_page_url"),
            "list_record": list_item,
            "list_endpoint": "/espace-documentation?resource_type%5B0%5D=50",
            "detail_endpoint": list_item.get("url"),
            "published_date_raw": detail.get("detail_date_raw"),
            "category": category,
            "keywords": detail.get("keywords") or [],
            "tags": detail.get("tags") or [],
            "downloads": detail.get("downloads") or [],
            "mc_datalayer": detail.get("mc_datalayer") or {},
            "meta_tags": detail.get("meta_tags") or {},
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": detail.get("title") or list_item.get("title") or "(untitled)",
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "",
            "publisher": detail.get("publisher") or "Ministere de la Culture",
            "department": detail.get("department") or "",
            "journal": detail.get("journal") or "",
            "url": url,
            "pdf_url": detail.get("pdf_url"),
            "keywords": keywords,
            "category": category,
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"
        reached_cap = True

        for page in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                reached_cap = False
                break

            if time.time() - start_time >= self.WALL_CLOCK_SECONDS - self.WALL_CLOCK_GRACE_SECONDS:
                print(
                    f"[{self.site_id}] approaching 25-minute budget at page {page}; "
                    "exiting cleanly."
                )
                reached_cap = False
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            page_url = self._build_list_url(page)
            raw = self._curl_get(page_url, referer=self.START_URL)
            if not raw:
                print(f"[{self.site_id}] page {page}: fetch failed, stopping.")
                reached_cap = False
                break

            items, has_next = self._parse_list_page(raw, page_url)
            if not items:
                print(f"[{self.site_id}] page {page}: no items, stopping.")
                reached_cap = False
                break

            new_urls_on_page = 0
            for index, item in enumerate(items, start=1):
                item_url = item.get("url")
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_urls_on_page += 1

                if limit is not None and saved >= limit:
                    break

                try:
                    if time.time() - start_time >= self.WALL_CLOCK_SECONDS - self.WALL_CLOCK_GRACE_SECONDS:
                        print(f"[{self.site_id}] approaching 25-minute budget during page {page}; exiting.")
                        reached_cap = False
                        return saved

                    if self.detail_sleep:
                        time.sleep(self.detail_sleep)

                    detail_html = self._curl_get(item_url, referer=page_url)
                    if not detail_html:
                        print(f"[{self.site_id}] item {item_url} failed: detail fetch failed")
                        continue

                    detail = self._parse_detail(detail_html, item_url)
                    paper = self._to_paper(item, detail)
                    abstract_len = len(paper.get("abstract") or "")
                    if abstract_len < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_url} skipped: "
                            f"abstract too short ({abstract_len} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url or index} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records, stopping.")
                reached_cap = False
                break

            if limit is not None and saved >= limit:
                reached_cap = False
                break

            if not has_next:
                reached_cap = False
                break

        if reached_cap:
            print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached.")

        return saved
