# -*- coding: utf-8 -*-
"""Crawler for CNAM press communiques.

Start URL:
    https://presse.cnam.fr/communiques/

The start URL resolves to the K-Sup HTML listing:
    /communiques/tous-les-communiques-de-presse-1072.kjsp?RH=1648634270109

Each row links to a detail ``.kjsp`` page.  Detail pages expose a generated
PDF through the built-in ``toPdf=true`` action, which returns application/pdf
with a Content-Disposition filename.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.parse
import uuid
from typing import Any

from crawler.base_crawler import BaseCrawler


SITE_ID = "presse-cnam-fr-communiques"
START_URL = "https://presse.cnam.fr/communiques/"
CANONICAL_LIST_URL = (
    "https://presse.cnam.fr/communiques/"
    "tous-les-communiques-de-presse-1072.kjsp?RH=1648634270109"
)
MAX_PAGES = 200
WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
RETRY_DELAYS = (1, 3, 9)
MIN_ABSTRACT_CHARS = 50

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

FRENCH_MONTHS = {
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


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\x00", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def _make_soup(raw_html: str):
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw_html or "", parser)
        except Exception as exc:
            print(f"[{SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None

    text = _clean_text(raw)
    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso_match:
        return iso_match.group(0)

    numeric_match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text)
    if numeric_match:
        day, month, year = numeric_match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    folded = _fold_text(text)
    french_match = re.search(
        r"\b(\d{1,2})(?:er)?\s+"
        r"(janvier|fevrier|février|mars|avril|mai|juin|juillet|aout|août|"
        r"septembre|octobre|novembre|decembre|décembre)\s+(\d{4})\b",
        folded,
    )
    if french_match:
        day, month_name, year = french_match.groups()
        month = FRENCH_MONTHS.get(month_name)
        if month:
            return f"{year}-{month}-{int(day):02d}"

    ksup_match = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", text)
    if ksup_match:
        year, month, day = ksup_match.groups()
        return f"{year}-{month}-{day}"

    return None


def _absolute_url(url: str | None, base_url: str) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(base_url, html.unescape(url)).replace("&amp;", "&")


def _canonical_detail_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in query if k.lower() != "topdf"]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(filtered), "")
    )


def _node_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path
    match = re.search(r"-(\d+)\.kjsp$", path)
    return match.group(1) if match else None


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    stem = os.path.basename(urllib.parse.urlsplit(url).path)
    if stem.endswith(".kjsp"):
        stem = stem[:-5]
    return urllib.parse.unquote(stem) or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path
    filename = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return filename if filename and "." in filename and len(filename) <= 240 else None


def _filename_from_headers(header_text: str | None) -> str | None:
    if not header_text:
        return None
    for line in header_text.splitlines():
        if not line.lower().startswith("content-disposition:"):
            continue
        value = line.split(":", 1)[1].strip()
        match = re.search(r'filename\*?=(?:"([^"]+)"|([^;\r\n]+))', value, re.I)
        if match:
            filename = (match.group(1) or match.group(2) or "").strip()
            if filename.lower().startswith("utf-8''"):
                filename = filename[7:]
            return urllib.parse.unquote(filename.strip('"')) or None
    return None


def _text_from_element(element) -> str:
    if element is None:
        return ""
    for unwanted in element.select("script, style, noscript"):
        unwanted.decompose()
    return _clean_text(element.get_text(" ", strip=True))


def _meta_content(soup, names: tuple[str, ...]) -> str | None:
    if soup is None:
        return None
    for name in names:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_text(tag.get("content"))
        tag = soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            return _clean_text(tag.get("content"))
    return None


def _json_dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class PresseCnamFrCommuniquesCrawler(BaseCrawler):
    site_id = "presse-cnam-fr-communiques"
    site_name = "Custom: presse-cnam-fr-communiques"
    base_url = "https://presse.cnam.fr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else float("inf")
        limit_label = str(limit) if limit is not None else "inf"

        for page in range(1, MAX_PAGES + 1):
            if saved >= limit_or_inf:
                break
            if time.monotonic() - started_at >= WALL_CLOCK_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                break
            if page % 10 == 0 or page == 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_url = self._list_url(page)
            raw = self._curl_text(list_url)
            if not raw:
                print(f"[{self.site_id}] listing page {page} failed or empty; stopping")
                break

            items = self._parse_list(raw, list_url)
            new_items = []
            for item in items:
                url = item.get("url")
                if not url:
                    continue
                canonical_url = _canonical_detail_url(url)
                if canonical_url in seen_urls:
                    continue
                seen_urls.add(canonical_url)
                item["url"] = canonical_url
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page} had 0 new records; stopping")
                break

            for idx, item in enumerate(new_items, start=1):
                if saved >= limit_or_inf:
                    break
                if time.monotonic() - started_at >= WALL_CLOCK_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                    return saved

                item_label = item.get("external_id") or item.get("url") or f"page {page} item {idx}"
                try:
                    paper = self._fetch_and_parse_item(item)
                    if not paper:
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping {item_label}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

        else:
            print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached")

        return saved

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return START_URL
        separator = "&" if "?" in CANONICAL_LIST_URL else "?"
        return f"{CANONICAL_LIST_URL}{separator}page={page - 1}"

    def _curl_text(self, url: str, *, headers: bool = False) -> str | tuple[str, str]:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-H",
            f"User-Agent: {USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        ]
        if headers:
            cmd.append("-I")
        cmd.append(url)

        for attempt, wait in enumerate(RETRY_DELAYS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and stdout.strip():
                    if not headers:
                        return stdout
                    return self._last_header_block(stdout), ""
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: "
                    f"exit={result.returncode}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: {exc}")
            if attempt < len(RETRY_DELAYS):
                time.sleep(wait)

        return ("", "") if headers else ""

    def _last_header_block(self, stdout: str) -> str:
        # curl -I -L can emit multiple header blocks; use the final response.
        marker = "\r\n\r\n"
        if marker not in stdout:
            marker = "\n\n"
        parts = [part for part in stdout.split(marker) if part.strip()]
        return parts[-1] if parts else stdout

    def _parse_list(self, raw_html: str, list_url: str) -> list[dict[str, Any]]:
        soup = _make_soup(raw_html)
        if soup is None:
            return []

        records: list[dict[str, Any]] = []
        for li in soup.select("ul.objets.actualites li"):
            link = li.find("a", href=True)
            if not link:
                continue
            url = _absolute_url(link.get("href"), list_url)
            if not url:
                continue
            if "/communiques/" not in url and "/anciens-communiques-amelie/" not in url:
                continue
            if "tous-les-communiques-de-presse" in url:
                continue

            title = _clean_text(link.get_text(" ", strip=True))
            listed_raw = _text_from_element(li.select_one(".date"))
            listed_date = _parse_date(listed_raw)
            list_abstract = _text_from_element(li.select_one(".resume"))
            category = _text_from_element(li.select_one(".surtitre")) or "Communiqué de presse"
            node_id = _node_id_from_url(url)
            slug = _slug_from_url(url)
            external_id = node_id or slug or hashlib.sha1(url.encode("utf-8")).hexdigest()

            records.append(
                {
                    "url": url,
                    "external_id": external_id,
                    "node_id": node_id,
                    "slug": slug,
                    "post_number": node_id or slug,
                    "title": title,
                    "listed_date": listed_date,
                    "listed_date_raw": listed_raw,
                    "list_abstract": list_abstract,
                    "category": category,
                }
            )
        return records

    def _fetch_and_parse_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        url = item.get("url")
        if not url:
            return None
        raw = self._curl_text(url)
        if not raw:
            print(f"[{self.site_id}] failed to fetch detail {url} after 3 attempts; skipping")
            return None

        soup = _make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] failed to parse detail {url}; skipping")
            return None

        title = _text_from_element(soup.find("h1")) or item.get("title") or "(untitled)"
        detail_date_raw = self._extract_detail_date_raw(soup)
        published_date = (
            _parse_date(detail_date_raw)
            or _parse_date(_meta_content(soup, ("DC.Date.created", "Date-Creation-yyyymmdd")))
            or item.get("listed_date")
        )
        listed_date = item.get("listed_date") or published_date

        resume = _text_from_element(soup.select_one("#resume")) or item.get("list_abstract") or ""
        description = _text_from_element(soup.select_one("#description"))
        abstract = self._compose_abstract(resume, description)
        if len(abstract) < MIN_ABSTRACT_CHARS:
            abstract = self._compose_abstract(item.get("list_abstract") or "", description)

        pdf_url = self._extract_pdf_url(soup, url)
        original_filename = _filename_from_url(pdf_url)
        if pdf_url:
            header_text, _body = self._curl_text(pdf_url, headers=True)
            original_filename = _filename_from_headers(header_text) or original_filename

        authors = _meta_content(soup, ("author", "DC.Creator"))
        publisher = _meta_content(soup, ("DC.Publisher",)) or "Cnam"
        keywords = _meta_content(soup, ("keywords",)) or None
        category = item.get("category") or _meta_content(soup, ("category",)) or "Communiqué de presse"

        node_id = item.get("node_id") or _node_id_from_url(url)
        slug = item.get("slug") or _slug_from_url(url)
        external_id = item.get("external_id") or node_id or slug
        post_number = item.get("post_number") or node_id or slug
        if not external_id:
            external_id = hashlib.sha1(url.encode("utf-8")).hexdigest()

        metadata = {
            "posted_date": item.get("listed_date_raw") or listed_date,
            "listed_date": listed_date,
            "listed_date_raw": item.get("listed_date_raw"),
            "detail_date_raw": detail_date_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "slug": slug,
            "post_number": post_number,
            "source_list_url": START_URL,
            "canonical_list_url": CANONICAL_LIST_URL,
            "generated_pdf": bool(pdf_url),
            "dc_date_created": _meta_content(soup, ("DC.Date.created", "Date-Creation-yyyymmdd")),
            "dc_date_modified": _meta_content(soup, ("DC.Date.modified", "Date-Revision-yyyymmdd")),
            "dc_creator": _meta_content(soup, ("DC.Creator",)),
            "dc_publisher": _meta_content(soup, ("DC.Publisher",)),
            "category_raw": category,
            "list_abstract": item.get("list_abstract"),
        }

        return {
            "id": str(uuid.uuid4()),
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
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def _extract_detail_date_raw(self, soup) -> str | None:
        candidates = []
        for selector in (
            ".encadre_auto_fiche__actualite-1 h2.date",
            ".encadre_auto_fiche h2.date",
            "h2.date",
        ):
            candidates.extend(soup.select(selector))
        for candidate in candidates:
            text = _text_from_element(candidate)
            if _parse_date(text):
                return text
        return None

    def _compose_abstract(self, resume: str, description: str) -> str:
        parts = []
        for value in (resume, description):
            value = _clean_text(value)
            if value and value not in parts:
                parts.append(value)
        return "\n\n".join(parts)

    def _extract_pdf_url(self, soup, detail_url: str) -> str | None:
        for link in soup.select('li.actions-fiche__item--pdf a[href*="toPdf=true"]'):
            pdf_url = _absolute_url(link.get("href"), detail_url)
            if pdf_url:
                return pdf_url

        for link in soup.find_all("a", href=True):
            href = link.get("href") or ""
            if ".pdf" in href.lower():
                return _absolute_url(href, detail_url)

        separator = "&" if "?" in detail_url else "?"
        return f"{detail_url}{separator}toPdf=true"
