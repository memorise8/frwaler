# -*- coding: utf-8 -*-
"""GICHD Publications crawler.

Live discovery notes, 2026-06-04:
- List endpoint is server-rendered TYPO3 HTML at
  https://www.gichd.org/publications-resources/publications/
- Pagination is HTML at /publications-resources/publications/{offset}/ where
  the visible second page is suffix /1/.
- RSS exists but only exposes the first 10 latest publications, so it is not
  sufficient for full-depth crawling.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except ImportError:  # pragma: no cover - crawler runtime normally includes bs4
    _BeautifulSoup = None


def _make_soup(raw: str):
    """Build BeautifulSoup with parser fallback; never raise on bad markup."""
    if _BeautifulSoup is None or raw is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


_MONTHS = {
    "jan": "01",
    "january": "01",
    "feb": "02",
    "february": "02",
    "mar": "03",
    "march": "03",
    "apr": "04",
    "april": "04",
    "may": "05",
    "jun": "06",
    "june": "06",
    "jul": "07",
    "july": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "sept": "09",
    "september": "09",
    "oct": "10",
    "october": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _tag_text(tag) -> str:
    if tag is None:
        return ""
    return _clean_text(tag.get_text(" ", strip=True))


def _parse_date(raw: str | None) -> str | None:
    """Return YYYY-MM-DD for common GICHD/RSS/TYPO3 date strings."""
    if not raw:
        return None
    text = _clean_text(str(raw))
    if not text:
        return None

    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return match.group(0)

    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if match:
        day, month_name, year = match.groups()
        month = _MONTHS.get(month_name.lower())
        if month:
            return f"{year}-{month}-{day.zfill(2)}"

    try:
        return parsedate_to_datetime(text).date().isoformat()
    except Exception:
        return None


def _filename_from_content_disposition(header_text: str | None) -> str | None:
    if not header_text:
        return None
    match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", header_text, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";\r\n]+)"?', header_text, re.I)
    if match:
        return unquote(match.group(1).strip())
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").split("/")[-1]
    if not tail:
        return None
    return unquote(tail)


def _dedupe_keep_order(values):
    seen = set()
    out = []
    for value in values:
        value = _clean_text(str(value))
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


class GichdPublicationsCrawler(BaseCrawler):
    """Crawler for GICHD publications."""

    site_id = "gichd-org-publications-resourc"
    site_name = "Custom: gichd-org-publications-resourc"
    base_url = "https://www.gichd.org"

    _LIST_START_URL = "https://www.gichd.org/publications-resources/publications/"
    _MAX_PAGES = 200
    _WALL_CLOCK_BUDGET_S = 25 * 60
    _ABSTRACT_MIN_CHARS = 50

    def _curl_get(self, url: str, accept: str = "text/html,application/xhtml+xml,*/*;q=0.9") -> str | None:
        """GET with curl, TLS 1.3 cap, retries, and replacement decoding."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--fail",
            "--max-time",
            "45",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept}",
            url,
        ]
        waits = (1, 3, 9)
        for attempt in range(3):
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=55)
                body = proc.stdout.decode("utf-8", errors="replace")
                if proc.returncode == 0 and body.strip():
                    return body
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl attempt {attempt + 1}/3 failed "
                    f"for {url}: rc={proc.returncode} {err[:200]}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt + 1}/3 failed for {url}: {exc}")
            if attempt < 2:
                time.sleep(waits[attempt])
        return None

    def _curl_head(self, url: str) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skIL",
            "--max-time",
            "20",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            if proc.returncode == 0:
                return proc.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{self.site_id}] PDF HEAD failed for {url}: {exc}")
        return None

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self._LIST_START_URL
        return f"{self._LIST_START_URL}{page - 1}/"

    def _abs_url(self, href: str | None) -> str | None:
        if not href:
            return None
        return urljoin(self.base_url, href)

    @staticmethod
    def _slug_from_url(url: str) -> str | None:
        path = urlparse(url).path.rstrip("/")
        if not path:
            return None
        return unquote(path.split("/")[-1]) or None

    def _canonical_publication_url(self, href: str | None) -> str | None:
        abs_url = self._abs_url(href)
        if not abs_url:
            return None
        parsed = urlparse(abs_url)
        if parsed.netloc and parsed.netloc != "www.gichd.org":
            return None
        path = parsed.path
        prefix = "/publications-resources/publications/"
        if not path.startswith(prefix):
            return None
        slug = path.rstrip("/").split("/")[-1]
        if not slug or slug.isdigit() or slug in {"publications", "rss"}:
            return None
        return urljoin(self.base_url, path.rstrip("/") + "/")

    def _parse_taxonomy_topics(self, soup) -> list[str]:
        topics = []
        for meta in soup.select('meta[property="search:taxonomy:topics"]'):
            raw = meta.get("content") or ""
            try:
                data = json.loads(unescape(raw))
            except Exception:
                data = None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("title"):
                        topics.append(item["title"])
        return _dedupe_keep_order(topics)

    def _parse_json_ld(self, soup) -> list[dict]:
        items = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if isinstance(data, list):
                items.extend(x for x in data if isinstance(x, dict))
            elif isinstance(data, dict):
                graph = data.get("@graph")
                if isinstance(graph, list):
                    items.extend(x for x in graph if isinstance(x, dict))
                else:
                    items.append(data)
        return items

    def _article_json_ld(self, json_ld: list[dict]) -> dict:
        for item in json_ld:
            item_type = item.get("@type")
            if item_type == "Article" or (isinstance(item_type, list) and "Article" in item_type):
                return item
        return {}

    def _extract_pdf_filename(self, pdf_url: str | None) -> str | None:
        filename = _filename_from_url(pdf_url)
        if filename and "." in filename:
            return filename
        if not pdf_url:
            return filename
        headers = self._curl_head(pdf_url)
        return _filename_from_content_disposition(headers) or filename

    def _fetch_list_page(self, page: int) -> tuple[list[dict], bool]:
        url = self._page_url(page)
        raw = self._curl_get(url)
        if not raw:
            return [], False
        soup = _make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] list page {page} could not be parsed")
            return [], False

        containers = soup.select("div.news.list.publications div.records.list")
        if not containers:
            containers = [
                div for div in soup.select("div.records")
                if div.find_next_sibling("div", class_="pagination") is not None
            ]
        if not containers:
            containers = soup.select("div.records")

        records = []
        for container in containers:
            for record in container.select("div.record"):
                parsed = self._parse_list_record(record, page, url)
                if parsed:
                    records.append(parsed)

        has_next = bool(soup.select_one("div.pagination li.next a[href]") or soup.select_one('link[rel="next"]'))
        return records, has_next

    def _parse_list_record(self, record, page: int, url: str) -> dict | None:
        try:
            title_link = record.select_one("h2.title a[href]")
            if title_link is None:
                return None
            detail_url = self._canonical_publication_url(title_link.get("href"))
            if not detail_url:
                return None
            slug = self._slug_from_url(detail_url)
            title = _tag_text(title_link)
            if not title or not slug:
                return None

            short = _tag_text(record.select_one("div.short"))
            raw_date = _tag_text(record.select_one("div.details span.date") or record.select_one("span.date"))
            listed_date = _parse_date(raw_date)
            language = _tag_text(record.select_one("div.labelLanguage"))
            available_languages = _tag_text(record.select_one("li.languages span"))
            author = _tag_text(record.select_one("li.author"))
            image = record.select_one("div.image img[src], span.photo img[src]")
            image_url = self._abs_url(image.get("src")) if image else None

            return {
                "url": detail_url,
                "slug": slug,
                "title": title,
                "short_abstract": short,
                "raw_date": raw_date,
                "listed_date": listed_date,
                "language": language,
                "available_languages": available_languages,
                "author": author,
                "image_url": image_url,
                "source_page": page,
                "source_page_url": url,
            }
        except Exception as exc:
            print(f"[{self.site_id}] list record parse error on page {page}: {exc}")
            return None

    def _fetch_detail(self, url: str) -> dict:
        raw = self._curl_get(url)
        if not raw:
            return {}
        soup = _make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] detail page could not be parsed: {url}")
            return {}

        page_title = _tag_text(soup.find("title"))
        if page_title.lower().startswith("error - not found"):
            return {}

        result = {
            "title": None,
            "abstract": None,
            "published_date": None,
            "raw_published_date": None,
            "date_modified": None,
            "authors": [],
            "publisher": "GICHD",
            "language": None,
            "available_languages": None,
            "pdf_url": None,
            "original_filename": None,
            "category": None,
            "keywords": None,
            "doi": None,
            "json_ld": [],
            "image_url": None,
        }

        header = soup.select_one("div.publications.single.header")
        if header is not None:
            result["title"] = _tag_text(header.select_one("h1.articleHeading")) or None
            image = header.select_one("span.photo img[src], img[src]")
            if image:
                result["image_url"] = self._abs_url(image.get("src"))

            for li in header.select("ul.info li"):
                classes = set(li.get("class") or [])
                value = _tag_text(li.select_one("span.description"))
                if not value:
                    continue
                if "date" in classes:
                    result["raw_published_date"] = value
                    result["published_date"] = _parse_date(value)
                elif "author" in classes:
                    result["authors"] = _dedupe_keep_order([value])
                elif "labelLanguage" in classes:
                    result["language"] = value
                elif "languages" in classes:
                    result["available_languages"] = value

            pdf_link = None
            for selector in (
                "p.button.pdf a[href]",
                ".button.pdf a[href]",
                'a[href$=".pdf"]',
                'a[href*=".pdf"]',
            ):
                pdf_link = header.select_one(selector)
                if pdf_link is not None:
                    break
            if pdf_link is not None:
                result["pdf_url"] = self._abs_url(pdf_link.get("href"))

        json_ld = self._parse_json_ld(soup)
        result["json_ld"] = json_ld
        article = self._article_json_ld(json_ld)
        if article:
            result["title"] = result["title"] or _clean_text(article.get("headline")) or None
            if not result["published_date"]:
                result["published_date"] = _parse_date(article.get("datePublished"))
            result["date_modified"] = _parse_date(article.get("dateModified"))
            author = article.get("author")
            author_names = []
            if isinstance(author, dict):
                author_names.append(author.get("name"))
            elif isinstance(author, list):
                author_names.extend(a.get("name") for a in author if isinstance(a, dict))
            if author_names and not result["authors"]:
                result["authors"] = _dedupe_keep_order(author_names)
            publisher = article.get("publisher")
            if isinstance(publisher, dict) and publisher.get("name"):
                result["publisher"] = _clean_text(publisher.get("name"))

        content = soup.select_one("div.news.single.publications.model-publications div.content")
        paragraphs = []
        categories = []
        if content is not None:
            for cat_link in content.select("p.category span.categories a"):
                categories.append(_tag_text(cat_link))
            for p_tag in content.find_all("p"):
                if p_tag.find_parent(class_="related"):
                    continue
                if "category" in (p_tag.get("class") or []):
                    continue
                text = _tag_text(p_tag)
                if text:
                    paragraphs.append(text)

        abstract = "\n\n".join(paragraphs).strip()
        if not abstract:
            for attr, value in (
                ("property", "og:description"),
                ("name", "description"),
                ("name", "twitter:description"),
            ):
                meta = soup.find("meta", attrs={attr: value})
                if meta and meta.get("content"):
                    abstract = _clean_text(meta.get("content"))
                    break
        if not abstract and article.get("description"):
            abstract = _clean_text(article.get("description"))
        result["abstract"] = abstract or None

        categories.extend(self._parse_taxonomy_topics(soup))
        categories = _dedupe_keep_order(categories)
        if categories:
            result["category"] = "; ".join(categories)
            result["keywords"] = ", ".join(categories)

        if result["pdf_url"]:
            result["original_filename"] = self._extract_pdf_filename(result["pdf_url"])

        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", raw)
        if doi_match:
            result["doi"] = doi_match.group(0).rstrip(".,;)")

        return result

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.time() - start_time
                if elapsed >= self._WALL_CLOCK_BUDGET_S - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly")
                    break

                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                    break

                records, has_next = self._fetch_list_page(page)
                if not records:
                    print(f"[{self.site_id}] page {page}: 0 records; stopping")
                    break

                new_records = []
                for record in records:
                    url = record.get("url")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_records.append(record)

                if not new_records:
                    print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                    break

                for record in new_records:
                    if limit is not None and saved >= limit:
                        break

                    url = record["url"]
                    item_label = record.get("slug") or url
                    try:
                        time.sleep(max(float(getattr(self, "_delay", 1.0) or 0), 0.0))
                        detail = self._fetch_detail(url)

                        title = detail.get("title") or record.get("title") or "(untitled)"
                        abstract = detail.get("abstract") or record.get("short_abstract") or ""
                        abstract = abstract.strip()
                        if len(abstract) < self._ABSTRACT_MIN_CHARS:
                            print(f"[{self.site_id}] skipping {item_label}: abstract too short ({len(abstract)} chars)")
                            continue

                        listed_date = record.get("listed_date")
                        published_date = detail.get("published_date") or listed_date
                        authors = detail.get("authors") or [record.get("author")]
                        authors = _dedupe_keep_order(authors)
                        pdf_url = detail.get("pdf_url")
                        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
                        language = detail.get("language") or record.get("language")
                        available_languages = detail.get("available_languages") or record.get("available_languages")
                        category = detail.get("category")
                        keywords = detail.get("keywords")
                        external_id = record.get("slug") or self._slug_from_url(url) or url

                        metadata = {
                            "posted_date": record.get("raw_date"),
                            "listed_date": listed_date,
                            "originalFilename": original_filename,
                            "journal_raw": None,
                            "series": None,
                            "volume": None,
                            "issue": None,
                            "slug": external_id,
                            "node_id": None,
                            "source_page": record.get("source_page"),
                            "source_page_url": record.get("source_page_url"),
                            "list": {
                                "title": record.get("title"),
                                "short_abstract": record.get("short_abstract"),
                                "author": record.get("author"),
                                "language": record.get("language"),
                                "available_languages": record.get("available_languages"),
                                "image_url": record.get("image_url"),
                            },
                            "detail": {
                                "raw_published_date": detail.get("raw_published_date"),
                                "date_modified": detail.get("date_modified"),
                                "language": language,
                                "available_languages": available_languages,
                                "image_url": detail.get("image_url"),
                                "json_ld": detail.get("json_ld"),
                            },
                            "endpoint": {
                                "list": self._page_url(page),
                                "detail": url,
                                "type": "TYPO3 ll_catalog HTML",
                            },
                        }

                        self._save_paper(
                            {
                                "id": f"{self.site_id}:{external_id}",
                                "site_id": self.site_id,
                                "external_id": external_id,
                                "post_number": external_id,
                                "title": title,
                                "abstract": abstract,
                                "published_date": published_date,
                                "listed_date": listed_date,
                                "posted_date": listed_date,
                                "authors": "; ".join(authors) if authors else None,
                                "publisher": detail.get("publisher") or "GICHD",
                                "department": None,
                                "journal": None,
                                "url": url,
                                "pdf_url": pdf_url,
                                "keywords": keywords,
                                "category": category,
                                "doi": detail.get("doi"),
                                "original_filename": original_filename,
                                "metadata": json.dumps(metadata, ensure_ascii=False),
                            }
                        )
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {title[:80]}")
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                if not has_next:
                    print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                    break

                page += 1
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user; saved {saved}")
            raise

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
