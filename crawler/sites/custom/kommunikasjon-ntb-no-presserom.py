# -*- coding: utf-8 -*-
"""Crawler for NTB Kommunikasjon pressroom: Veterinaerinstituttet releases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
from email.utils import parsedate_to_datetime
from typing import Any

from crawler.base_crawler import BaseCrawler


_BASE_URL = "https://kommunikasjon.ntb.no"
_PUBLISHER_ID = "17848243"
_CATEGORY_ID = "19"
_PAGE_SIZE = 20
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_CHARS = 50
_STARTING_URL = (
    "https://kommunikasjon.ntb.no/presserom/17848243/vetinst/r?categories=19"
)


try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except Exception:  # pragma: no cover - dependency is present in the crawler env.
    _BeautifulSoup = None


def _make_soup(raw: str):
    """Parse malformed HTML defensively, preferring html5lib."""
    if _BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BeautifulSoup(raw or "", parser)
        except Exception:
            continue
    return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(raw_html: Any) -> str:
    if not raw_html:
        return ""
    soup = _make_soup(str(raw_html))
    if soup is None:
        return _clean_text(re.sub(r"<[^>]+>", " ", str(raw_html)))
    return _clean_text(soup.get_text(" ", strip=True))


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(_BASE_URL, url)


def _localized_release_url(url: str | None) -> str | None:
    absolute = _absolute_url(url)
    if not absolute:
        return None
    parsed = urllib.parse.urlsplit(absolute)
    path = parsed.path
    if path.startswith("/release/"):
        path = "/pressemelding/" + path[len("/release/") :]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _iso_date(value: Any) -> str | None:
    """Return YYYY-MM-DD from ISO, RFC822, or displayed NTB date text."""
    if value is None:
        return None
    text = _clean_text(value)
    if not text:
        return None

    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)

    try:
        return parsedate_to_datetime(text).date().isoformat()
    except Exception:
        pass

    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    tail = urllib.parse.unquote(tail)
    if tail and "." in tail and len(tail) <= 240:
        return tail
    return None


class KommunikasjonNtbNoPresseromCrawler(BaseCrawler):
    site_id = "kommunikasjon-ntb-no-presserom"
    site_name = "Custom: kommunikasjon-ntb-no-presserom"
    base_url = "https://kommunikasjon.ntb.no"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_text(self, url: str, *, accept: str = "application/json,*/*;q=0.8") -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            "60",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: no-NO,no;q=0.9,en;q=0.7",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=75,
                    check=False,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr[:240]}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(waits):
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _curl_json(self, url: str) -> dict[str, Any] | None:
        raw = self._curl_text(url)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] JSON parse failed for {url}: {exc}")
            return None
        if not isinstance(parsed, dict):
            print(f"[{self.site_id}] JSON root is not an object for {url}")
            return None
        return parsed

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _list_api_url(self, page: int) -> str:
        return (
            f"{self.base_url}/public-website-api/pressroom/{_PUBLISHER_ID}"
            f"/releases/{_PAGE_SIZE}/{page}?categories={_CATEGORY_ID}"
        )

    def _detail_api_url(self, release_id: str) -> str:
        return f"{self.base_url}/public-website-api/release/{release_id}"

    def _parse_list_json(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        releases = payload.get("releases")
        if isinstance(releases, list):
            return [item for item in releases if isinstance(item, dict)]
        return []

    def _parse_list_html_fallback(self, html: str) -> list[dict[str, Any]]:
        soup = _make_soup(html)
        if soup is None:
            return []
        items: list[dict[str, Any]] = []
        for link in soup.find_all("a", href=True):
            href = link.get("href") or ""
            m = re.search(r"/pressemelding/(\d+)/", href)
            if not m:
                continue
            text = link.get_text(" | ", strip=True)
            items.append(
                {
                    "id": int(m.group(1)),
                    "date": None,
                    "versions": {
                        "no": {
                            "title": text.split("|", 1)[0].strip(),
                            "metadescription": "",
                            "type": "Pressemelding",
                            "url": href,
                        }
                    },
                }
            )
        return items

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _pick_version(self, release: dict[str, Any]) -> dict[str, Any]:
        versions = release.get("versions")
        if isinstance(versions, dict):
            if isinstance(versions.get("no"), dict):
                return versions["no"]
            for value in versions.values():
                if isinstance(value, dict):
                    return value
        return {}

    def _pick_pdf(self, version: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any] | None]:
        documents = version.get("documents")
        if not isinstance(documents, list):
            return None, None, None

        for doc in documents:
            if not isinstance(doc, dict):
                continue
            raw_url = (
                doc.get("downloadPath")
                or doc.get("downloadUrl")
                or doc.get("url")
                or doc.get("path")
            )
            pdf_url = _absolute_url(raw_url)
            filename = (
                doc.get("originalFilename")
                or doc.get("original_filename")
                or doc.get("filename")
                or doc.get("fileName")
                or doc.get("name")
                or _filename_from_url(pdf_url)
            )
            if filename:
                filename = urllib.parse.unquote(_clean_text(filename))
            return pdf_url, filename, doc
        return None, None, None

    def _build_paper(self, list_item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any] | None:
        release_id = str(detail.get("id") or list_item.get("id") or "").strip()
        if not release_id:
            return None

        version = self._pick_version(detail) or self._pick_version(list_item)
        title = _clean_text(version.get("title"))
        if not title:
            return None

        lead = _html_to_text(version.get("leadtext")) or _clean_text(version.get("metadescription"))
        body_html = ""
        body = version.get("body")
        if isinstance(body, dict):
            body_html = body.get("complete") or ""
            cookieless = body.get("cookieless")
            if not body_html and isinstance(cookieless, dict):
                body_html = cookieless.get("content") or ""
        body_text = _html_to_text(body_html)
        abstract = _clean_text(" ".join(part for part in (lead, body_text) if part))
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] abstract too short ({len(abstract)} chars) "
                f"for release {release_id}; skipping"
            )
            return None

        raw_date = detail.get("date") or list_item.get("date")
        published_date = _iso_date(raw_date)
        listed_date = published_date

        raw_url = detail.get("url") or version.get("url") or self._pick_version(list_item).get("url")
        meta_url = _localized_release_url(raw_url) or f"{self.base_url}/pressemelding/{release_id}"

        publisher_obj = detail.get("publisher") if isinstance(detail.get("publisher"), dict) else {}
        publisher = _clean_text(publisher_obj.get("name")) or "Veterinærinstituttet"

        contacts_obj = version.get("contacts") if isinstance(version.get("contacts"), dict) else {}
        contacts = contacts_obj.get("cards") if isinstance(contacts_obj.get("cards"), list) else []
        author_names = []
        departments = []
        for contact in contacts:
            if not isinstance(contact, dict):
                continue
            name = _clean_text(contact.get("name"))
            if name:
                author_names.append(name)
            dept = _clean_text(contact.get("company_department") or contact.get("title"))
            if dept:
                departments.append(dept)

        pdf_url, original_filename, pdf_doc = self._pick_pdf(version)
        keywords = _clean_text(version.get("keywords"))
        category = _clean_text(version.get("type")) or "Pressemelding"

        metadata = {
            "posted_date": raw_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "release_id": release_id,
            "publisher_id": _PUBLISHER_ID,
            "category_id": _CATEGORY_ID,
            "format": detail.get("format"),
            "localizedlabel": version.get("localizedlabel"),
            "list_item": list_item,
            "publisher": publisher_obj,
            "contacts": contacts,
            "leadimage": version.get("leadimage"),
            "images": version.get("images"),
            "documents": version.get("documents"),
            "links": version.get("links"),
        }
        if pdf_doc:
            metadata["pdf_document"] = pdf_doc

        return {
            "id": f"{self.site_id}:{release_id}",
            "site_id": self.site_id,
            "external_id": release_id,
            "post_number": release_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(dict.fromkeys(author_names)) or None,
            "publisher": publisher,
            "department": "; ".join(dict.fromkeys(departments)) or None,
            "journal": None,
            "url": meta_url,
            "pdf_url": pdf_url,
            "keywords": keywords or None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        try:
            for page in range(_MAX_PAGES):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] wall-clock limit reached; exiting cleanly")
                    break
                if page > 0 and page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                payload = self._curl_json(self._list_api_url(page))
                items: list[dict[str, Any]]
                if payload:
                    items = self._parse_list_json(payload)
                elif page == 0:
                    html = self._curl_text(
                        _STARTING_URL,
                        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    )
                    items = self._parse_list_html_fallback(html or "")
                else:
                    items = []

                if not items:
                    print(f"[{self.site_id}] page {page}: no records; stopping")
                    break

                new_on_page = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    item_id = str(item.get("id") or "").strip()
                    version = self._pick_version(item)
                    item_url = _localized_release_url(version.get("url")) or (
                        f"{self.base_url}/pressemelding/{item_id}" if item_id else None
                    )
                    if not item_url:
                        continue
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_on_page += 1

                    try:
                        if self._detail_delay:
                            time.sleep(self._detail_delay)
                        detail = self._curl_json(self._detail_api_url(item_id)) if item_id else None
                        if not detail:
                            print(f"[{self.site_id}] item {item_id or item_url} failed: missing detail JSON")
                            continue
                        paper = self._build_paper(item, detail)
                        if not paper:
                            continue
                        self._save_paper(paper)
                        saved += 1
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_id or item_url} failed: {exc}")
                        continue

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                    break
            else:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")
        except KeyboardInterrupt:
            raise

        return saved
