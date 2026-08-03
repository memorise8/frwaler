# -*- coding: utf-8 -*-
"""Crawler for mivau.gob.es - Sala de Prensa.

Starting URL: https://www.mivau.gob.es/el-ministerio/sala-de-prensa
List endpoint: HTML pages, first page at the starting URL and subsequent pages
as ?page=N (Drupal zero-based pager, so page=1 is the second visible page).
Detail endpoint: each /el-ministerio/sala-de-prensa/noticias/<slug> page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_SITE_ID = "mivau-gob-es-el-ministerio"
_BASE_URL = "https://www.mivau.gob.es"
_LIST_URL = f"{_BASE_URL}/el-ministerio/sala-de-prensa"
_PUBLISHER = "Ministerio de Vivienda y Agenda Urbana"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = [
    "-H", f"User-Agent: {_UA}",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: es-ES,es;q=0.9,en-US;q=0.8",
    "-H", "Connection: keep-alive",
]

_BACKOFF = (1, 3, 9)
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_RUNTIME_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT = 50
_STATUS_MARKER = b"\n__MIVAU_HTTP_STATUS__:"


def _normalize_ws(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _curl_get(url: str, retries: int = 3, timeout: int = 30) -> str | None:
    """Fetch a URL using curl, retrying with exponential backoff."""
    cmd = [
        "curl",
        "-sk",
        "--tls-max", "1.3",
        "--compressed",
        "-L",
        "--max-time", str(timeout),
        "-w", _STATUS_MARKER.decode("ascii") + "%{http_code}",
    ]
    cmd.extend(_HEADERS)
    cmd.append(url)

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            stdout = result.stdout or b""
            body, status = stdout, None
            if _STATUS_MARKER in stdout:
                body, status_raw = stdout.rsplit(_STATUS_MARKER, 1)
                try:
                    status = int(status_raw.strip()[-3:])
                except ValueError:
                    status = None

            if result.returncode == 0 and body and (status is None or status < 400):
                return body.decode("utf-8", errors="replace")

            status_text = f", status={status}" if status is not None else ""
            print(
                f"[{_SITE_ID}] curl failed "
                f"(attempt {attempt + 1}/{retries}, code={result.returncode}{status_text}) "
                f"for {url[:120]}"
            )
        except subprocess.TimeoutExpired:
            print(f"[{_SITE_ID}] curl timeout (attempt {attempt + 1}/{retries}) for {url[:120]}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}) for {url[:120]}: {exc}")

        if attempt < retries - 1:
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])

    return None


def _make_soup(raw: str | bytes | None):
    """Parse HTML with a tolerant parser fallback chain."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw

    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] parser {parser} failed: {exc}")
    return None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return "-".join(match.groups())
    match = re.search(r"(\d{2})-(\d{2})-(\d{4})", value)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", value)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return None


def _page_url(page_number: int) -> str:
    if page_number <= 1:
        return _LIST_URL
    return f"{_LIST_URL}?page={page_number - 1}"


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    filename = unquote(path.rsplit("/", 1)[-1])
    return filename if "." in filename and len(filename) <= 240 else None


def _parse_node_id(soup, detail_url: str) -> str | None:
    shortlink = soup.find("link", rel=lambda v: v and "shortlink" in v)
    href = shortlink.get("href", "") if shortlink else ""
    match = re.search(r"/node/(\d+)", href)
    if match:
        return match.group(1)

    body_classes = " ".join(soup.body.get("class", [])) if soup.body else ""
    match = re.search(r"page-node-(\d+)", body_classes)
    if match:
        return match.group(1)

    match = re.search(r"/noticias/([^/?#]+)", detail_url)
    return match.group(1) if match else None


def _parse_list_page(html: str, page_number: int) -> tuple[list[dict], bool]:
    soup = _make_soup(html)
    if soup is None:
        return [], False

    items = []
    for row in soup.select("li.noticia"):
        try:
            link = row.select_one("a.stretched-link[href]") or row.find("a", href=True)
            if not link:
                continue

            href = (link.get("href") or "").strip()
            if not href or "/sala-de-prensa/noticias/" not in href:
                continue

            title_el = link.find("h2") or link
            title = _normalize_ws(title_el.get_text(" ", strip=True))
            if not title:
                continue

            time_el = row.find("time")
            listed_date = _iso_date(time_el.get("datetime")) if time_el else None
            posted_raw = _normalize_ws(time_el.get_text(" ", strip=True)) if time_el else None

            antetitulo_el = row.select_one(".antetitulo_noticia")
            category_el = row.select_one(".detalle_etiqueta")
            teaser = _normalize_ws(" ".join(
                p.get_text(" ", strip=True) for p in row.select(".cuerpo_noticia p")
            ))
            url = urljoin(_BASE_URL, href)
            slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

            items.append({
                "title": title,
                "url": url,
                "slug": slug,
                "listed_date": listed_date,
                "posted_raw": posted_raw,
                "teaser": teaser,
                "section": _normalize_ws(antetitulo_el.get_text(" ", strip=True)) if antetitulo_el else None,
                "category": _normalize_ws(category_el.get_text(" ", strip=True)) if category_el else "Noticia",
                "list_page": page_number,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] row parse error: {exc}")
            continue

    next_link = soup.select_one("li.pager-next a[href]")
    return items, next_link is not None


def _extract_pdf(soup) -> tuple[str | None, str | None]:
    pdf_links = []
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        classes = " ".join(link.get("class", []))
        title = link.get("title", "")
        text = link.get_text(" ", strip=True)
        looks_pdf = ".pdf" in href.lower()
        looks_main = "documento principal" in (title + " " + text).lower()
        looks_class_pdf = "stack-file-pdf" in classes
        if looks_pdf or looks_main or looks_class_pdf:
            pdf_url = urljoin(_BASE_URL, href)
            pdf_links.append((pdf_url, looks_main or looks_class_pdf))

    chosen = None
    for pdf_url, preferred in pdf_links:
        if preferred and ".pdf" in pdf_url.lower():
            chosen = pdf_url
            break
    if chosen is None:
        for pdf_url, _preferred in pdf_links:
            if ".pdf" in pdf_url.lower():
                chosen = pdf_url
                break
    if chosen is None:
        return None, None
    return chosen, _filename_from_url(chosen)


def _extract_archive_terms(soup) -> dict:
    archived = soup.select_one("#archivado")
    if not archived:
        return {}

    labels = {}
    current_label = None
    for child in archived.children:
        name = getattr(child, "name", None)
        text = _normalize_ws(child.get_text(" ", strip=True)) if hasattr(child, "get_text") else _normalize_ws(str(child))
        if not text:
            continue
        if name == "span":
            current_label = text.rstrip(":").strip()
        elif current_label:
            labels[current_label] = text
            current_label = None
    return labels


def _extract_abstract(soup) -> str:
    body = soup.select_one(".detalle_noticia .cuerpo_noticia")
    if not body:
        meta = soup.find("meta", attrs={"name": "description"})
        return _normalize_ws(meta.get("content")) if meta else ""

    chunks = []
    for el in body.select("p, li"):
        text = _normalize_ws(el.get_text(" ", strip=True))
        if not text:
            continue
        if text.lower() == "descargar imagen":
            continue
        if re.fullmatch(r"\d{2}[-/]\d{2}[-/]\d{4}", text):
            continue
        chunks.append(text)

    if not chunks:
        return _normalize_ws(body.get_text(" ", strip=True))
    return _normalize_ws(" ".join(chunks))


def _parse_detail(html: str, item: dict) -> dict | None:
    soup = _make_soup(html)
    if soup is None:
        return None

    node_id = _parse_node_id(soup, item["url"])
    published_meta = soup.find("meta", property="article:published_time")
    modified_meta = soup.find("meta", property="article:modified_time")
    published_raw = published_meta.get("content") if published_meta else None
    modified_raw = modified_meta.get("content") if modified_meta else None
    published_date = _iso_date(published_raw) or item.get("listed_date")

    h1 = soup.find("h1")
    title = _normalize_ws(h1.get_text(" ", strip=True)) if h1 else item["title"]
    abstract = _extract_abstract(soup)
    if len(abstract) < _MIN_ABSTRACT:
        abstract = item.get("teaser") or abstract

    pdf_url, original_filename = _extract_pdf(soup)
    archive_terms = _extract_archive_terms(soup)
    area = archive_terms.get("Area") or archive_terms.get("Área")
    scope = archive_terms.get("Ambito geográfico") or archive_terms.get("Ámbito geográfico")

    keyword_parts = []
    for value in (item.get("section"), area, scope):
        if value and value not in keyword_parts:
            keyword_parts.append(value)

    return {
        "title": title or item["title"],
        "abstract": abstract,
        "node_id": node_id,
        "published_date": published_date,
        "published_raw": published_raw,
        "modified_raw": modified_raw,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "archive_terms": archive_terms,
        "area": area,
        "scope": scope,
        "keywords": ", ".join(keyword_parts) if keyword_parts else None,
    }


class MivauGobEsElMinisterioCrawler(BaseCrawler):
    site_id = "mivau-gob-es-el-ministerio"
    site_name = "Custom: mivau-gob-es-el-ministerio"
    base_url = "https://www.mivau.gob.es"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        started_at = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page_number in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - started_at
            if elapsed >= _MAX_RUNTIME_S - 5:
                print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; stopping cleanly")
                break

            if page_number % 10 == 0:
                print(f"[{_SITE_ID}] page {page_number}: saved {saved}/{limit_or_inf}")

            if page_number == _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping after this page")

            list_url = _page_url(page_number)
            html = _curl_get(list_url)
            if not html:
                print(f"[{_SITE_ID}] page {page_number}: fetch failed; stopping")
                break

            items, has_next = _parse_list_page(html, page_number)
            if not items:
                print(f"[{_SITE_ID}] page {page_number}: 0 records; stopping")
                break

            new_items = [item for item in items if item["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] page {page_number}: all records already seen; stopping")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                seen_urls.add(url)

                try:
                    if time.time() - started_at >= _MAX_RUNTIME_S - 5:
                        print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; stopping cleanly")
                        return saved

                    time.sleep(getattr(self, "_delay", 1.0) or 0)
                    detail_html = _curl_get(url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] item {url} failed: detail fetch failed after retries")
                        continue

                    detail = _parse_detail(detail_html, item)
                    if detail is None:
                        print(f"[{_SITE_ID}] item {url} failed: detail parse returned no data")
                        continue

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT:
                        print(f"[{_SITE_ID}] skipping {url}: abstract too short ({len(abstract)} chars)")
                        continue

                    external_id = detail.get("node_id") or item["slug"]
                    post_number = detail.get("node_id") or item["slug"]
                    metadata = {
                        "posted_date": item.get("posted_raw") or item.get("listed_date"),
                        "listed_date": item.get("listed_date"),
                        "originalFilename": detail.get("original_filename"),
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "node_id": detail.get("node_id"),
                        "slug": item.get("slug"),
                        "list_page": item.get("list_page"),
                        "section": item.get("section"),
                        "archive_terms": detail.get("archive_terms"),
                        "scope": detail.get("scope"),
                        "detail_published_time": detail.get("published_raw"),
                        "detail_modified_time": detail.get("modified_raw"),
                    }

                    self._save_paper({
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": detail.get("title") or item["title"],
                        "abstract": abstract,
                        "published_date": detail.get("published_date") or item.get("listed_date"),
                        "listed_date": item.get("listed_date"),
                        "posted_date": item.get("listed_date"),
                        "authors": None,
                        "publisher": _PUBLISHER,
                        "department": detail.get("area"),
                        "journal": None,
                        "url": url,
                        "pdf_url": detail.get("pdf_url"),
                        "keywords": detail.get("keywords"),
                        "category": item.get("category") or "Noticia",
                        "doi": None,
                        "original_filename": detail.get("original_filename"),
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {(detail.get('title') or item['title'])[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            if not has_next:
                print(f"[{_SITE_ID}] page {page_number}: next page link absent; stopping")
                break

        print(f"[{_SITE_ID}] crawl complete. total saved: {saved}")
        return saved
