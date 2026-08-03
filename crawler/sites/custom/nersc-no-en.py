# -*- coding: utf-8 -*-
"""NERSC English publications crawler.

Starting page: https://nersc.no/en/publications/

The NERSC page renders publications from the Norwegian Cristin/NVA data
services. The list endpoint is Cristin v2 filtered to NERSC institution 7444;
the per-record detail endpoint is NVA's public publication endpoint.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import unquote, urlparse

from crawler.base_crawler import BaseCrawler


_START_URL = "https://nersc.no/en/publications/"
_CRISTIN_LIST_API = "https://api.cristin.no/v2/results"
_NVA_PUBLICATION_API = "https://api.nva.unit.no/publication"
_NVA_REGISTRATION_URL = "https://nva.sikt.no/registration"
_CROSSREF_API = "https://api.crossref.org/works"
_INSTITUTION_ID = "7444"
_PAGE_SIZE = 50
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_WAITS = (1, 3, 9)
_BS_PARSERS = ("html5lib", "lxml", "html.parser")
_MIN_ABSTRACT_CHARS = 100


def _clean_text(value):
    if value is None:
        return ""
    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _lang_value(value):
    """Return the best human text from Cristin/NVA language maps."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        for key in ("en", "nb", "nn", "no"):
            if value.get(key):
                return _clean_text(value.get(key))
        for candidate in value.values():
            text = _lang_value(candidate)
            if text:
                return text
    return _clean_text(value)


def _iso_date(value):
    if not value:
        return None
    if isinstance(value, dict):
        if value.get("date"):
            return _iso_date(value.get("date"))
        year = str(value.get("year") or "").strip()
        if year:
            month = str(value.get("month") or "1").zfill(2)
            day = str(value.get("day") or "1").zfill(2)
            return f"{year}-{month}-{day}"
    text = str(value).strip()
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    if match:
        return f"{match.group(1)}-01-01"
    return None


def _filename_from_url(url):
    if not url:
        return None
    name = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    if name and "." in name and len(name) <= 240:
        return name
    return None


def _normalize_doi(value):
    if not value:
        return None
    text = unquote(str(value).strip())
    match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", text, re.I)
    if not match:
        return None
    return match.group(1).rstrip(").,;")


def _join_unique(values, sep="; "):
    seen = set()
    out = []
    for value in values:
        text = _clean_text(value)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return sep.join(out) if out else None


def _make_soup(raw):
    """BeautifulSoup construction with the required parser fallback chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None

    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


class NerscNoEnCrawler(BaseCrawler):
    site_id = "nersc-no-en"
    site_name = "Custom: nersc-no-en"
    base_url = "https://nersc.no"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_bytes(
        self,
        url,
        *,
        accept="application/json",
        referer=None,
        max_time=30,
        retries=3,
    ):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                )
                body = result.stdout or b""
                if result.returncode == 0 and body.strip():
                    return body
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = stderr or f"empty response (curl rc={result.returncode})"
            except Exception as exc:
                last_error = str(exc)

            if attempt < retries - 1:
                wait = _RETRY_WAITS[attempt]
                print(
                    f"[nersc-no-en] curl failed for {url} "
                    f"(attempt {attempt + 1}/{retries}): {last_error}; retry in {wait}s"
                )
                time.sleep(wait)

        print(f"[nersc-no-en] curl failed after {retries} attempts for {url}: {last_error}")
        return None

    def _get_json(self, url, *, referer=None, retries=3):
        raw = self._curl_bytes(url, referer=referer, retries=retries)
        if raw is None:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            print(f"[nersc-no-en] JSON parse failed for {url}: {exc}")
            return None

    def _get_html(self, url, *, retries=3):
        raw = self._curl_bytes(
            url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            retries=retries,
        )
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")

    def _head_filename(self, url):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skLI",
            "--max-time",
            "20",
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: */*",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                headers = result.stdout.decode("utf-8", errors="replace")
                match = re.search(
                    r"content-disposition:.*?filename\*?=(?:UTF-8''|\"?)([^\";\r\n]+)",
                    headers,
                    re.I,
                )
                if match:
                    return unquote(match.group(1).strip())
                if result.returncode == 0:
                    return None
            except Exception:
                pass
            if attempt < 2:
                time.sleep(_RETRY_WAITS[attempt])
        return None

    # ------------------------------------------------------------------
    # Endpoint helpers
    # ------------------------------------------------------------------

    def _list_url(self, page):
        return (
            f"{_CRISTIN_LIST_API}?institution={_INSTITUTION_ID}"
            f"&lang=en&per_page={_PAGE_SIZE}&page={page}&fields=all"
        )

    def _fetch_list_page(self, page):
        data = self._get_json(self._list_url(page), referer=_START_URL)
        if isinstance(data, list):
            return data
        return None

    def _fetch_nva_detail(self, nva_id):
        if not nva_id:
            return None
        data = self._get_json(f"{_NVA_PUBLICATION_API}/{nva_id}", referer=_START_URL)
        return data if isinstance(data, dict) else None

    def _fetch_cristin_detail(self, cristin_id):
        if not cristin_id:
            return None
        data = self._get_json(f"{_CRISTIN_LIST_API}/{cristin_id}", referer=_START_URL)
        return data if isinstance(data, dict) else None

    def _fetch_crossref_abstract(self, doi):
        doi = _normalize_doi(doi)
        if not doi:
            return ""
        url = f"{_CROSSREF_API}/{doi}"
        raw = self._curl_bytes(
            url,
            accept="application/json",
            referer=_START_URL,
            max_time=20,
            retries=3,
        )
        if raw is None:
            return ""
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (TypeError, ValueError):
            return ""
        return _clean_text(((data.get("message") or {}).get("abstract") or ""))

    def _try_start_page_once(self):
        """Best-effort sanity probe for the public starting page.

        The crawl does not depend on this because some environments can reach
        the Cristin/NVA APIs while nersc.no itself is temporarily unroutable.
        """
        html = self._get_html(_START_URL, retries=1)
        if not html:
            return
        soup = _make_soup(html)
        if soup is None:
            print("[nersc-no-en] start page fetched but HTML parsing failed")
            return
        title = soup.find(["h1", "title"])
        if title:
            print(f"[nersc-no-en] start page ok: {_clean_text(title.get_text())[:80]}")

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _links_from_cristin(self, item):
        links = {}
        for link in item.get("links") or []:
            url = _clean_text(link.get("url"))
            link_type = _clean_text(link.get("url_type")).upper()
            if url and link_type and link_type not in links:
                links[link_type] = url
        return links

    def _authors_from_nva(self, nva_detail):
        entity = (nva_detail or {}).get("entityDescription") or {}
        contributors = entity.get("contributors") or []
        names = []
        for contributor in sorted(contributors, key=lambda x: x.get("sequence") or 9999):
            identity = contributor.get("identity") or {}
            names.append(identity.get("name"))
        return _join_unique(names)

    def _authors_from_cristin(self, item):
        preview = ((item.get("contributors") or {}).get("preview")) or []
        names = []
        for contributor in preview:
            first = _clean_text(contributor.get("first_name"))
            surname = _clean_text(contributor.get("surname"))
            if first and first.endswith(",") and surname:
                names.append(f"{first} {surname}")
            else:
                names.append(" ".join(part for part in (first, surname) if part))
        return _join_unique(names)

    def _title(self, item, nva_detail):
        entity = (nva_detail or {}).get("entityDescription") or {}
        return (
            _clean_text(entity.get("mainTitle"))
            or _lang_value((item.get("title") or {}))
        )

    def _abstract(self, item, nva_detail, doi):
        entity = (nva_detail or {}).get("entityDescription") or {}
        candidates = [
            entity.get("abstract"),
            entity.get("alternativeAbstracts"),
            item.get("summary"),
        ]
        for candidate in candidates:
            text = _lang_value(candidate)
            if len(text) >= _MIN_ABSTRACT_CHARS:
                return text
        return self._fetch_crossref_abstract(doi)

    def _publication_context(self, item, nva_detail):
        entity = (nva_detail or {}).get("entityDescription") or {}
        reference = entity.get("reference") or {}
        context = reference.get("publicationContext") or {}
        instance = reference.get("publicationInstance") or {}

        journal = (
            ((item.get("journal") or {}).get("name"))
            or (item.get("channel") or {}).get("title")
            or _clean_text(context.get("title"))
        )
        publisher = item.get("publisher")
        if isinstance(publisher, dict):
            publisher_name = _lang_value(publisher.get("name")) or _clean_text(publisher.get("id"))
        else:
            publisher_name = _clean_text(publisher)
        if not publisher_name and (item.get("category") or {}).get("code") == "REPORT":
            publisher_name = _clean_text((item.get("channel") or {}).get("title"))
        if not publisher_name:
            publisher_name = "Nansen Environmental and Remote Sensing Center"

        return {
            "journal": _clean_text(journal) or None,
            "publisher": publisher_name or None,
            "series": _clean_text(item.get("series")) or None,
            "volume": _clean_text(item.get("volume")) or None,
            "issue": _clean_text(item.get("issue")) or None,
            "article_number": _clean_text(item.get("article_number")) or None,
            "publication_instance": _clean_text(instance.get("type")) or None,
            "publication_context": context or None,
        }

    def _file_info(self, nva_detail):
        artifacts = (nva_detail or {}).get("associatedArtifacts") or []
        pdf_url = None
        original_filename = None
        open_files = []
        associated_links = []

        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_type = artifact.get("type")
            if artifact_type in ("OpenFile", "PublishedFile"):
                open_files.append(artifact)
                name = _clean_text(artifact.get("name"))
                if name and not original_filename:
                    original_filename = name
            elif artifact_type == "AssociatedLink":
                link = _clean_text(artifact.get("id"))
                if link:
                    associated_links.append(link)
                    lower_path = urlparse(link).path.lower()
                    if lower_path.endswith(".pdf") and not pdf_url:
                        pdf_url = link

        if pdf_url and not original_filename:
            original_filename = self._head_filename(pdf_url) or _filename_from_url(pdf_url)

        return pdf_url, original_filename, open_files, associated_links

    def _keywords(self, nva_detail):
        entity = (nva_detail or {}).get("entityDescription") or {}
        tags = entity.get("tags") or []
        if isinstance(tags, dict):
            tags = list(tags.values())
        if not isinstance(tags, list):
            tags = [tags]
        return _join_unique(tags, sep=", ")

    def _category(self, item, nva_detail):
        cristin_category = item.get("category") or {}
        label = _lang_value(cristin_category.get("name"))
        if label:
            return label
        entity = (nva_detail or {}).get("entityDescription") or {}
        reference = entity.get("reference") or {}
        instance = reference.get("publicationInstance") or {}
        return _clean_text(instance.get("type")) or _clean_text(cristin_category.get("code")) or None

    def _doi(self, item, nva_detail, links):
        entity = (nva_detail or {}).get("entityDescription") or {}
        reference = entity.get("reference") or {}
        return (
            _normalize_doi(reference.get("doi"))
            or _normalize_doi(links.get("DOI"))
            or _normalize_doi(item.get("doi"))
        )

    def _build_paper(self, item, nva_detail):
        cristin_id = _clean_text(item.get("cristin_result_id"))
        nva_id = _clean_text(item.get("nva_result_id") or (nva_detail or {}).get("identifier"))
        links = self._links_from_cristin(item)
        doi = self._doi(item, nva_detail, links)
        title = self._title(item, nva_detail)
        abstract = self._abstract(item, nva_detail, doi or links.get("DOI"))
        context = self._publication_context(item, nva_detail)
        pdf_url, original_filename, open_files, associated_links = self._file_info(nva_detail)

        entity = (nva_detail or {}).get("entityDescription") or {}
        nva_pub_date = ((entity.get("reference") or {}).get("publicationDate")) or entity.get("publicationDate")
        published_date = (
            _iso_date(nva_pub_date)
            or _iso_date(item.get("date_published"))
            or _iso_date(item.get("year_published"))
        )

        raw_listed_date = (
            (nva_detail or {}).get("publishedDate")
            or (nva_detail or {}).get("createdDate")
            or ((item.get("created") or {}).get("date"))
            or item.get("created")
        )
        listed_date = _iso_date(raw_listed_date)

        url = f"{_NVA_REGISTRATION_URL}/{nva_id}" if nva_id else item.get("url")
        if not url:
            url = f"{_CRISTIN_LIST_API}/{cristin_id}"

        category = self._category(item, nva_detail)
        keywords = self._keywords(nva_detail)
        authors = self._authors_from_nva(nva_detail) or self._authors_from_cristin(item)

        metadata = {
            "posted_date": raw_listed_date,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": context.get("journal"),
            "series": context.get("series"),
            "volume": context.get("volume"),
            "issue": context.get("issue"),
            "article_number": context.get("article_number"),
            "cristin_result_id": cristin_id,
            "nva_result_id": nva_id,
            "node_id": nva_id,
            "cristin_detail_api": f"{_CRISTIN_LIST_API}/{cristin_id}" if cristin_id else None,
            "nva_detail_api": f"{_NVA_PUBLICATION_API}/{nva_id}" if nva_id else None,
            "nva_registration_url": url,
            "arkiv_url": links.get("ARKIV"),
            "doi_url": links.get("DOI"),
            "category_code": (item.get("category") or {}).get("code"),
            "publication_instance": context.get("publication_instance"),
            "publication_context": context.get("publication_context"),
            "year_published": item.get("year_published"),
            "original_language": item.get("original_language"),
            "contributors_count": (item.get("contributors") or {}).get("count"),
            "associated_links": associated_links,
            "open_files": open_files,
            "raw_cristin": item,
            "raw_nva": nva_detail,
        }

        return {
            "id": str(uuid.uuid4()),
            "site_id": self.site_id,
            "external_id": cristin_id or nva_id,
            "post_number": cristin_id or nva_id or None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": context.get("publisher"),
            "department": "Nansen Environmental and Remote Sensing Center",
            "journal": context.get("journal"),
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        self._try_start_page_once()

        while page <= _MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed >= _WALL_BUDGET_SECONDS - 30:
                print(
                    f"[nersc-no-en] approaching 25-minute wall-clock budget "
                    f"at page {page}; saved {saved}, stopping cleanly"
                )
                break

            if page % 10 == 0:
                print(f"[nersc-no-en] page {page}: saved {saved}/{limit_or_inf}")

            try:
                items = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[nersc-no-en] page {page} failed: {exc}")
                break

            if not items:
                print(f"[nersc-no-en] page {page}: no records; stopping")
                break

            new_urls_on_page = 0
            for item_index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                try:
                    cristin_id = _clean_text(item.get("cristin_result_id"))
                    nva_id = _clean_text(item.get("nva_result_id"))
                    item_url = f"{_NVA_REGISTRATION_URL}/{nva_id}" if nva_id else item.get("url")
                    item_url = item_url or f"{_CRISTIN_LIST_API}/{cristin_id}"

                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_urls_on_page += 1

                    if self._delay:
                        time.sleep(self._delay)

                    nva_detail = self._fetch_nva_detail(nva_id)
                    if nva_detail is None and cristin_id:
                        nva_detail = {}
                        cristin_detail = self._fetch_cristin_detail(cristin_id)
                        if cristin_detail:
                            item = {**item, **cristin_detail}

                    paper = self._build_paper(item, nva_detail or {})
                    title = paper.get("title") or "(untitled)"
                    abstract = paper.get("abstract") or ""

                    if not paper.get("external_id"):
                        print(f"[nersc-no-en] item {item_index} skipped: no native id")
                        continue
                    if not title or title == "(untitled)":
                        print(f"[nersc-no-en] item {paper.get('external_id')} skipped: no title")
                        continue
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[nersc-no-en] item {paper.get('external_id')} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[nersc-no-en] saved {saved}/{limit_or_inf}: {title[:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_id = item.get("cristin_result_id") or item.get("nva_result_id") or item_index
                    print(f"[nersc-no-en] item {item_id} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[nersc-no-en] page {page}: all items already seen; stopping")
                break

            page += 1

        if page > _MAX_PAGES:
            print(f"[nersc-no-en] safety cap of {_MAX_PAGES} pages reached")

        print(f"[nersc-no-en] done. saved {saved}")
        return saved
