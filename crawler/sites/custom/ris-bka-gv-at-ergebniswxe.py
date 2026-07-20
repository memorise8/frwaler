# -*- coding: utf-8 -*-
"""Crawler for RIS Landesrecht Ergebnis.wxe result pages.

Discovered public endpoints:
  - list/detail list: https://www.ris.bka.gv.at/Ergebnis.wxe?...&Position=N
  - detail metadata/text: https://www.ris.bka.gv.at/eli/lgbl/.../{document_id}
    (server renders through Dokument.wxe)
  - source documents: /Dokumente/Landesnormen/{document_id}/{document_id}.html|rtf|pdf

Absolute imports are intentional: this module is loaded through
spec_from_file_location without package context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse, urlunparse

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - only used if bs4 is unavailable
    BeautifulSoup = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_SITE_ID = "ris-bka-gv-at-ergebniswxe"
_BASE_URL = "https://www.ris.bka.gv.at"
_START_URL = (
    "https://www.ris.bka.gv.at/Ergebnis.wxe?"
    "Abfrage=Landesnormen&Kundmachungsorgan=&Bundesland=Undefined&"
    "BundeslandDefault=Undefined&Index=&Titel=&Gesetzesnummer=&"
    "VonArtikel=&BisArtikel=&VonParagraf=&BisParagraf=&VonAnlage=&"
    "BisAnlage=&Typ=&Kundmachungsnummer=&Unterzeichnungsdatum=&"
    "FassungVom=04.08.2025&VonInkrafttretedatum=&BisInkrafttretedatum=&"
    "VonAusserkrafttretedatum=&BisAusserkrafttretedatum=&"
    "NormabschnittnummerKombination=Und&ImRisSeitVonDatum=&"
    "ImRisSeitBisDatum=&ImRisSeit=Undefined&ResultPageSize=100&"
    "Suchworte=&Position=1&SkipToDocumentPage=true"
)
_MAX_PAGES = 200
_WALL_BUDGET_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 50

_LABEL_KEYS = {
    "bundesland": "bundesland",
    "kurztitel": "short_title",
    "kundmachungsorgan": "promulgation_organ",
    "typ": "type",
    "§/artikel/anlage": "section",
    "§/art./anl.": "section",
    "inkrafttretensdatum": "effective_date",
    "ausserkrafttretensdatum": "expiry_date",
    "außerkrafttretensdatum": "expiry_date",
    "index": "index",
    "titel": "long_title",
    "langtitel": "long_title",
    "präambel/promulgationsklausel": "preamble",
    "praeambel/promulgationsklausel": "preamble",
    "text": "text",
    "im ris seit": "posted_date",
    "zuletzt aktualisiert am": "last_updated_date",
    "gesetzesnummer": "law_number",
    "dokumentnummer": "document_number",
    "european legislation identifier (eli)": "eli",
}


def _clean_text(value):
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _make_soup(raw):
    """Build BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    if not raw or BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _remove_noise(soup):
    if soup is None:
        return
    for tag in soup(["script", "style", "noscript"]):
        try:
            tag.decompose()
        except Exception:
            pass
    for tag in soup.find_all(class_=lambda c: c and (
        "sr-only" in c or "onlyScreenreader" in c
    )):
        try:
            tag.decompose()
        except Exception:
            pass


def _absolute_url(href):
    href = _clean_text(href)
    if not href:
        return None
    return urljoin(_BASE_URL, href)


def _stable_detail_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.path.startswith("/eli/"):
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def _parse_ris_date(raw):
    raw = _clean_text(raw)
    if not raw:
        return None
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _filename_from_url(url):
    if not url:
        return None
    tail = os.path.basename(urlparse(url).path)
    tail = unquote(tail or "").strip()
    return tail or None


def _extract_external_id(*values):
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        match = re.search(r"([A-Z]{2,}\d{5,})", text, re.I)
        if match:
            return match.group(1).upper()
    return None


def _post_number_from_external_id(external_id):
    if not external_id:
        return None
    match = re.search(r"(\d+)$", external_id)
    if match:
        return match.group(1)
    return external_id


def _trim_abstract(text, max_chars=3000):
    text = _clean_text(text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "..."


class RisBkaGvAtErgebniswxeCrawler(BaseCrawler):
    site_id = "ris-bka-gv-at-ergebniswxe"
    site_name = "Custom: ris-bka-gv-at-ergebniswxe"
    base_url = "https://www.ris.bka.gv.at"

    detail_delay = 1.0

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        started_at = time.time()
        saved = 0
        page = 1
        current_url = _START_URL
        seen_urls = set()
        limit_label = limit if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at > _WALL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            raw = self._curl_get(current_url, accept="text/html, */*")
            if not raw:
                print(f"[{self.site_id}] page {page}: list fetch failed; stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] page {page}: HTML parse failed; stopping")
                break
            _remove_noise(soup)

            rows = self._parse_list_rows(soup)
            if not rows:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            new_urls_on_page = 0
            for item in rows:
                if limit is not None and saved >= limit:
                    break
                if time.time() - started_at > _WALL_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    break

                detail_url = item.get("url")
                dedupe_url = detail_url or item.get("external_id")
                if not dedupe_url or dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                new_urls_on_page += 1

                item_label = item.get("external_id") or item.get("row_number") or detail_url
                try:
                    time.sleep(float(getattr(self, "detail_delay", 1.0)))
                    detail_raw = self._curl_get(detail_url, accept="text/html, */*")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_label} failed: detail fetch failed")
                        continue
                    detail = self._parse_detail_page(detail_raw, item)
                    paper = self._build_paper(item, detail)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
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

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping to avoid pagination loop")
                break

            next_url = self._find_next_page_url(soup)
            if not next_url:
                print(f"[{self.site_id}] page {page}: next page absent; done")
                break

            current_url = next_url
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, accept="text/html, */*", retries=3, max_time=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: de-AT,de;q=0.9,en;q=0.8",
            url,
        ]
        waits = (1, 3, 9)
        last_error = None
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode == 0 and stdout.strip():
                    return stdout
                last_error = stderr.strip() or f"curl returned {result.returncode}"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)

            if attempt < retries - 1:
                print(
                    f"[{self.site_id}] network error for {url}: {last_error}; "
                    f"retrying in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] network failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list_rows(self, soup):
        rows = []
        table_host = soup.find(id="MainContent_DocumentsList") or soup
        for tr in table_host.find_all("tr", class_=lambda c: c and "bocListDataRow" in c):
            cells = tr.find_all("td")
            if len(cells) < 7:
                continue

            section_link = cells[2].find("a", href=True)
            if not section_link:
                continue

            detail_url = _stable_detail_url(_absolute_url(section_link.get("href")))
            external_id = _extract_external_id(detail_url)
            row_number = _clean_text(cells[0].get_text(" ", strip=True))
            section = _clean_text(section_link.get_text(" ", strip=True))
            title = _clean_text(
                section_link.get("title")
                or cells[6].get_text(" ", strip=True)
            )
            effective_raw = _clean_text(cells[3].get_text(" ", strip=True))
            expiry_raw = _clean_text(cells[4].get_text(" ", strip=True))
            state = _clean_text(cells[5].get_text(" ", strip=True))
            short_info = _clean_text(cells[6].get_text(" ", strip=True))

            html_url = None
            rtf_url = None
            pdf_url = None
            for link in cells[7].find_all("a", href=True):
                href = _absolute_url(link.get("href"))
                lower = (href or "").lower()
                if lower.endswith(".html"):
                    html_url = href
                elif lower.endswith(".rtf"):
                    rtf_url = href
                elif lower.endswith(".pdf"):
                    pdf_url = href

            if not external_id:
                external_id = _extract_external_id(pdf_url, html_url, rtf_url)
            if not detail_url and external_id:
                detail_url = f"{self.base_url}/Dokument.wxe?Dokumentnummer={external_id}&Abfrage=Landesnormen"
            if not detail_url:
                continue

            rows.append({
                "row_number": row_number,
                "section": section,
                "title": title or short_info,
                "short_info": short_info,
                "effective_date_raw": effective_raw,
                "effective_date": _parse_ris_date(effective_raw),
                "expiry_date_raw": expiry_raw,
                "expiry_date": _parse_ris_date(expiry_raw),
                "state": state,
                "external_id": external_id,
                "post_number": _post_number_from_external_id(external_id),
                "url": detail_url,
                "html_url": html_url,
                "rtf_url": rtf_url,
                "pdf_url": pdf_url,
                "original_filename": _filename_from_url(pdf_url),
            })
        return rows

    def _find_next_page_url(self, soup):
        for link_id in ("PagingBottomControl_NextPageLink", "PagingTopControl_NextPageLink"):
            link = soup.find("a", id=link_id, href=True)
            if link is not None:
                return _absolute_url(link.get("href"))
        for link in soup.find_all("a", href=True):
            text = _clean_text(link.get_text(" ", strip=True)).lower()
            title = _clean_text(link.get("title")).lower()
            if "nächste seite" in text or "naechste seite" in text or "nächste seite" in title:
                return _absolute_url(link.get("href"))
        return None

    # ------------------------------------------------------------------
    # Detail parsing and mapping
    # ------------------------------------------------------------------

    def _parse_detail_page(self, raw, list_item):
        soup = _make_soup(raw)
        if soup is None:
            return {}
        _remove_noise(soup)

        fields = {}
        for heading in soup.find_all(["h1", "h2", "h3"]):
            label = _clean_text(heading.get_text(" ", strip=True))
            key = self._label_key(label)
            if not key:
                continue
            parent = heading.parent
            value = self._value_from_heading_parent(heading, parent, label)
            if value:
                fields[key] = value

        for link in soup.find_all("a", href=True):
            href = _absolute_url(link.get("href"))
            lower = (href or "").lower()
            if lower.endswith(".html") and "/dokumente/landesnormen/" in lower:
                fields.setdefault("html_url", href)
            elif lower.endswith(".rtf") and "/dokumente/landesnormen/" in lower:
                fields.setdefault("rtf_url", href)
            elif lower.endswith(".pdf") and "/dokumente/landesnormen/" in lower:
                fields.setdefault("pdf_url", href)
            elif "/eli/" in lower and "lgbl" in lower:
                fields.setdefault("eli", _stable_detail_url(href))

        if not fields.get("document_number"):
            fields["document_number"] = _extract_external_id(raw, list_item.get("url"))
        if not fields.get("eli"):
            fields["eli"] = list_item.get("url")
        return fields

    def _label_key(self, label):
        label_norm = _clean_text(label).lower()
        label_norm = label_norm.replace("paragraph/artikel/anlage", "§/artikel/anlage")
        return _LABEL_KEYS.get(label_norm)

    def _value_from_heading_parent(self, heading, parent, label):
        if parent is None:
            return ""
        text = _clean_text(parent.get_text(" ", strip=True))
        label_text = _clean_text(label)
        if text.lower().startswith(label_text.lower()):
            text = text[len(label_text):]
        text = _clean_text(text.strip(" :-"))
        return text

    def _build_paper(self, item, detail):
        external_id = (
            _clean_text(detail.get("document_number"))
            or item.get("external_id")
            or _extract_external_id(item.get("url"), item.get("pdf_url"))
        )
        if not external_id:
            external_id = _stable_detail_url(item.get("url"))

        section = detail.get("section") or item.get("section")
        short_title = detail.get("short_title") or item.get("short_info") or item.get("title")
        long_title = detail.get("long_title")
        state = detail.get("bundesland") or item.get("state")
        effective_raw = detail.get("effective_date") or item.get("effective_date_raw")
        effective_date = _parse_ris_date(effective_raw) or item.get("effective_date")
        expiry_raw = detail.get("expiry_date") or item.get("expiry_date_raw")
        expiry_date = _parse_ris_date(expiry_raw) or item.get("expiry_date")
        posted_raw = detail.get("posted_date")
        posted_date = _parse_ris_date(posted_raw)
        last_updated_raw = detail.get("last_updated_date")
        last_updated_date = _parse_ris_date(last_updated_raw)

        title = self._compose_title(short_title, section, long_title)
        pdf_url = detail.get("pdf_url") or item.get("pdf_url")
        html_url = detail.get("html_url") or item.get("html_url")
        original_filename = _filename_from_url(pdf_url) or item.get("original_filename")
        post_number = _post_number_from_external_id(external_id)

        metadata = {
            "posted_date": posted_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "document_number": external_id,
            "post_number": post_number,
            "row_number": item.get("row_number"),
            "section": section,
            "short_title": short_title,
            "long_title": long_title,
            "bundesland": state,
            "kundmachungsorgan": detail.get("promulgation_organ"),
            "type": detail.get("type"),
            "index": detail.get("index"),
            "effective_date_raw": effective_raw,
            "expiry_date_raw": expiry_raw,
            "last_updated_raw": last_updated_raw,
            "last_updated_date": last_updated_date,
            "law_number": detail.get("law_number"),
            "eli": detail.get("eli") or item.get("url"),
            "html_url": html_url,
            "rtf_url": detail.get("rtf_url") or item.get("rtf_url"),
            "pdf_url": pdf_url,
            "list_url": item.get("url"),
            "list_title": item.get("title"),
            "list_short_info": item.get("short_info"),
        }

        category_parts = ["Landesrecht konsolidiert"]
        if state:
            category_parts.append(state)
        category = " - ".join(category_parts)

        keywords = self._join_keywords([
            "Landesnormen",
            state,
            detail.get("type"),
            detail.get("promulgation_organ"),
            detail.get("index"),
            short_title,
        ])
        abstract = self._build_abstract(title, section, detail, state, effective_date)

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": effective_date,
            "listed_date": posted_date,
            "posted_date": posted_date,
            "authors": None,
            "publisher": self._join_publishers(["Rechtsinformationssystem des Bundes", state]),
            "department": state,
            "journal": None,
            "url": detail.get("eli") or item.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _compose_title(self, short_title, section, long_title):
        title = _clean_text(short_title or long_title or "RIS Landesnorm")
        section = _clean_text(section)
        if section and section not in title:
            return f"{title} - {section}"
        return title

    def _build_abstract(self, title, section, detail, state, effective_date):
        parts = []
        long_title = detail.get("long_title")
        preamble = detail.get("preamble")
        text = detail.get("text")

        context = [
            f"Kurztitel: {title}" if title else None,
            f"Bundesland: {state}" if state else None,
            f"Kundmachungsorgan: {detail.get('promulgation_organ')}"
            if detail.get("promulgation_organ") else None,
            f"Normabschnitt: {section}" if section else None,
            f"Inkrafttretensdatum: {effective_date}" if effective_date else None,
            f"Index: {detail.get('index')}" if detail.get("index") else None,
        ]
        context = [p for p in context if p]
        if context:
            parts.append("; ".join(context) + ".")
        if long_title:
            parts.append(f"Titel: {long_title}.")
        if preamble:
            parts.append(f"Präambel/Promulgationsklausel: {preamble}.")
        if text:
            parts.append(f"Text: {text}.")

        abstract = _trim_abstract(" ".join(parts))
        if len(abstract) < 100:
            fallback = [
                abstract,
                f"Dokumentnummer: {detail.get('document_number')}"
                if detail.get("document_number") else None,
                f"Gesetzesnummer: {detail.get('law_number')}"
                if detail.get("law_number") else None,
                f"Im RIS seit: {_parse_ris_date(detail.get('posted_date')) or detail.get('posted_date')}"
                if detail.get("posted_date") else None,
            ]
            abstract = _trim_abstract(". ".join([p for p in fallback if p]))
        return abstract

    def _join_keywords(self, values):
        seen = set()
        out = []
        for value in values:
            value = _clean_text(value)
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
        return ", ".join(out) if out else None

    def _join_publishers(self, values):
        seen = set()
        out = []
        for value in values:
            value = _clean_text(value)
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
        return "; ".join(out) if out else None
