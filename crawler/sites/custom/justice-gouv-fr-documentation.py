# -*- coding: utf-8 -*-
"""Crawler for justice.gouv.fr documentation resources.

Target list endpoint:
https://www.justice.gouv.fr/documentation/ressources?categories%5B%5D=399&categories%5B%5D=431&items_per_page=100

The site is Drupal-rendered HTML. List pages expose DSFR card markup and
detail pages expose Drupal node IDs, JSON-LD article metadata, tags, dates,
and PDF download anchors.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - runtime environment should provide it.
    BeautifulSoup = None


class JusticeGouvFrDocumentationCrawler(BaseCrawler):
    site_id = "justice-gouv-fr-documentation"
    site_name = "Custom: justice-gouv-fr-documentation"
    base_url = "https://www.justice.gouv.fr"

    START_URL = (
        "https://www.justice.gouv.fr/documentation/ressources"
        "?categories%5B%5D=399&categories%5B%5D=431&items_per_page=100"
    )
    MAX_PAGES = 200
    PAGE_SIZE = 100
    CURL_TIMEOUT = 60
    DETAIL_SLEEP = 1.0
    WALL_CLOCK_SECONDS = 25 * 60
    WALL_CLOCK_GRACE_SECONDS = 30
    MIN_ABSTRACT_CHARS = 50

    _MONTHS_FR = {
        "janvier": "01",
        "fevrier": "02",
        "février": "02",
        "mars": "03",
        "avril": "04",
        "mai": "05",
        "juin": "06",
        "juillet": "07",
        "aout": "08",
        "août": "08",
        "septembre": "09",
        "octobre": "10",
        "novembre": "11",
        "decembre": "12",
        "décembre": "12",
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
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        waits = [1, 3, 9]
        last = ""
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                    check=False,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last = f"exit={result.returncode} {stderr[:160]}"
            except Exception as exc:
                last = str(exc)

            if attempt < len(waits):
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last}")
        return None

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
    def _class_contains(tag, needle: str) -> bool:
        classes = tag.get("class") or []
        return any(needle in cls for cls in classes)

    def _build_list_url(self, page: int) -> str:
        url = self.START_URL
        if page > 0:
            url = f"{url}&page={page}"
        return url

    # ------------------------------------------------------------------
    # Date and filename helpers
    # ------------------------------------------------------------------

    def _parse_french_date(self, raw: str | None) -> str | None:
        text = self._clean_text(raw).lower()
        if not text:
            return None
        m = re.search(r"(\d{1,2})\s+([a-zàâäéèêëîïôöùûüç]+)\s+(\d{4})", text)
        if not m:
            return None
        day, month_name, year = m.groups()
        month = self._MONTHS_FR.get(month_name)
        if not month:
            return None
        return f"{year}-{month}-{day.zfill(2)}"

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
        tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        if tail and "." in tail and len(tail) <= 255:
            return tail
        return None

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen = set()
        out = []
        for value in values:
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
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str, page_url: str) -> tuple[list[dict], bool]:
        soup = self._make_soup(html, f"list page {page_url}")
        if soup is None:
            return [], False

        items = []
        for article in soup.find_all("article"):
            if not self._class_contains(article, "fr-card"):
                continue
            try:
                parsed = self._parse_card(article, page_url)
                if parsed:
                    items.append(parsed)
            except Exception as exc:
                print(f"[{self.site_id}] list card parse failed: {exc}")
                continue

        return items, self._has_next_page(soup)

    def _parse_card(self, article, page_url: str) -> dict | None:
        link = article.find("a", href=True, class_=lambda c: c and "fr-card__link" in c)
        if link is None:
            link = article.find("a", href=True)
        if link is None:
            return None

        href = (link.get("href") or "").strip()
        if not href:
            return None
        url = urljoin(self.base_url, href)

        title = self._clean_text(link)
        desc_el = article.find(class_=lambda c: c and "fr-card__desc" in c)
        abstract = self._clean_text(desc_el)
        detail_el = article.find(class_=lambda c: c and "fr-card__detail" in c)
        detail_raw = self._clean_text(detail_el)

        category = ""
        listed_date_raw = ""
        updated_date_raw = ""
        published_date = None
        listed_date = None
        updated_date = None
        m = re.match(
            r"^(.*?)\s*-\s*Publié le\s+(.+?)(?:\s*-\s*Mis à jour le\s+(.+))?$",
            detail_raw,
            flags=re.IGNORECASE,
        )
        if m:
            category = self._clean_text(m.group(1))
            listed_date_raw = self._clean_text(m.group(2))
            updated_date_raw = self._clean_text(m.group(3))
            listed_date = self._parse_french_date(listed_date_raw)
            published_date = listed_date
            updated_date = self._parse_french_date(updated_date_raw)
        else:
            category = detail_raw.split(" - ", 1)[0].strip() if detail_raw else ""

        slug = self._slug_from_url(url)
        return {
            "title": title,
            "abstract": abstract,
            "url": url,
            "slug": slug,
            "category": category,
            "detail_raw": detail_raw,
            "listed_date_raw": listed_date_raw,
            "listed_date": listed_date,
            "published_date": published_date,
            "updated_date_raw": updated_date_raw,
            "updated_date": updated_date,
            "list_page_url": page_url,
        }

    def _has_next_page(self, soup) -> bool:
        for link in soup.find_all("a", class_=lambda c: c and "fr-pagination__link--next" in c):
            if (link.get("aria-disabled") or "").lower() == "true":
                continue
            if link.get("href"):
                return True
        return False

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str) -> dict:
        soup = self._make_soup(html, f"detail {url}")
        if soup is None:
            return {}

        jsonld_articles = self._extract_jsonld_articles(soup)
        article_json = jsonld_articles[0] if jsonld_articles else {}
        drupal_settings = self._extract_drupal_settings(soup)
        eulerian = drupal_settings.get("eulerianDataLayer") or {}
        path_info = drupal_settings.get("path") or {}

        title = self._clean_text(soup.find("h1")) or self._clean_text(
            article_json.get("headline") or article_json.get("name")
        )
        if not title:
            title = self._meta_content(soup, "og:title") or self._meta_content(soup, "twitter:title")

        node_id = self._extract_node_id(soup, html, eulerian, path_info)
        category = self._clean_text(
            soup.find(class_=lambda c: c and "mj-content-header__typology" in c)
        )
        category = re.sub(r"^Typologie de contenus:\s*", "", category).strip()

        tags = []
        for tag in soup.select(".mj-content-header__tags a.fr-tag"):
            tags.append(self._clean_text(tag))
        tags = self._dedupe(tags)
        eulerian_tags = self._split_eulerian_tags(eulerian.get("content_tags"))

        date_text = self._clean_text(
            soup.find(class_=lambda c: c and "mj-content-header__date" in c)
        )
        detail_date_raw = ""
        m_date = re.search(r"Publié le\s+(.+)$", date_text, flags=re.IGNORECASE)
        if m_date:
            detail_date_raw = self._clean_text(m_date.group(1))

        published_date = (
            self._date_only(article_json.get("datePublished"))
            or self._date_only(self._meta_content(soup, "article:published_time"))
            or self._date_only(eulerian.get("content_date"))
            or self._parse_french_date(detail_date_raw)
        )
        modified_date = (
            self._date_only(article_json.get("dateModified"))
            or self._date_only(self._meta_content(soup, "article:modified_time"))
        )

        chapo = self._extract_chapo(soup)
        description = (
            chapo
            or self._clean_text(article_json.get("description"))
            or self._meta_content(soup, "description")
            or self._meta_content(soup, "og:description")
            or self._meta_content(soup, "twitter:description")
        )

        downloads = self._extract_downloads(soup)
        pdf_download = next((d for d in downloads if d.get("url", "").lower().split("?")[0].endswith(".pdf")), None)
        if pdf_download is None and downloads:
            pdf_download = downloads[0]

        author = self._extract_author(article_json, eulerian)
        if author and author.casefold() == "anonyme":
            author = ""

        meta_tags = self._extract_meta_tags(soup)
        shortlink = self._meta_href(soup, "shortlink")
        canonical = self._meta_href(soup, "canonical") or url

        return {
            "title": title,
            "abstract": description,
            "published_date": published_date,
            "modified_date": modified_date,
            "detail_date_raw": detail_date_raw,
            "category": category,
            "keywords": self._dedupe(tags + eulerian_tags),
            "downloads": downloads,
            "pdf_url": pdf_download.get("url") if pdf_download else None,
            "original_filename": (
                self._filename_from_url(pdf_download.get("url")) if pdf_download else None
            ),
            "download_attr": pdf_download.get("download") if pdf_download else None,
            "authors": author,
            "publisher": "Ministère de la justice",
            "node_id": node_id,
            "canonical": canonical,
            "shortlink": shortlink,
            "jsonld_article": article_json,
            "jsonld_articles": jsonld_articles,
            "drupal_settings": drupal_settings,
            "eulerian": eulerian,
            "path_info": path_info,
            "meta_tags": meta_tags,
        }

    def _extract_jsonld_articles(self, soup) -> list[dict]:
        articles = []
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            candidates = []
            if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                candidates = [x for x in data.get("@graph") if isinstance(x, dict)]
            elif isinstance(data, dict):
                candidates = [data]
            elif isinstance(data, list):
                candidates = [x for x in data if isinstance(x, dict)]
            for item in candidates:
                types = item.get("@type")
                if isinstance(types, str):
                    types = [types]
                if types and any("Article" in str(t) for t in types):
                    articles.append(item)
        return articles

    def _extract_drupal_settings(self, soup) -> dict:
        script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if script is None:
            return {}
        raw = script.string or script.get_text()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _extract_node_id(self, soup, html: str, eulerian: dict, path_info: dict) -> str | None:
        for value in (
            eulerian.get("content_id"),
            (path_info.get("currentPath") or "").replace("node/", "", 1),
        ):
            if value and str(value).isdigit():
                return str(value)

        shortlink = self._meta_href(soup, "shortlink")
        for source in (shortlink, html):
            if not source:
                continue
            m = re.search(r"/node/(\d+)", source)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _split_eulerian_tags(value) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            raw = value
        else:
            raw = str(value).split(",")
        return [str(item).replace("_", " ").strip() for item in raw if str(item).strip()]

    def _extract_chapo(self, soup) -> str:
        chapo = soup.find(class_=lambda c: c and "mj-content-header__chapo" in c)
        if chapo is None:
            return ""
        field = chapo.find(class_=lambda c: c and "field__item" in c)
        if field is not None:
            return self._clean_text(field)
        paragraphs = []
        for child in chapo.find_all(["p", "div"], recursive=True):
            classes = child.get("class") or []
            if any("mj-content-header__date" in cls for cls in classes):
                continue
            text = self._clean_text(child)
            if text and not text.lower().startswith("publié le"):
                paragraphs.append(text)
        return " ".join(self._dedupe(paragraphs))

    def _extract_downloads(self, soup) -> list[dict]:
        downloads = []
        for link in soup.find_all("a", href=True):
            href = (link.get("href") or "").strip()
            classes = link.get("class") or []
            is_download = any("fr-link--download" in cls for cls in classes)
            if not is_download and not href.lower().split("?")[0].endswith(".pdf"):
                continue
            full_url = urljoin(self.base_url, href)
            title = self._clean_text(link.get("title")) or self._clean_text(link)
            detail = self._clean_text(link.find(class_=lambda c: c and "fr-link__detail" in c))
            downloads.append({
                "url": full_url,
                "title": title,
                "text": self._clean_text(link),
                "detail": detail,
                "download": link.get("download"),
                "originalFilename": self._filename_from_url(full_url) or link.get("download"),
            })
        return downloads

    def _extract_author(self, article_json: dict, eulerian: dict) -> str:
        author = eulerian.get("content_author") or ""
        if author:
            return self._clean_text(author)
        raw_author = article_json.get("author")
        if isinstance(raw_author, dict):
            return self._clean_text(raw_author.get("name"))
        if isinstance(raw_author, list):
            names = []
            for item in raw_author:
                if isinstance(item, dict):
                    names.append(self._clean_text(item.get("name")))
                else:
                    names.append(self._clean_text(item))
            return "; ".join(self._dedupe(names))
        return self._clean_text(raw_author)

    @staticmethod
    def _meta_content(soup, key: str) -> str | None:
        tag = soup.find("meta", attrs={"name": key})
        if tag is None:
            tag = soup.find("meta", attrs={"property": key})
        if tag is None:
            return None
        value = tag.get("content")
        return unescape(value).strip() if value else None

    @staticmethod
    def _meta_href(soup, rel_name: str) -> str | None:
        tag = soup.find("link", rel=lambda rel: rel and rel_name in rel)
        if tag is None:
            return None
        href = tag.get("href")
        return unescape(href).strip() if href else None

    @staticmethod
    def _extract_meta_tags(soup) -> dict:
        out = {}
        for tag in soup.find_all("meta"):
            key = tag.get("name") or tag.get("property")
            value = tag.get("content")
            if key and value:
                out[key] = unescape(value)
        return out

    # ------------------------------------------------------------------
    # Record construction
    # ------------------------------------------------------------------

    def _build_abstract(self, list_item: dict, detail: dict) -> str:
        abstract = detail.get("abstract") or list_item.get("abstract") or ""
        abstract = self._clean_text(abstract)
        if len(abstract) >= 100:
            return abstract

        parts = [abstract]
        list_abstract = self._clean_text(list_item.get("abstract"))
        if list_abstract and list_abstract not in parts:
            parts.append(list_abstract)

        for download in detail.get("downloads") or []:
            title = self._clean_text(download.get("title"))
            if title:
                parts.append(title)
            if len(" ".join(parts)) >= 100:
                break

        return self._clean_text(" ".join(p for p in parts if p))

    def _to_paper(self, list_item: dict, detail: dict) -> dict:
        url = detail.get("canonical") or list_item["url"]
        slug = self._slug_from_url(url) or list_item.get("slug")
        node_id = detail.get("node_id")
        external_id = str(node_id or slug or list_item["url"])
        post_number = str(node_id) if node_id and str(node_id).isdigit() else (slug or external_id)

        listed_date = list_item.get("listed_date") or detail.get("published_date")
        published_date = detail.get("published_date") or list_item.get("published_date") or listed_date
        category = detail.get("category") or list_item.get("category") or ""
        original_filename = detail.get("original_filename") or self._filename_from_url(detail.get("pdf_url"))
        keywords = ", ".join(detail.get("keywords") or [])
        abstract = self._build_abstract(list_item, detail)

        metadata = {
            "posted_date": list_item.get("listed_date_raw") or detail.get("detail_date_raw") or listed_date,
            "listed_date": listed_date,
            "originalFilename": original_filename or detail.get("download_attr"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "content_id": node_id,
            "post_number": post_number,
            "slug": slug,
            "canonical": detail.get("canonical"),
            "shortlink": detail.get("shortlink"),
            "list_url": self.START_URL,
            "list_page_url": list_item.get("list_page_url"),
            "list_record": list_item,
            "list_detail_raw": list_item.get("detail_raw"),
            "published_date_raw": detail.get("detail_date_raw"),
            "modified_date": detail.get("modified_date"),
            "updated_date": list_item.get("updated_date"),
            "updated_date_raw": list_item.get("updated_date_raw"),
            "category": category,
            "keywords": detail.get("keywords") or [],
            "downloads": detail.get("downloads") or [],
            "jsonld_article": detail.get("jsonld_article") or {},
            "jsonld_articles": detail.get("jsonld_articles") or [],
            "drupal_settings": detail.get("drupal_settings") or {},
            "eulerian": detail.get("eulerian") or {},
            "path_info": detail.get("path_info") or {},
            "meta_tags": detail.get("meta_tags") or {},
            "detail_endpoint": list_item["url"],
            "list_endpoint": "/documentation/ressources",
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
            "authors": detail.get("authors") or "",
            "publisher": detail.get("publisher") or "Ministère de la justice",
            "department": "",
            "journal": "",
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
        limit_or_inf = limit if limit is not None else "∞"
        reached_cap = True

        for page in range(self.MAX_PAGES):
            if limit is not None and saved >= limit:
                reached_cap = False
                break

            elapsed = time.time() - start_time
            if elapsed >= self.WALL_CLOCK_SECONDS - self.WALL_CLOCK_GRACE_SECONDS:
                print(
                    f"[{self.site_id}] approaching 25-minute budget at page {page}; "
                    "exiting cleanly."
                )
                reached_cap = False
                break

            if page % 10 == 0:
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
