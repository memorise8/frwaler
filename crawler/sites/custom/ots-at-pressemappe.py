# -*- coding: utf-8 -*-
"""OTS.at Pressemappe crawler — Bundesministerium für Inneres (emittentId=54).

Target: https://www.ots.at/pressemappe/54/bundesministerium-fuer-inneres

OTS.at (APA-OTS / "Originaltext-Service") is the Austrian press-release
distributor. Each "Pressemappe" (press folder) lists all releases issued by
one sending organisation (here: Bundesministerium für Inneres, the Austrian
Ministry of Interior — emittentId 54).

Data source
-----------
The site is an Angular SSR app. Rather than scraping the rendered HTML, we
call the same JSON API the frontend uses (discovered via the embedded
``<script id="ng-state" type="application/json">`` Angular TransferState
blob on the list/detail pages):

- List/search:   GET https://core2023.ots.at/api/v1/search
                     ?query=&page=<N>&maxCountPerPage=10&emittentId=54
                 -> {"meta": {"totalPages": ..., "count": ...},
                     "result": [{"key": "OTS_20260717_OTS0045", "date": ...,
                                 "time": ..., "title": {...}, "lead": {...},
                                 "emittentName": ..., "channel": [...], ...}]}

- Detail:        GET https://core2023.ots.at/api/v1/pressrelease/<key>
                 -> {"result": {"article": [{"type": "text"/"structured"/
                                 "inquiryHint", "content": "..."}], "date":
                                 ..., "time": ..., "emittentName": ...,
                                 "keywords": [...], "channel": [...],
                                 "inquiryHint": {...}, "lead": {...}, ...}}

The detail URL path used for the ``url`` field mirrors what visitors see:
``https://www.ots.at/presseaussendung/<key>/<titleSlug>``.

Native IDs on OTS are NOT plain integers — they are composite strings like
``OTS_20260717_OTS0045`` (date + sequence number). ``external_id`` and
``post_number`` both use this native key as-is (per spec: "same native ID
as a STRING if numeric, else native slug/uuid").

No PDF/attachment links were found anywhere in the API payloads for this
Pressemappe (OTS releases here are plain text releases) — ``pdf_url`` and
``original_filename`` are therefore expected to stay ``None`` in practice,
but the code still looks for one defensively.

A raw-HTML BeautifulSoup fallback (``_soup`` / ``_fetch_detail_html``) is
kept for robustness in case the JSON detail endpoint fails for a given item;
it targets the "leaf" ``<article>`` tag (the one with no nested ``<article>``
descendant) since the SSR markup nests a duplicate outer ``<article>`` that
also wraps the lead/contact/disclaimer text.
"""

from __future__ import annotations

import json
import os
import re
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.ots.at"
_API_BASE = "https://core2023.ots.at/api/v1"
_EMITTENT_ID = "54"
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MIN_ABSTRACT_SAVE = 50     # skip saving if shorter than this
_BACKOFF = (1, 3, 9)
_TIME_BUDGET_SECONDS = 1500  # 25 minutes


class OtsAtPressemappeCrawler(BaseCrawler):
    site_id = "ots-at-pressemappe"
    site_name = "Custom: ots-at-pressemappe"
    base_url = _BASE
    DELIVERY_ORDER = "arbitrary"

    def __init__(self, db_conn=None, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.request_delay = 1.0

    # ------------------------------------------------------------------ #
    # HTML parsing helper (fallback path only)                            #
    # ------------------------------------------------------------------ #

    def _soup(self, html):
        """Parse *html* trying html5lib -> lxml -> html.parser in order."""
        from bs4 import BeautifulSoup

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception as exc:
                print(f"[{self.site_id}] _soup: parser {parser} failed: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # Low-level HTTP with retry/backoff                                   #
    # ------------------------------------------------------------------ #

    def _get(self, url, params=None, as_json=True):
        """GET *url* with up to 3 retries (backoff 1s/3s/9s).

        Returns the parsed JSON dict when ``as_json`` is True, else the raw
        decoded text. Returns ``None`` after exhausting retries.
        """
        last_exc = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                if as_json:
                    try:
                        return resp.json()
                    except ValueError:
                        raw = resp.content.decode("utf-8", errors="replace")
                        return json.loads(raw)
                try:
                    return resp.text
                except Exception:
                    return resp.content.decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001 - broad on purpose, network
                last_exc = exc
                print(f"[{self.site_id}] request error (attempt {attempt + 1}/3) for {url}: {exc}")
                if attempt < 2:
                    time.sleep(_BACKOFF[attempt])
        print(f"[{self.site_id}] giving up on {url}: {last_exc}")
        return None

    # ------------------------------------------------------------------ #
    # List page (search API)                                              #
    # ------------------------------------------------------------------ #

    def _fetch_list_page(self, page):
        """Fetch one page of the press-release list.

        Returns the list of items on success (an empty list means the list
        genuinely ended). Returns None on a transient fetch/parse failure so
        the caller can tell that apart from a real end of pagination.
        """
        params = {
            "query": "",
            "page": page,
            "maxCountPerPage": _PAGE_SIZE,
            "emittentId": _EMITTENT_ID,
        }
        data = self._get(f"{_API_BASE}/search", params=params, as_json=True)
        if not data or not isinstance(data, dict):
            return None
        return data.get("result") or []

    # ------------------------------------------------------------------ #
    # Detail page (pressrelease API, with HTML fallback)                  #
    # ------------------------------------------------------------------ #

    def _fetch_detail_json(self, key):
        data = self._get(f"{_API_BASE}/pressrelease/{key}", as_json=True)
        if not data or not isinstance(data, dict):
            return None
        return data.get("result")

    def _fetch_detail_html_fallback(self, detail_url):
        """Best-effort HTML scrape used only if the JSON API call fails."""
        html = self._get(detail_url, as_json=False)
        if not html:
            return None
        soup = self._soup(html)
        if soup is None:
            return None

        title_el = soup.select_one("h1.h1-big") or soup.find("h1")
        title = title_el.get_text(strip=True) if title_el else None

        lead_el = soup.select_one("p.lead")
        lead = lead_el.get_text(strip=True) if lead_el else ""

        articles = soup.find_all("article")
        content_article = None
        if articles:
            leaf_articles = [a for a in articles if not a.find("article")]
            content_article = leaf_articles[-1] if leaf_articles else articles[-1]
        body_paras = content_article.find_all("p") if content_article else []
        body = "\n\n".join(p.get_text(" ", strip=True) for p in body_paras)

        date_match = re.search(r"(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}:\d{2}:\d{2})", html)
        iso_date = None
        if date_match:
            dd, mm, yyyy, _hhmmss = date_match.groups()
            iso_date = f"{yyyy}-{mm}-{dd}"

        keywords = [b.get_text(strip=True) for b in soup.select(".keyword-link")]

        return {
            "title": title,
            "lead": lead,
            "body": body,
            "date": iso_date,
            "keywords": keywords,
            "emittentName": None,
            "channel": [],
        }

    # ------------------------------------------------------------------ #
    # Field extraction helpers                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_ws(text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _iso_date(date_str):
        if not date_str:
            return None
        date_str = date_str.strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_str)
        if m:
            return m.group(0)
        m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", date_str)
        if m:
            dd, mm, yyyy = m.groups()
            return f"{yyyy}-{mm}-{dd}"
        return None

    def _build_abstract_from_json(self, detail, lead):
        parts = []
        if lead:
            parts.append(lead)
        for item in detail.get("article") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in ("text", "structured"):
                continue
            content = item.get("content")
            if content:
                parts.append(content)
        return self._normalize_ws("\n\n".join(parts))

    def _extract_pdf_url(self, detail):
        """Best-effort scan for an attached PDF/original document link.

        No attachment fields have been observed in this Pressemappe's API
        payloads; this stays defensive in case another release has one.
        """
        for key in ("attachments", "files", "documents"):
            val = detail.get(key)
            if isinstance(val, list):
                for entry in val:
                    if isinstance(entry, dict):
                        url = entry.get("url") or entry.get("href")
                        if url and str(url).lower().endswith(".pdf"):
                            return url
                    elif isinstance(entry, str) and entry.lower().endswith(".pdf"):
                        return entry
        return None

    # ------------------------------------------------------------------ #
    # Main crawl loop                                                     #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        start = time.monotonic()
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "inf"
        page_start = (self.delivery_cursor or {}).get("page", 1)
        pages_attempted = 0

        # _MAX_PAGES is a per-run chunk size (not an absolute ceiling) so a resume
        # from a large cursor still walks a full budget of pages this run.
        for page in range(page_start, page_start + _MAX_PAGES):
            elapsed = time.monotonic() - start
            if elapsed > _TIME_BUDGET_SECONDS:
                print(f"[{self.site_id}] time budget ({_TIME_BUDGET_SECONDS}s) exceeded, stopping. saved={saved}")
                break

            pages_attempted += 1

            if page % 10 == 0 or page == 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            items = self._fetch_list_page(page)
            if items is None:
                print(f"[{self.site_id}] page {page} fetch failed, stopping pagination")
                break
            if not items:
                print(f"[{self.site_id}] page {page} returned no items, stopping pagination")
                self._mark_exhausted()
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.monotonic() - start
                if elapsed > _TIME_BUDGET_SECONDS:
                    print(f"[{self.site_id}] time budget exceeded mid-page, stopping. saved={saved}")
                    return saved

                key = item.get("key")
                title_slug = item.get("titleSlug") or ""
                if not key:
                    continue
                detail_url = f"{_BASE}/presseaussendung/{key}/{title_slug}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self.request_delay)
                    detail = self._fetch_detail_json(key)

                    posted_date_raw = None
                    list_date = item.get("date")
                    list_time = item.get("time")
                    if list_date:
                        posted_date_raw = f"{list_date} {list_time}".strip() if list_time else list_date
                    listed_date = self._iso_date(list_date)

                    if detail:
                        item_title = item.get("title")
                        item_title_content = item_title.get("content") if isinstance(item_title, dict) else None
                        title = (detail.get("title") or {}).get("content") or item_title_content
                        if not title:
                            title = title_slug.replace("-", " ").strip() or None
                        lead = self._normalize_ws((detail.get("lead") or {}).get("content"))
                        abstract = self._build_abstract_from_json(detail, lead)
                        published_date = self._iso_date(detail.get("date")) or listed_date
                        emittent_name = detail.get("emittentName") or item.get("emittentName")
                        channel = detail.get("channel") or item.get("channel") or []
                        keywords = detail.get("keywords") or []
                        pressreleasenumber = detail.get("pressreleasenumber")
                        emittent_id = detail.get("emittentId") or item.get("emittentId")
                        emittent_slug = detail.get("emittentSlug") or item.get("emittentSlug")
                        docsrc = detail.get("docsrc")
                        customer_code = detail.get("customerCode")
                        pdf_url = self._extract_pdf_url(detail)
                    else:
                        # Fall back to scraping the HTML detail page.
                        fb = self._fetch_detail_html_fallback(detail_url)
                        if not fb:
                            print(f"[{self.site_id}] item {detail_url} failed: no JSON or HTML detail available")
                            continue
                        title = fb.get("title") or (title_slug.replace("-", " ").strip() or None)
                        lead = self._normalize_ws(fb.get("lead"))
                        body = self._normalize_ws(fb.get("body"))
                        abstract = self._normalize_ws(f"{lead}\n\n{body}" if lead else body)
                        published_date = fb.get("date") or listed_date
                        emittent_name = fb.get("emittentName") or item.get("emittentName")
                        channel = fb.get("channel") or item.get("channel") or []
                        keywords = fb.get("keywords") or []
                        pressreleasenumber = None
                        emittent_id = item.get("emittentId")
                        emittent_slug = item.get("emittentSlug")
                        docsrc = None
                        customer_code = None
                        pdf_url = None

                    if len(abstract) < _MIN_ABSTRACT_SAVE:
                        print(f"[{self.site_id}] item {detail_url} skipped: abstract too short ({len(abstract)} chars)")
                        continue

                    if not title:
                        title = "(untitled)"

                    category_names = [
                        c.get("name") for c in channel if isinstance(c, dict) and c.get("name")
                    ]
                    category = ", ".join(category_names) if category_names else None
                    keywords_str = ", ".join(keywords) if keywords else None
                    publisher = emittent_name or None

                    original_filename = None
                    if pdf_url:
                        original_filename = pdf_url.rstrip("/").rsplit("/", 1)[-1] or None

                    metadata = {
                        "posted_date": posted_date_raw,
                        "originalFilename": original_filename,
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "ots_id": key,
                        "aussender_id": emittent_id,
                        "emittentSlug": emittent_slug,
                        "titleSlug": title_slug,
                        "pressreleasenumber": pressreleasenumber,
                        "docsrc": docsrc,
                        "customerCode": customer_code,
                        "channel_raw": channel,
                        "keywords_raw": keywords,
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": key,
                        "post_number": key,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": "",
                        "publisher": publisher or "",
                        "department": None,
                        "journal": None,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "keywords": keywords_str,
                        "category": category,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:  # noqa: BLE001 - per-item isolation
                    print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                    continue

            self._advance_cursor({"page": page + 1}, items_done=len(items))

            if limit is not None and saved >= limit:
                break
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page} yielded 0 new records, stopping")
                break
        else:
            if pages_attempted > 0:
                print(f"[{self.site_id}] hit safety cap of {_MAX_PAGES} pages this run")

        print(f"[{self.site_id}] done: saved {saved}/{limit_str}")
        return saved
