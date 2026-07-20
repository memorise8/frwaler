# -*- coding: utf-8 -*-
"""Crawler for digital.gob.es/comunicacion.

List endpoint discovered from the AEM PLP component:
  /content/portalmtdfp/es/comunicacion/jcr:content/root/container/containerSpace/content_display_copy.list.html
  /content/portalmtdfp/es/comunicacion/jcr:content/root/container/containerSpace/content_display_copy.list.p2.html

Detail metadata is exposed by AEM model JSON at:
  /content/portalmtdfp/es{public_path}.model.json
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - dependency is present in the crawler env
    BeautifulSoup = None


_SITE_ID = "digital-gob-es-comunicacion"
_BASE_URL = "https://digital.gob.es"
_START_URL = f"{_BASE_URL}/comunicacion"
_LIST_SERVICE = (
    "/content/portalmtdfp/es/comunicacion/jcr:content/root/container/"
    "containerSpace/content_display_copy"
)
_CONTENT_PREFIX = "/content/portalmtdfp/es"
_PUBLISHER = "Ministerio para la Transformación Digital y de la Función Pública"

_MAX_PAGES = 200
_MAX_RUNTIME_SECONDS = 25 * 60
_RETRY_DELAYS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 100

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_HTML_HEADERS = [
    "-H",
    f"User-Agent: {_UA}",
    "-H",
    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "-H",
    "Accept-Language: es-ES,es;q=0.9,en-US;q=0.8",
    "-H",
    "Connection: keep-alive",
]

_JSON_HEADERS = [
    "-H",
    f"User-Agent: {_UA}",
    "-H",
    "Accept: application/json,text/plain,*/*",
    "-H",
    "Accept-Language: es-ES,es;q=0.9,en-US;q=0.8",
    "-H",
    f"Referer: {_START_URL}",
]


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(_BASE_URL, url)


def _parse_date(raw: str | int | float | None) -> str | None:
    """Parse AEM millis or Spanish dd/mm/yyyy text to YYYY-MM-DD."""
    if raw is None or raw == "":
        return None

    if isinstance(raw, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(raw / 1000, tz=_dt.timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    text = _clean_text(str(raw))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        day, month, year = m.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)

    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlparse(url).path
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return None
    try:
        tail = urllib.parse.unquote(tail)
    except Exception:
        pass
    return tail if "." in tail and len(tail) <= 240 else None


def _slug_from_path(path: str) -> str | None:
    path = path.rstrip("/")
    if not path:
        return None
    slug = path.rsplit("/", 1)[-1]
    return slug or None


def _category_from_path(path: str) -> tuple[str | None, str | None]:
    parts = [p for p in path.split("/") if p]
    try:
        idx = parts.index("notas-prensa")
    except ValueError:
        return None, None
    if idx + 1 >= len(parts):
        return None, None
    slug = parts[idx + 1]
    label = slug.replace("-", " ").strip().title()
    return slug, label or slug


def _hash_id(external_id: str) -> str:
    digest = hashlib.sha1(external_id.encode("utf-8", errors="replace")).hexdigest()
    return f"{_SITE_ID}:{digest}"


def _iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _soup(raw: bytes | str | None):
    if raw is None or BeautifulSoup is None:
        return None
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


class DigitalGobEsComunicacionCrawler(BaseCrawler):
    site_id = "digital-gob-es-comunicacion"
    site_name = "Custom: digital-gob-es-comunicacion"
    base_url = "https://digital.gob.es"

    def _curl_text(self, url: str, headers: list[str] | None = None, retries: int = 3) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-sk",
            "-L",
            "--max-time",
            "30",
        ]
        cmd.extend(headers or _HTML_HEADERS)
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{_SITE_ID}] fetch failed "
                    f"(attempt {attempt + 1}/{retries}, code={result.returncode}) "
                    f"for {url[:120]} {err[:160]}"
                )
            except subprocess.TimeoutExpired:
                print(f"[{_SITE_ID}] fetch timeout (attempt {attempt + 1}/{retries}) for {url[:120]}")
            except Exception as exc:
                print(f"[{_SITE_ID}] fetch error (attempt {attempt + 1}/{retries}) for {url[:120]}: {exc}")

            if attempt < retries - 1:
                time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])

        return None

    def _list_url(self, page: int) -> str:
        suffix = "list" if page <= 1 else f"list.p{page}"
        return f"{_BASE_URL}{_LIST_SERVICE}.{suffix}.html"

    def _parse_list_page(self, html: str) -> list[dict]:
        soup = _soup(html)
        if not soup:
            return []

        items: list[dict] = []
        for article in soup.select(".dnt-card"):
            try:
                link = article.select_one(".dnt-title a[href]")
                if not link:
                    link = article.find("a", href=lambda h: h and "/comunicacion/notas-prensa/" in h)
                if not link:
                    continue

                href = link.get("href")
                url = _absolute_url(href)
                if not url:
                    continue

                parsed = urllib.parse.urlparse(url)
                if "/comunicacion/notas-prensa/" not in parsed.path:
                    continue

                title = _clean_text(link.get_text(" ", strip=True))
                if not title:
                    continue

                date_el = article.select_one(".dnt-date")
                listed_date_raw = _clean_text(date_el.get_text(" ", strip=True)) if date_el else None
                listed_date = _parse_date(listed_date_raw)

                teaser_el = article.select_one(".dnt-text")
                teaser = _clean_text(teaser_el.get_text(" ", strip=True)) if teaser_el else ""

                img = article.find("img")
                image_url = _absolute_url(img.get("src")) if img and img.get("src") else None
                image_alt = _clean_text(img.get("alt")) if img and img.get("alt") else None

                category_slug, category = _category_from_path(parsed.path)
                slug = _slug_from_path(parsed.path)

                items.append(
                    {
                        "title": title,
                        "url": url,
                        "href": href,
                        "public_path": parsed.path,
                        "listed_date": listed_date,
                        "listed_date_raw": listed_date_raw,
                        "teaser": teaser,
                        "category_slug": category_slug,
                        "category": category,
                        "slug": slug,
                        "image_url": image_url,
                        "image_alt": image_alt,
                    }
                )
            except Exception as exc:
                print(f"[{_SITE_ID}] list item parse failed: {exc}")

        return items

    def _detail_model_url(self, public_path: str) -> str:
        return f"{_BASE_URL}{_CONTENT_PREFIX}{public_path}.model.json"

    def _parse_detail_model(self, text: str, list_item: dict) -> dict:
        data = json.loads(text)

        title = _clean_text(data.get("title")) if isinstance(data, dict) else ""
        if not title:
            for node in _iter_dicts(data):
                if str(node.get(":type", "")).endswith("/title") and node.get("text"):
                    title = _clean_text(node.get("text"))
                    break

        published_date = None
        datepicker = None
        body_parts = []
        pdf_url = None
        button_text = None

        for node in _iter_dicts(data):
            if datepicker is None and node.get("datepicker") is not None:
                datepicker = node.get("datepicker")
                published_date = _parse_date(datepicker)

            node_type = str(node.get(":type", ""))
            if "components/common/text" in node_type and node.get("text"):
                part_soup = _soup(node.get("text"))
                if part_soup:
                    part = _clean_text(part_soup.get_text(" ", strip=True))
                else:
                    part = _clean_text(re.sub(r"<[^>]+>", " ", str(node.get("text"))))
                if part and "Este sitio web utiliza cookies" not in part and "© Ministerio" not in part:
                    body_parts.append(part)

            button_link = node.get("buttonLink")
            if isinstance(button_link, dict) and button_link.get("url"):
                link = button_link.get("url")
                if ".pdf" in link.lower() and pdf_url is None:
                    pdf_url = _absolute_url(link)
                    button_text = _clean_text(node.get("text")) or None

            for key in ("url", "href", "link"):
                value = node.get(key)
                if isinstance(value, str) and ".pdf" in value.lower() and pdf_url is None:
                    pdf_url = _absolute_url(value)

        teaser = list_item.get("teaser") or ""
        abstract_parts = []
        if teaser:
            abstract_parts.append(teaser)
        for part in body_parts:
            if part and part not in abstract_parts:
                abstract_parts.append(part)

        return {
            "title": title or list_item.get("title"),
            "abstract": _clean_text(" ".join(abstract_parts)),
            "published_date": published_date or list_item.get("listed_date"),
            "pdf_url": pdf_url,
            "original_filename": _filename_from_url(pdf_url),
            "raw": data,
            "aem_model_id": data.get("id") if isinstance(data, dict) else None,
            "aem_lastModifiedDate": data.get("lastModifiedDate") if isinstance(data, dict) else None,
            "datepicker": datepicker,
            "button_text": button_text,
        }

    def _parse_detail_html(self, html: str, list_item: dict) -> dict:
        soup = _soup(html)
        if not soup:
            return {}

        content = soup.select_one("#container-0a7df88752") or soup.find("main") or soup
        title_el = content.find("h1") or soup.find("h1")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else list_item.get("title")

        date_el = content.select_one(".date")
        published_date_raw = _clean_text(date_el.get_text(" ", strip=True)) if date_el else None
        published_date = _parse_date(published_date_raw) or list_item.get("listed_date")

        abstract_parts = []
        teaser = list_item.get("teaser") or ""
        if teaser:
            abstract_parts.append(teaser)
        for sel in (".subtitle-img .cmp-text", ".text .cmp-text"):
            for el in content.select(sel):
                part = _clean_text(el.get_text(" ", strip=True))
                if part and "Este sitio web utiliza cookies" not in part and part not in abstract_parts:
                    abstract_parts.append(part)

        pdf_url = None
        for link in content.find_all("a", href=True):
            href = link.get("href")
            if ".pdf" in href.lower():
                pdf_url = _absolute_url(href)
                break

        return {
            "title": title,
            "abstract": _clean_text(" ".join(abstract_parts)),
            "published_date": published_date,
            "published_date_raw": published_date_raw,
            "pdf_url": pdf_url,
            "original_filename": _filename_from_url(pdf_url),
            "raw": None,
        }

    def _fetch_detail(self, list_item: dict) -> dict:
        model_url = self._detail_model_url(list_item["public_path"])
        detail = {}

        text = self._curl_text(model_url, headers=_JSON_HEADERS)
        if text:
            try:
                if text.lstrip().startswith("{"):
                    detail = self._parse_detail_model(text, list_item)
            except Exception as exc:
                print(f"[{_SITE_ID}] model parse failed for {list_item['url']}: {exc}")

        if not detail.get("abstract"):
            html = self._curl_text(list_item["url"], headers=_HTML_HEADERS)
            if html:
                detail = self._parse_detail_html(html, list_item)

        detail.setdefault("detail_model_url", model_url)
        return detail

    def _build_paper(self, item: dict, detail: dict, page: int) -> dict:
        public_path = item["public_path"]
        external_id = public_path.strip("/") or item["url"]
        slug = item.get("slug") or _slug_from_path(public_path) or external_id
        post_number = slug
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)

        published_date = detail.get("published_date") or item.get("listed_date")
        listed_date = item.get("listed_date") or published_date
        category = item.get("category")
        department = category
        keywords = ", ".join(x for x in (item.get("category_slug"), category) if x)

        metadata = {
            "posted_date": item.get("listed_date_raw") or listed_date,
            "listed_date": listed_date,
            "published_date_raw": detail.get("published_date_raw"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "post_number": post_number,
            "slug": slug,
            "public_path": public_path,
            "content_path": f"{_CONTENT_PREFIX}{public_path}",
            "detail_model_url": detail.get("detail_model_url"),
            "list_endpoint": self._list_url(page),
            "list_page": page,
            "list_teaser": item.get("teaser"),
            "category_slug": item.get("category_slug"),
            "image_url": item.get("image_url"),
            "image_alt": item.get("image_alt"),
            "aem_model_id": detail.get("aem_model_id"),
            "aem_lastModifiedDate": detail.get("aem_lastModifiedDate"),
            "datepicker": detail.get("datepicker"),
            "button_text": detail.get("button_text"),
        }
        if detail.get("raw") is not None:
            metadata["aem_model"] = detail.get("raw")

        return {
            "id": _hash_id(external_id),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": detail.get("title") or item.get("title"),
            "abstract": detail.get("abstract"),
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": department,
            "journal": None,
            "url": item["url"],
            "pdf_url": pdf_url,
            "keywords": keywords or None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def crawl(self, limit=None):
        saved = 0
        limit_eff = float("inf") if limit is None else int(limit)
        limit_label = "inf" if limit is None else str(limit)
        seen_urls: set[str] = set()
        started = time.monotonic()
        deadline = started + _MAX_RUNTIME_SECONDS

        for page in range(1, _MAX_PAGES + 1):
            if saved >= limit_eff:
                break
            if time.monotonic() >= deadline - 5:
                print(f"[{_SITE_ID}] approaching 25-minute budget, stopping cleanly")
                break

            list_url = self._list_url(page)
            html = self._curl_text(list_url, headers=_HTML_HEADERS)
            if not html:
                print(f"[{_SITE_ID}] page {page}: fetch failed, stopping")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no items, stopping")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            new_on_page = 0
            for idx, item in enumerate(items, start=1):
                if saved >= limit_eff:
                    break
                if time.monotonic() >= deadline - 5:
                    print(f"[{_SITE_ID}] approaching 25-minute budget during page {page}, stopping cleanly")
                    break

                url = item.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(item)
                    abstract = _clean_text(detail.get("abstract"))
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{_SITE_ID}] item {page}.{idx} skipped: "
                            f"abstract too short ({len(abstract)} chars) for {url}"
                        )
                        continue
                    detail["abstract"] = abstract

                    paper = self._build_paper(item, detail, page)
                    if not paper.get("title") or not paper.get("url"):
                        print(f"[{_SITE_ID}] item {page}.{idx} skipped: missing title/url")
                        continue
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {page}.{idx} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: no new URLs, stopping")
                break
        else:
            print(f"[{_SITE_ID}] reached safety cap of {_MAX_PAGES} pages")

        return saved
