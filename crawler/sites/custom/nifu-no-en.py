# -*- coding: utf-8 -*-
"""NIFU (Nordic Institute for Studies of Innovation, Research and Education) crawler.

Starting URL: https://www.nifu.no/en/nifu-alle-publikasjoner/

Discovery notes:
  - The publication tables on the listing page are populated client-side by
    jQuery DataTables. Each of the four tabs (Reports / Scientific articles /
    Working notes / Insights) points at a static JSON feed served from the
    theme folder:
        https://www.nifu.no/wp-content/themes/nifu-child/functions/json/rapporter.json
        https://www.nifu.no/wp-content/themes/nifu-child/functions/json/article.json
        https://www.nifu.no/wp-content/themes/nifu-child/functions/json/arbeidsnotat.json
        https://www.nifu.no/wp-content/themes/nifu-child/functions/json/innsikt.json
    These return the *entire* record set in one response (no server-side
    paging) with fields: tittel, summary, post_link, url, forfattere,
    publisert (year), cristinID, kategori (missing on rapporter.json,
    defaults to "Rapport"), issue, forskningstemaer.
  - A large fraction of records (mostly older reports) have an empty
    ``summary`` in the feed *and* an empty article body on the detail page —
    this is a genuine gap in NIFU's own data, not a parsing bug, so those
    records are skipped per the abstract-length rule.
  - Detail pages (``post_link``) carry extra metadata not present in the
    feed: the WordPress post id (``<article id="post-NNNNNN">``), the full
    publish timestamp (JSON-LD ``datePublished``), and occasionally
    ISBN/ISSN/page-count. ``url`` in the feed is either an external handle/
    DOI landing page (hdl.handle.net, doi.org) or, for some older reports, a
    PDF hosted directly under www.nifu.no.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urlparse

# Absolute import - spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_START_URL = "https://www.nifu.no/en/nifu-alle-publikasjoner/"
_JSON_BASE = "https://www.nifu.no/wp-content/themes/nifu-child/functions/json"
_CATEGORIES = (
    # (feed key, feed url, default category label when 'kategori' is absent)
    ("rapporter", f"{_JSON_BASE}/rapporter.json", "Rapport"),
    ("article", f"{_JSON_BASE}/article.json", "Artikkel"),
    ("arbeidsnotat", f"{_JSON_BASE}/arbeidsnotat.json", "Arbeidsnotat"),
    ("innsikt", f"{_JSON_BASE}/innsikt.json", "Innsikt"),
)
_PAGE_CAP = 200
_CHUNK_SIZE = 25
_CRAWL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_WAITS = (1, 3, 9)
_MIN_ABSTRACT_LEN = 50


def _clean_text(value):
    if not value:
        return ""
    text = unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    for ch in ("\xa0", "​", " ", " "):
        text = text.replace(ch, " ")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;")


def _make_soup(raw):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[nifu-no-en] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _year_to_date(value):
    text = _clean_text(value)
    if text.isdigit() and len(text) == 4:
        return f"{text}-01-01"
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return f"{match.group(0)}-01-01" if match else None


def _extract_doi(url):
    if not url:
        return None
    match = re.search(r"\b10\.\d{4,9}/[^\s\"'<>]+", url)
    return match.group(0).rstrip(".,);") if match else None


def _direct_pdf_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.path.lower().endswith(".pdf") and "nifu.no" in parsed.netloc.lower():
        return url
    return None


def _filename_from_url(url):
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    return tail[:240] if tail and "." in tail else None


def _slug_from_url(url):
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(tail) if tail else None


def _dedupe_join(values, sep="; "):
    seen = set()
    out = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)
    return sep.join(out) if out else None


def _authors_from_list(forfattere):
    if not isinstance(forfattere, list):
        return None
    names = []
    for entry in forfattere:
        if not isinstance(entry, dict):
            continue
        first = _clean_text(entry.get("first_name"))
        last = _clean_text(entry.get("surname"))
        full = f"{first} {last}".strip()
        if full:
            names.append(full)
    return _dedupe_join(names)


def _keywords_from_themes(themes):
    if not isinstance(themes, list):
        return None
    names = [t.get("navn") for t in themes if isinstance(t, dict) and t.get("navn")]
    joined = _dedupe_join(names, sep=", ")
    return joined


class NifuNoEnCrawler(BaseCrawler):
    site_id = "nifu-no-en"
    site_name = "Custom: nifu-no-en"
    base_url = "https://www.nifu.no"

    def _curl_get(self, url, referer=None):
        headers = [
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9,nb;q=0.8",
        ]
        if referer:
            headers.extend(["-H", f"Referer: {referer}"])

        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "45",
            *headers,
            url,
        ]

        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                if result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"[nifu-no-en] empty response attempt {attempt}/3 for {url}: {err}")
            except Exception as exc:
                print(f"[nifu-no-en] curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < len(_RETRY_WAITS):
                time.sleep(wait)

        print(f"[nifu-no-en] fetch failed after {len(_RETRY_WAITS)} attempts: {url}")
        return None

    def _fetch_all_items(self):
        """Fetch the four static JSON feeds and flatten them into one list."""
        items = []
        for key, feed_url, default_category in _CATEGORIES:
            raw = self._curl_get(feed_url, referer=_START_URL)
            if not raw:
                print(f"[nifu-no-en] category {key}: fetch failed, skipping")
                continue
            try:
                data = json.loads(raw)
            except Exception as exc:
                print(f"[nifu-no-en] category {key}: JSON parse failed: {exc}")
                continue
            if not isinstance(data, list):
                print(f"[nifu-no-en] category {key}: unexpected JSON shape, skipping")
                continue

            print(f"[nifu-no-en] category {key}: {len(data)} records discovered")
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                entry = dict(entry)
                entry["_feed_key"] = key
                entry["_default_category"] = default_category
                items.append(entry)
        return items

    @staticmethod
    def _extract_node_id(soup, raw):
        if soup is not None:
            article = soup.select_one('article[id^="post-"]')
            if article:
                match = re.search(r"post-(\d+)", article.get("id", ""))
                if match:
                    return match.group(1)
        match = re.search(r'id="post-(\d+)"', raw or "")
        return match.group(1) if match else None

    @staticmethod
    def _extract_meta_fields(soup):
        """Parse the '<li>Label: Value</li>' block on the detail page."""
        fields = {}
        if soup is None:
            return fields
        container = soup.select_one(".col-md-10.border-top ul.list-unstyled") or soup.select_one("ul.list-unstyled")
        if not container:
            return fields
        for li in container.find_all("li"):
            text = _clean_text(li.get_text(" ", strip=True))
            if not text or ":" not in text:
                continue
            label, _, value = text.partition(":")
            label = label.strip().lower()
            value = value.strip()
            if label and value:
                fields[label] = value
        return fields

    @staticmethod
    def _extract_ld_dates(soup):
        out = {}
        if soup is None:
            return out
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for node in candidates:
                if not isinstance(node, dict):
                    continue
                nodes = node.get("@graph") if isinstance(node.get("@graph"), list) else [node]
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    if n.get("datePublished") and "datePublished" not in out:
                        out["datePublished"] = n.get("datePublished")
                    if n.get("dateModified") and "dateModified" not in out:
                        out["dateModified"] = n.get("dateModified")
        return out

    def _fetch_detail(self, detail_url):
        """Return (node_id, meta_fields, ld_dates) for a detail page, or (None, {}, {}) on failure."""
        raw = self._curl_get(detail_url, referer=_START_URL)
        if not raw:
            return None, {}, {}
        soup = _make_soup(raw)
        node_id = self._extract_node_id(soup, raw)
        meta_fields = self._extract_meta_fields(soup)
        ld_dates = self._extract_ld_dates(soup)
        return node_id, meta_fields, ld_dates

    def _build_paper(self, item):
        title = _clean_text(item.get("tittel"))
        abstract = _clean_text(item.get("summary"))
        detail_url = item.get("post_link")

        if not title or not detail_url:
            return None
        if len(abstract) < _MIN_ABSTRACT_LEN:
            print(f"[nifu-no-en] item {detail_url} skipped: abstract too short ({len(abstract)} chars)")
            return None

        time.sleep(self._delay)
        node_id, meta_fields, ld_dates = self._fetch_detail(detail_url)

        ext_url = (item.get("url") or "").strip()
        feed_key = item.get("_feed_key")
        default_category = item.get("_default_category")

        year_raw = meta_fields.get("publisert") or item.get("publisert")
        published_date = _year_to_date(year_raw)

        date_published_raw = ld_dates.get("datePublished")
        listed_date = None
        if date_published_raw:
            listed_date = _clean_text(date_published_raw)[:10] or None
        listed_date = listed_date or published_date

        cristin_id = _clean_text(item.get("cristinID")) or None
        slug = _slug_from_url(detail_url)
        external_id = cristin_id or slug
        if node_id:
            post_number = node_id
        elif cristin_id and cristin_id.isdigit():
            post_number = cristin_id
        else:
            post_number = slug

        authors = _authors_from_list(item.get("forfattere"))
        keywords = _keywords_from_themes(item.get("forskningstemaer"))
        category = _clean_text(item.get("kategori")) or default_category

        doi = _extract_doi(ext_url)
        pdf_url = _direct_pdf_url(ext_url)
        original_filename = _filename_from_url(pdf_url) if pdf_url else None

        metadata = {
            "posted_date": date_published_raw or year_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": _clean_text(item.get("issue")) or None,
            "node_id": node_id,
            "cristinID": cristin_id,
            "kategori_raw": item.get("kategori") or default_category,
            "forskningstemaer": item.get("forskningstemaer") or None,
            "isbn": meta_fields.get("isbn"),
            "issn": meta_fields.get("issn"),
            "pages": meta_fields.get("antall sider"),
            "date_modified": ld_dates.get("dateModified"),
            "category_feed": feed_key,
            "ext_url": ext_url or None,
            "author_ids": [
                a.get("id") for a in item.get("forfattere") or [] if isinstance(a, dict) and a.get("id")
            ] or None,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": str(post_number) if post_number else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "NIFU",
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        started = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        all_items = self._fetch_all_items()
        if not all_items:
            print("[nifu-no-en] no records discovered from any category feed.")
            return 0

        total_items = len(all_items)
        idx = 0
        page = 0

        while idx < total_items:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                print("[nifu-no-en] wall-clock budget nearly reached; stopping cleanly.")
                break

            page += 1
            if page > _PAGE_CAP:
                print(f"[nifu-no-en] safety cap of {_PAGE_CAP} pages reached.")
                break

            chunk = all_items[idx: idx + _CHUNK_SIZE]
            idx += _CHUNK_SIZE

            new_items = [it for it in chunk if it.get("post_link") and it["post_link"] not in seen_urls]
            for it in new_items:
                seen_urls.add(it["post_link"])

            if not new_items:
                if page % 10 == 0:
                    print(f"[nifu-no-en] page {page}: saved {saved}/{limit_or_inf}")
                continue

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                    print("[nifu-no-en] wall-clock budget nearly reached; stopping cleanly.")
                    return saved

                detail_url = item.get("post_link")
                try:
                    paper = self._build_paper(item)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[nifu-no-en] Saved {saved}/{limit_or_inf}: {paper.get('title', '')[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nifu-no-en] item {detail_url} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[nifu-no-en] page {page}: saved {saved}/{limit_or_inf}")

        print(f"[nifu-no-en] Done. Total saved: {saved}")
        return saved
