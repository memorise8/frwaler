# -*- coding: utf-8 -*-
"""Crawler for Nivel English publications.

Target: https://www.nivel.nl/en/publications

Nivel is a Drupal 10 site.  The first result page is rendered in the
starting HTML and later pages are served by the Drupal Views AJAX endpoint:

    GET/POST https://www.nivel.nl/en/views/ajax?page=N

The important detail is that ``page`` must be in the query string.  Sending
it only as POST form data makes Drupal silently return page 0 again.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

# spec_from_file_location loads this file without package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402


try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover - repo normally installs bs4
    BeautifulSoup = None  # type: ignore


def _make_soup(raw):
    """Parse malformed site HTML without letting parser failures abort a crawl."""
    if BeautifulSoup is None or not raw:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(value):
    text = unescape(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _field_text(node, selector):
    if node is None:
        return ""
    found = node.select_one(selector)
    if found is None:
        return ""
    for br in found.find_all("br"):
        br.replace_with("\n")
    return re.sub(r"[ \t\r\f\v]+", " ", found.get_text("\n", strip=True)).strip()


def _parse_iso_date(raw):
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw or "")
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return ""


def _parse_dmy_date(raw):
    raw = _clean_text(raw)
    match = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", raw)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return _parse_iso_date(raw)


def _slug_from_url(url):
    path = urlparse(url or "").path.rstrip("/")
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


def _filename_from_url(url):
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _looks_like_initials(value):
    value = value.strip()
    if not value:
        return False
    return bool(re.match(r"^[A-Z][A-Za-z.\- ]{0,35}$", value))


def _split_authors(raw):
    """Convert Nivel's comma-heavy author string to semicolon-separated names."""
    raw = _clean_text(raw).strip()
    if not raw:
        return ""
    if ";" in raw:
        return "; ".join(p.strip() for p in raw.split(";") if p.strip())

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        return raw

    authors = []
    i = 0
    while i < len(parts):
        if i + 1 < len(parts) and _looks_like_initials(parts[i + 1]):
            authors.append(f"{parts[i]}, {parts[i + 1]}".strip())
            i += 2
            continue
        authors.append(parts[i].strip())
        i += 1
    return "; ".join(a for a in authors if a)


def _parse_imprint(imprint):
    """Extract journal/publisher-ish metadata from Nivel's free-text imprint."""
    imprint = _clean_text(imprint)
    result = {
        "journal": None,
        "journal_raw": imprint or None,
        "series": None,
        "volume": None,
        "issue": None,
        "publisher": "Nivel",
    }
    if not imprint:
        return result

    left = imprint.split(":", 1)[0].strip()
    right = imprint.split(":", 1)[1].strip() if ":" in imprint else ""

    # Reports are commonly rendered as "Utrecht: Nivel, 2026. 58 p.".
    if right and not re.search(r"\bNivel\b", right, flags=re.I):
        result["journal"] = left or None

    volume_issue = re.search(r"\b(\d+)\s*\(\s*([^)]+)\s*\)", imprint)
    if volume_issue:
        result["volume"] = volume_issue.group(1)
        result["issue"] = volume_issue.group(2)
    else:
        volume = re.search(r"\bVolume\s+(\d+)\b", imprint, flags=re.I)
        if volume:
            result["volume"] = volume.group(1)

    series = re.search(r"\b(series|report|factsheet|monitor)\b", imprint, flags=re.I)
    if series:
        result["series"] = series.group(1)

    return result


class NivelNlEnCrawler(BaseCrawler):
    site_id = "nivel-nl-en"
    site_name = "Custom: nivel-nl-en"
    base_url = "https://www.nivel.nl"

    START_URL = "https://www.nivel.nl/en/publications"
    SAFETY_CAP_PAGES = 200
    MAX_WALL_SECONDS = 25 * 60
    WALL_STOP_MARGIN_SECONDS = 30
    MIN_ABSTRACT_CHARS = 50
    BACKOFF_SECONDS = (1, 3, 9)

    DEFAULT_VIEW_CONFIG = {
        "ajax_path": "/en/views/ajax",
        "view_name": "publications",
        "view_display_id": "block_43",
        "view_args": "",
        "view_path": "/node/3637",
        "view_base_path": "publicaties2",
        "view_dom_id": "370f64b2e726ee771f36caad225cfd4c3956fa1496839e8362dd20a8ca783ac7",
        "pager_element": 0,
    }

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()
        seen_external_ids = set()
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        start_html = self._curl_get(self.START_URL, "start page")
        if not start_html:
            print("[nivel-nl-en] start page fetch failed; stopping")
            return 0

        view_config = self._extract_view_config(start_html)

        while page < self.SAFETY_CAP_PAGES:
            if limit is not None and saved >= limit:
                break
            if self._wall_budget_nearly_spent(started_at):
                print("[nivel-nl-en] wall-clock budget nearly reached; stopping cleanly")
                break
            if page % 10 == 0:
                print(f"[nivel-nl-en] page {page}: saved {saved}/{limit_or_inf}")

            if page == 0:
                list_html = start_html
            else:
                raw_json = self._fetch_ajax_page(page, view_config)
                if not raw_json:
                    print(f"[nivel-nl-en] page {page}: empty AJAX response; stopping")
                    break
                list_html = self._extract_ajax_html(raw_json)

            if not list_html:
                print(f"[nivel-nl-en] page {page}: no list HTML; stopping")
                break

            try:
                items = self._parse_list_items(list_html, page)
            except Exception as exc:
                print(f"[nivel-nl-en] page {page}: list parse failed: {exc}")
                break

            if not items:
                print(f"[nivel-nl-en] page {page}: no records; stopping")
                break

            new_items = []
            for item in items:
                item_url = item.get("url")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_items.append(item)

            if not new_items:
                print(f"[nivel-nl-en] page {page}: all item URLs already seen; stopping")
                break

            for index, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._wall_budget_nearly_spent(started_at):
                    print("[nivel-nl-en] wall-clock budget nearly reached; stopping cleanly")
                    return saved

                item_url = item.get("url") or ""
                item_label = f"{page}.{index}"
                try:
                    detail_delay = getattr(self, "detail_delay", getattr(self, "_delay", 1.0))
                    time.sleep(max(float(detail_delay or 0), 0.0))
                    detail_html = self._curl_get(item_url, f"detail {item_url}")
                    if not detail_html:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(item, detail_html)
                    if not paper:
                        continue

                    external_id = paper.get("external_id")
                    if external_id and external_id in seen_external_ids:
                        continue
                    if external_id:
                        seen_external_ids.add(external_id)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        title = paper.get("title") or item_url
                        print(
                            f"[nivel-nl-en] skipping '{title[:80]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[nivel-nl-en] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nivel-nl-en] item {item_label} failed: {exc}")
                    continue

            if not self._has_next_page(list_html):
                print(f"[nivel-nl-en] page {page}: no next page link; stopping")
                break
            page += 1

        if page >= self.SAFETY_CAP_PAGES:
            print(f"[nivel-nl-en] safety cap of {self.SAFETY_CAP_PAGES} pages reached")

        print(f"[nivel-nl-en] Done. Total saved: {saved}")
        return saved

    def _wall_budget_nearly_spent(self, started_at):
        elapsed = time.monotonic() - started_at
        return elapsed >= self.MAX_WALL_SECONDS - self.WALL_STOP_MARGIN_SECONDS

    def _curl_get(self, url, label):
        return self._curl(url, label=label)

    def _curl_post(self, url, fields, label):
        return self._curl(url, fields=fields, label=label)

    def _curl(self, url, fields=None, label="request"):
        if fields is None:
            accept = "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8"
        else:
            accept = "application/json, text/javascript, */*; q=0.01"
        headers = [
            f"Accept: {accept}",
            "Accept-Language: en-US,en;q=0.9,nl;q=0.8",
            f"Referer: {self.START_URL}",
        ]
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
            "-A", self.USER_AGENT,
        ]
        for header in headers:
            cmd.extend(["-H", header])
        if fields is not None:
            cmd.extend(["-H", "X-Requested-With: XMLHttpRequest"])
            for key, value in fields.items():
                cmd.extend(["--data-urlencode", f"{key}={value}"])
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                err = result.stderr.decode("utf-8", errors="replace").strip()
                if err:
                    print(
                        f"[nivel-nl-en] curl {label} attempt {attempt + 1}/3 "
                        f"returned {result.returncode}: {err[:160]}"
                    )
            except Exception as exc:
                print(f"[nivel-nl-en] curl {label} attempt {attempt + 1}/3 failed: {exc}")

            if attempt < 2:
                time.sleep(self.BACKOFF_SECONDS[attempt])

        print(f"[nivel-nl-en] curl {label} failed after 3 attempts")
        return ""

    def _extract_view_config(self, html):
        soup = _make_soup(html)
        if soup is None:
            return dict(self.DEFAULT_VIEW_CONFIG)

        script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if script is None:
            return dict(self.DEFAULT_VIEW_CONFIG)

        try:
            settings = json.loads(script.get_text() or "{}")
        except json.JSONDecodeError:
            return dict(self.DEFAULT_VIEW_CONFIG)

        views = ((settings.get("views") or {}).get("ajaxViews") or {})
        ajax_path = (settings.get("views") or {}).get("ajax_path") or self.DEFAULT_VIEW_CONFIG["ajax_path"]
        for view in views.values():
            if not isinstance(view, dict):
                continue
            if view.get("view_name") == "publications":
                config = dict(self.DEFAULT_VIEW_CONFIG)
                config.update(view)
                config["ajax_path"] = ajax_path
                return config
        return dict(self.DEFAULT_VIEW_CONFIG)

    def _fetch_ajax_page(self, page, view_config):
        ajax_url = urljoin(self.base_url, view_config.get("ajax_path") or "/en/views/ajax")
        separator = "&" if "?" in ajax_url else "?"
        ajax_url = f"{ajax_url}{separator}page={page}&_wrapper_format=drupal_ajax"
        fields = {
            "view_name": view_config.get("view_name", "publications"),
            "view_display_id": view_config.get("view_display_id", "block_43"),
            "view_args": view_config.get("view_args", ""),
            "view_path": view_config.get("view_path", "/node/3637"),
            "view_base_path": view_config.get("view_base_path", "publicaties2"),
            "view_dom_id": view_config.get("view_dom_id", ""),
            "pager_element": view_config.get("pager_element", 0),
        }
        return self._curl_post(ajax_url, fields, f"AJAX page {page}")

    def _extract_ajax_html(self, raw_json):
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            print(f"[nivel-nl-en] AJAX JSON parse failed: {exc}")
            return ""

        chunks = []
        for item in data:
            if isinstance(item, dict) and item.get("command") == "insert":
                value = item.get("data") or ""
                if "view-publications" in value or "views-row" in value:
                    chunks.append(value)
        return "\n".join(chunks)

    def _parse_list_items(self, html, page):
        soup = _make_soup(html)
        if soup is None:
            return self._parse_list_items_regex(html, page)

        rows = soup.select(".view-publications .views-row")
        if not rows:
            rows = soup.select(".views-row")

        items = []
        seen = set()
        for row in rows:
            pub = row.select_one(".publication--type-publication") or row
            href = self._row_href(pub)
            if not href:
                continue
            url = urljoin(self.base_url, href).split("#", 1)[0]
            if "/publicatie/" not in url or url in seen:
                continue
            seen.add(url)

            time_node = row.find("time")
            date_raw = time_node.get_text(" ", strip=True) if time_node else ""
            date_iso = time_node.get("datetime", "") if time_node else ""
            authors_raw = _field_text(row, ".field--name-field-authors")
            title = _field_text(row, ".field--name-field-title")
            imprint = _field_text(row, ".field--name-field-imprint")

            items.append({
                "url": url,
                "path": urlparse(url).path,
                "slug": _slug_from_url(url),
                "title": title,
                "authors_raw": authors_raw,
                "listed_date_raw": date_raw,
                "listed_datetime": date_iso,
                "listed_date": _parse_iso_date(date_iso) or _parse_dmy_date(date_raw),
                "imprint": imprint,
                "list_page": page,
            })
        return items

    def _parse_list_items_regex(self, html, page):
        items = []
        for match in re.finditer(
            r"onclick=\"location\.href=&#039;([^&]+)&#039;\"(?P<body>.*?)(?=onclick=\"location\.href=&#039;|</div>\s*</div>\s*</div>\s*</div>)",
            html,
            flags=re.S,
        ):
            href = unescape(match.group(1))
            if "/publicatie/" not in href:
                continue
            body = match.group("body")
            url = urljoin(self.base_url, href).split("#", 1)[0]
            title_m = re.search(r"field--name-field-title[^>]*field__item\">(.*?)</div>", body, flags=re.S)
            authors_m = re.search(r"field--name-field-authors.*?field__item\">(.*?)</div>", body, flags=re.S)
            imprint_m = re.search(r"field--name-field-imprint.*?field__item\">(.*?)</div>", body, flags=re.S)
            time_m = re.search(r"<time[^>]*datetime=\"([^\"]+)\"[^>]*>(.*?)</time>", body, flags=re.S)
            listed_datetime = time_m.group(1) if time_m else ""
            listed_date_raw = _clean_text(re.sub(r"<[^>]+>", " ", time_m.group(2))) if time_m else ""
            items.append({
                "url": url,
                "path": urlparse(url).path,
                "slug": _slug_from_url(url),
                "title": _clean_text(re.sub(r"<[^>]+>", " ", title_m.group(1))) if title_m else "",
                "authors_raw": _clean_text(re.sub(r"<[^>]+>", " ", authors_m.group(1))) if authors_m else "",
                "listed_date_raw": listed_date_raw,
                "listed_datetime": listed_datetime,
                "listed_date": _parse_iso_date(listed_datetime) or _parse_dmy_date(listed_date_raw),
                "imprint": _clean_text(re.sub(r"<[^>]+>", " ", imprint_m.group(1))) if imprint_m else "",
                "list_page": page,
            })
        return items

    def _row_href(self, row):
        onclick = row.get("onclick", "") if row is not None else ""
        match = re.search(r"location\.href=['\"]([^'\"]+)['\"]", onclick)
        if match:
            return match.group(1)
        link = row.find("a", href=True) if row is not None else None
        return link.get("href") if link else ""

    def _has_next_page(self, html):
        soup = _make_soup(html)
        if soup is not None:
            if soup.select_one('a[rel="next"], .pager a[href*="page="]'):
                return True
            return False
        return bool(re.search(r'href=["\']\?page=\d+["\']', html or ""))

    def _build_paper(self, list_item, detail_html):
        soup = _make_soup(detail_html)
        detail = self._parse_detail(detail_html, soup)

        title = detail.get("title") or list_item.get("title") or list_item.get("slug") or "(untitled)"
        abstract = detail.get("abstract") or ""
        listed_date = list_item.get("listed_date") or ""
        published_date = detail.get("published_date") or listed_date
        detail_url = detail.get("canonical_url") or list_item.get("url")
        slug = _slug_from_url(detail_url) or list_item.get("slug")
        node_id = detail.get("node_id")
        external_id = node_id or slug
        post_number = node_id if node_id and str(node_id).isdigit() else (slug or None)
        imprint_meta = _parse_imprint(list_item.get("imprint") or detail.get("imprint") or "")

        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
        authors = _split_authors(list_item.get("authors_raw") or detail.get("authors_raw") or "")
        publisher = imprint_meta.get("publisher") or "Nivel"
        journal = imprint_meta.get("journal")
        doi = detail.get("doi")
        category = detail.get("category") or "Publication"

        metadata = {
            "posted_date": listed_date,
            "posted_date_raw": list_item.get("listed_date_raw"),
            "listed_datetime": list_item.get("listed_datetime"),
            "originalFilename": original_filename,
            "journal_raw": imprint_meta.get("journal_raw"),
            "series": imprint_meta.get("series"),
            "volume": imprint_meta.get("volume"),
            "issue": imprint_meta.get("issue"),
            "node_id": node_id,
            "slug": slug,
            "post_number": post_number,
            "canonical_url": detail.get("canonical_url"),
            "list_url": list_item.get("url"),
            "list_page": list_item.get("list_page"),
            "list_title": list_item.get("title"),
            "authors_raw": list_item.get("authors_raw"),
            "imprint": list_item.get("imprint"),
            "published_date_raw": detail.get("published_date_raw"),
            "category_raw": detail.get("category_raw"),
            "research_programs": detail.get("research_programs"),
            "pdf_url_raw": pdf_url,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": "; ".join(detail.get("research_programs") or []) or None,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_detail(self, html, soup):
        result = {
            "abstract": "",
            "title": "",
            "published_date": "",
            "published_date_raw": "",
            "doi": None,
            "pdf_url": None,
            "original_filename": None,
            "node_id": None,
            "canonical_url": None,
            "category": None,
            "category_raw": None,
            "research_programs": [],
        }

        result["node_id"] = self._extract_node_id(html, soup)
        result["canonical_url"] = self._extract_canonical(soup)

        if soup is not None:
            title_node = soup.select_one("h1.field--name-field-title") or soup.find("h1")
            if title_node:
                result["title"] = _clean_text(title_node.get_text(" ", strip=True))

            abstract = (
                _field_text(soup, ".field--name-field-summary-english")
                or _field_text(soup, ".field--name-field-summary")
            )
            result["abstract"] = abstract

            date_node = soup.select_one(".field--name-field-webopac-date time")
            if date_node:
                result["published_date_raw"] = date_node.get_text(" ", strip=True)
                result["published_date"] = (
                    _parse_iso_date(date_node.get("datetime", ""))
                    or _parse_dmy_date(result["published_date_raw"])
                )
            else:
                raw_date = _field_text(soup, ".field--name-field-webopac-date .field__item")
                result["published_date_raw"] = raw_date
                result["published_date"] = _parse_dmy_date(raw_date)

            doi_node = soup.select_one(".field--name-field-doi a[href*='doi.org']")
            doi_source = doi_node.get("href", "") if doi_node else html
            doi_match = re.search(r"10\.\d{4,9}/[^\s\"'<>]+", doi_source)
            if doi_match:
                result["doi"] = doi_match.group(0).rstrip(".,;)")

            pdf_link = (
                soup.select_one("a.cta-button--download-the-pdf[href]")
                or soup.select_one("a[href$='.pdf']")
            )
            if pdf_link:
                result["pdf_url"] = urljoin(self.base_url, pdf_link.get("href")).split("#", 1)[0]
                result["original_filename"] = _filename_from_url(result["pdf_url"])

            category_raw = _field_text(soup, ".field--name-dynamic-token-fieldpublication-publicatie")
            if category_raw:
                result["category_raw"] = category_raw
                result["category"] = category_raw

            programs = []
            for node in soup.select(".field--name-field-research-program a, .field--name-taxonomy-term-title a"):
                text = _clean_text(node.get_text(" ", strip=True))
                if text and text not in programs:
                    programs.append(text)
            result["research_programs"] = programs
            return result

        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.S)
        if title_match:
            result["title"] = _clean_text(re.sub(r"<[^>]+>", " ", title_match.group(1)))
        summary_match = re.search(
            r"field--name-field-summary(?:-english)?[^>]*>(.*?)</div>",
            html,
            flags=re.S,
        )
        if summary_match:
            result["abstract"] = _clean_text(re.sub(r"<br\s*/?>", "\n", summary_match.group(1), flags=re.I))
            result["abstract"] = _clean_text(re.sub(r"<[^>]+>", " ", result["abstract"]))
        doi_match = re.search(r"10\.\d{4,9}/[^\s\"'<>]+", html)
        if doi_match:
            result["doi"] = doi_match.group(0).rstrip(".,;)")
        pdf_match = re.search(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html, flags=re.I)
        if pdf_match:
            result["pdf_url"] = urljoin(self.base_url, unescape(pdf_match.group(1))).split("#", 1)[0]
            result["original_filename"] = _filename_from_url(result["pdf_url"])
        return result

    def _extract_node_id(self, html, soup):
        if soup is not None:
            node = soup.find(attrs={"data-history-node-id": True})
            if node is not None:
                node_id = node.get("data-history-node-id")
                if node_id:
                    return str(node_id)

            script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
            if script is not None:
                try:
                    settings = json.loads(script.get_text() or "{}")
                    current_path = (settings.get("path") or {}).get("currentPath") or ""
                    match = re.search(r"node/(\d+)", current_path)
                    if match:
                        return match.group(1)
                except json.JSONDecodeError:
                    pass

        match = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', html or "")
        if match:
            return match.group(1)
        match = re.search(r'data-history-node-id=["\'](\d+)["\']', html or "")
        if match:
            return match.group(1)
        return None

    def _extract_canonical(self, soup):
        if soup is None:
            return None
        link = soup.find("link", rel="canonical")
        href = link.get("href") if link else None
        if href and "pagina-niet-gevonden-404" not in href:
            return urljoin(self.base_url, href)
        return None
