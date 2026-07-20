# -*- coding: utf-8 -*-
"""Crawler for Academie des sciences espace presse.

List source:
    https://www.academie-sciences.fr/espace-presse

The page is a Drupal 10 ``press_area`` view.  The browser uses the same
view through ``/views/ajax``, but the paged HTML endpoint is more stable
for crawling:

    page 1: https://www.academie-sciences.fr/espace-presse
    page 2: https://www.academie-sciences.fr/espace-presse?page=1

Each list row links directly to a PDF, which is the site's detail/download
record for the press item.  ``pdftotext`` is used to extract the body text
for the abstract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import uuid
from pathlib import Path

# absolute import -- spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler


_SITE_ID = "academie-sciences-fr-espace-presse"
_BASE_URL = "https://www.academie-sciences.fr"
_START_URL = f"{_BASE_URL}/espace-presse"
_VIEW_AJAX_URL = f"{_BASE_URL}/views/ajax"
_VIEW_NAME = "press_area"
_VIEW_DISPLAY_ID = "page_1"
_MAX_PAGES = 200
_BUDGET_SECONDS = 25 * 60
_RETRY_DELAYS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 50
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_MONTHS = {
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


def _json_dumps(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _clean_text(value):
    if value is None:
        return ""
    value = str(value).replace("\x00", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _fold_text(value):
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return ascii_text.lower()


def _make_soup(raw_html):
    """Parse HTML with html5lib -> lxml -> html.parser fallback chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw_html or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
            continue
    return None


def _date_from_iso(raw):
    if not raw:
        return None
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", str(raw))
    return match.group(0) if match else None


def _date_from_numeric(raw):
    if not raw:
        return None
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", str(raw))
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _date_from_french(raw):
    if not raw:
        return None
    folded = _fold_text(raw)
    match = re.search(
        r"\b(\d{1,2})(?:er)?\s+"
        r"(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|"
        r"octobre|novembre|decembre)\s+(\d{4})\b",
        folded,
    )
    if not match:
        return None
    month = _MONTHS.get(match.group(2))
    if not month:
        return None
    return f"{match.group(3)}-{month}-{int(match.group(1)):02d}"


def _parse_date(raw):
    return _date_from_iso(raw) or _date_from_numeric(raw) or _date_from_french(raw)


def _filename_from_url(url):
    path = urllib.parse.urlsplit(url or "").path
    filename = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return filename or None


def _slug_from_filename(filename):
    stem = os.path.splitext(filename or "")[0]
    folded = _fold_text(stem)
    folded = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return folded or stem or None


def _normalize_url(url):
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/._-()")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


def _external_id_from_url(url):
    parsed = urllib.parse.urlsplit(url or "")
    path = urllib.parse.unquote(parsed.path).strip("/")
    return path or hashlib.sha1((url or "").encode("utf-8")).hexdigest()


def _title_without_list_date(link):
    raw = _clean_text(link.get_text(" ", strip=True))
    time_el = link.find("time")
    if time_el:
        pieces = [
            time_el.get_text(" ", strip=True),
            time_el.get("datetime") or "",
            time_el.get("title") or "",
        ]
        for piece in pieces:
            piece = _clean_text(piece)
            if piece:
                raw = raw.replace(piece, " ")
    raw = re.sub(r"\s*-\s*$", "", raw)
    raw = re.sub(r"\s+-\s+", " ", raw)
    raw = _clean_text(raw)
    if raw.lower().endswith(".pdf"):
        raw = raw[:-4].strip()
    return raw


def _looks_like_noise_line(line):
    folded = _fold_text(line)
    if not folded or len(folded) <= 2:
        return True
    if folded in {"communique de presse", "dossier de presse"}:
        return True
    if _parse_date(line) and len(line) <= 32:
        return True
    if line.lstrip().startswith(("\u00a9", "(c)")):
        return True
    noise_fragments = (
        "academie-sciences.fr",
        "quai de conti",
        "contact presse",
        "contacts presse",
        "agenda interne",
        "l'academie des sciences fournit un cadre",
        "l academie des sciences fournit un cadre",
        "les evenements de l'academie",
        "inscription newsletter",
    )
    if any(fragment in folded for fragment in noise_fragments):
        return True
    if re.fullmatch(r"[\d\s./:+-]+", folded):
        return True
    if re.search(r"\b0\d(?:[ .-]?\d{2}){4}\b", folded):
        return True
    return False


def _pdf_title_from_text(text, fallback):
    lines = [_clean_text(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    candidates = []
    for line in lines[:50]:
        folded = _fold_text(line)
        if _looks_like_noise_line(line):
            continue
        if "communique de presse" in folded or "dossier de presse" in folded:
            continue
        candidates.append(line)
        joined = _clean_text(" ".join(candidates))
        if len(joined) >= 80 or len(candidates) >= 3:
            break
    title = _clean_text(" ".join(candidates))
    if 12 <= len(title) <= 300:
        return title
    return fallback


def _abstract_from_pdf_text(text, title):
    lines = [_clean_text(line) for line in (text or "").splitlines()]
    title_folded = _fold_text(title)
    body = []
    started = False
    for line in lines:
        if not line:
            continue
        folded = _fold_text(line)
        if started and (
            "contact presse" in folded
            or "contacts presse" in folded
            or "agenda interne" in folded
            or "l academie des sciences fournit un cadre" in folded
            or "l'academie des sciences fournit un cadre" in folded
        ):
            break
        if _looks_like_noise_line(line):
            continue
        if title_folded and folded and folded in title_folded:
            continue
        if not started:
            if len(line) < 45:
                continue
            started = True
        body.append(line)

    abstract = _clean_text(" ".join(body))
    if len(abstract) >= _MIN_ABSTRACT_CHARS:
        return abstract

    fallback_lines = [
        line for line in lines
        if line and not _looks_like_noise_line(line)
    ]
    return _clean_text(" ".join(fallback_lines))


class AcademieSciencesFrEspacePresseCrawler(BaseCrawler):
    site_id = "academie-sciences-fr-espace-presse"
    site_name = "Custom: academie-sciences-fr-espace-presse"
    base_url = "https://www.academie-sciences.fr"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        started_at = time.time()

        if limit is not None and limit <= 0:
            return 0

        for page_num in range(1, _MAX_PAGES + 1):
            if time.time() - started_at >= _BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget approaching at page {page_num}; stopping")
                break
            if limit is not None and saved >= limit:
                break

            query_page = page_num - 1
            list_url = self._list_url(query_page)
            raw = self._fetch_text(list_url, accept="text/html,*/*", context=f"list page {page_num}")
            if not raw:
                print(f"[{self.site_id}] list page {page_num} failed; stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page_num} unparseable; stopping")
                break

            items, has_next = self._parse_list_items(soup, page_num, list_url)
            if not items:
                print(f"[{self.site_id}] page {page_num}: no records; stopping")
                break

            new_on_page = 0
            for item_index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - started_at >= _BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget approaching during page {page_num}; stopping")
                    return saved

                item_url = item["pdf_url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    if self._process_item(item):
                        saved += 1
                    time.sleep(self.detail_delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page_num}.{item_index} failed: {exc}")
                    continue

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_num}: no new items; stopping")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page_num}: last page reached")
                break
        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

        return saved

    def _list_url(self, query_page):
        if query_page <= 0:
            return _START_URL
        return f"{_START_URL}?{urllib.parse.urlencode({'page': query_page})}"

    def _fetch_bytes(self, url, *, accept=None, context=None, max_time=60):
        headers = [
            "-H",
            f"User-Agent: {_USER_AGENT}",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        ]
        if accept:
            headers.extend(["-H", f"Accept: {accept}"])

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(max_time),
            *headers,
            url,
        ]

        label = context or url
        for attempt, wait in enumerate(_RETRY_DELAYS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for "
                    f"{label}: rc={result.returncode} {stderr[:200]}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt}/3 failed for {label}: {exc}")

            if attempt < len(_RETRY_DELAYS):
                time.sleep(wait)

        return None

    def _fetch_text(self, url, *, accept=None, context=None, max_time=60):
        raw = self._fetch_bytes(url, accept=accept, context=context, max_time=max_time)
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")

    def _parse_list_items(self, soup, page_num, list_url):
        view = None
        for candidate in soup.select("div.NodeEdito-view"):
            heading = candidate.select_one(".NodeEdito-view-title")
            if heading and "communique de presse" in _fold_text(heading.get_text(" ", strip=True)):
                view = candidate
                break
        root = view or soup
        rows = root.select("div.views-row")
        items = []

        for row in rows:
            link = row.select_one("h4.ParagraphAccordion-item-title a[href]")
            if not link:
                continue
            href = link.get("href") or ""
            full_url = _normalize_url(urllib.parse.urljoin(self.base_url, href))
            if ".pdf" not in urllib.parse.urlsplit(full_url).path.lower():
                continue

            time_el = link.find("time") or row.find("time")
            listed_datetime = time_el.get("datetime") if time_el else None
            listed_raw = time_el.get_text(" ", strip=True) if time_el else None
            listed_date = _parse_date(listed_datetime) or _parse_date(listed_raw)
            filename = _filename_from_url(full_url)
            list_title = _title_without_list_date(link) or os.path.splitext(filename or "")[0]

            items.append({
                "list_page": page_num,
                "list_url": list_url,
                "title": list_title,
                "pdf_url": full_url,
                "listed_date": listed_date,
                "posted_date_raw": listed_raw,
                "listed_datetime": listed_datetime,
                "original_filename": filename,
                "filename_slug": _slug_from_filename(filename),
            })

        pager = root.select_one('ul[data-drupal-views-infinite-scroll-pager] a[rel="next"]')
        has_next = bool(pager and pager.get("href"))
        return items, has_next

    def _process_item(self, item):
        pdf_url = item["pdf_url"]
        pdf_bytes = self._fetch_bytes(
            pdf_url,
            accept="application/pdf,*/*",
            context=f"detail {item.get('original_filename') or pdf_url}",
            max_time=90,
        )
        if not pdf_bytes:
            print(f"[{self.site_id}] item {pdf_url} failed: PDF fetch returned no body")
            return False

        pdf_text = self._extract_pdf_text(pdf_bytes, pdf_url)
        title = _pdf_title_from_text(pdf_text, item.get("title"))
        abstract = _abstract_from_pdf_text(pdf_text, title)
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skip short abstract {len(abstract)} chars: {pdf_url}")
            return False

        published_date = _date_from_french(pdf_text[:2500]) or item.get("listed_date")
        listed_date = item.get("listed_date")
        external_id = _external_id_from_url(pdf_url)
        post_number = item.get("filename_slug") or external_id
        original_filename = item.get("original_filename")
        category = self._category_from_title(title, original_filename)

        metadata = {
            "source_endpoint": item.get("list_url"),
            "detail_endpoint": pdf_url,
            "list_api_endpoint": _VIEW_AJAX_URL,
            "view_name": _VIEW_NAME,
            "view_display_id": _VIEW_DISPLAY_ID,
            "list_page": item.get("list_page"),
            "listed_date": listed_date,
            "listed_datetime": item.get("listed_datetime"),
            "posted_date": item.get("posted_date_raw"),
            "published_date_raw": _date_from_french(pdf_text[:2500]),
            "originalFilename": original_filename,
            "original_filename": original_filename,
            "filename_slug": item.get("filename_slug"),
            "post_number": post_number,
            "pdf_url": pdf_url,
            "file_path": urllib.parse.unquote(urllib.parse.urlsplit(pdf_url).path),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "abstract_source": "pdftotext",
            "raw_list_title": item.get("title"),
        }

        paper = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, pdf_url)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": "Academie des sciences",
            "department": "Service communication",
            "journal": None,
            "url": pdf_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }
        self._save_paper(paper)
        return True

    def _extract_pdf_text(self, pdf_bytes, url):
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", "-", "-"],
                input=pdf_bytes,
                capture_output=True,
                timeout=90,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext failed for {url}: {exc}")
            return ""

        text = result.stdout.decode("utf-8", errors="replace")
        if result.returncode != 0 and not text:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            print(f"[{self.site_id}] pdftotext failed for {url}: rc={result.returncode} {stderr[:200]}")
        return text

    def _category_from_title(self, title, filename):
        folded = _fold_text(" ".join([title or "", filename or ""]))
        if "dossier de presse" in folded:
            return "Dossier de presse"
        return "Communique de presse"
