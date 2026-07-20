# -*- coding: utf-8 -*-
"""Eidgenössisches Departement des Innern (EDI) media releases crawler.

Starting URL: https://www.edi.admin.ch/de/medienmitteilungen-des-edi

The page is a Nuxt 3 SPA. The actual listing is served by a JSON search API
(the "ContentBroker News API") at ``https://d-nsbc-p.admin.ch/v1/search``
(base URL discovered from the page's runtime config,
``CONTENTBROKER_NEWS_API_PUBLIC_URL``), filtered by ``publisherIDs=3`` (EDI).
Detail content (body text + PDF attachments) is fetched per-item from
``https://d-nsbc-p.admin.ch/v1/{id}``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _strip_html(fragment: str) -> str:
    """Strip HTML tags from a small text fragment, tolerating malformed markup."""
    if not fragment:
        return ""
    if _BS is not None:
        for parser in _PARSERS:
            try:
                return _BS(fragment, parser).get_text(" ", strip=True)
            except Exception:
                continue
    return re.sub(r"<[^>]+>", " ", fragment)


class EdiAdminChDeCrawler(BaseCrawler):
    """Crawler for EDI (Eidgenössisches Departement des Innern) media releases."""

    site_id = "edi-admin-ch-de"
    site_name = "Custom: edi-admin-ch-de"
    base_url = "https://www.edi.admin.ch"

    _SEARCH_BASE = "https://d-nsbc-p.admin.ch/v1"
    _PUBLISHER_ID = "3"
    _PUBLISHER_NAME = "Eidgenössisches Departement des Innern (EDI)"
    _PAGE_SIZE = 20
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _curl_get_json(self, url: str) -> dict | None:
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] JSON decode error for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _search(self, offset: int, limit: int) -> dict | None:
        url = (
            f"{self._SEARCH_BASE}/search"
            f"?languages=de&newsKinds=CONTENT_HUB&newsKinds=ONSB"
            f"&publisherIDs={self._PUBLISHER_ID}"
            f"&start_date=2000-01-01T00:00:00.000Z"
            f"&end_date=2100-01-01T00:00:00.000Z"
            f"&offset={offset}&limit={limit}&sort=DESC"
        )
        return self._curl_get_json(url)

    def _fetch_detail(self, item_id: str) -> dict | None:
        url = f"{self._SEARCH_BASE}/{item_id}"
        return self._curl_get_json(url)

    @staticmethod
    def _iso_date(iso_str: str | None) -> str | None:
        if not iso_str or len(iso_str) < 10:
            return None
        return iso_str[:10]

    @staticmethod
    def _find_pdf(detail: dict | None):
        """Return (pdf_url, original_filename) for the first PDF asset found, or (None, None)."""
        if not detail:
            return None, None
        refs = (detail.get("content") or {}).get("references") or {}
        media = refs.get("media") or {}
        permalinks = refs.get("permalinks") or {}
        for media_id, m in media.items():
            asset = (m or {}).get("asset") or {}
            mime = (asset.get("mimeType") or "").lower()
            filename = asset.get("filename") or ""
            if mime == "application/pdf" or filename.lower().endswith(".pdf"):
                links = permalinks.get(media_id) or {}
                pdf_url = links.get("de") or next(iter(links.values()), None) or asset.get("url")
                return pdf_url, (filename or None)
        return None, None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        offset = 0
        page_num = 0
        total = None
        lim_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page_num += 1
                data = self._search(offset, self._PAGE_SIZE)
                if not data:
                    print(f"[{self.site_id}] Search request failed at offset {offset}. Stopping.")
                    break

                if total is None:
                    total = data.get("pageResults")

                items = data.get("items") or []
                if not items:
                    print(f"[{self.site_id}] No more items at offset {offset}. Stopping.")
                    break

                new_count = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    item_id = item.get("id") or ""
                    lang_group_id = item.get("langGroupId") or ""
                    if not lang_group_id:
                        continue
                    url = f"{self.base_url}/de/newnsb/{lang_group_id}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_count += 1

                    try:
                        md = (item.get("content") or {}).get("metadata") or {}
                        title = md.get("metaTitle") or ""
                        if not title:
                            print(f"[{self.site_id}] Empty title for {item_id}, skipping.")
                            continue

                        description = md.get("description") or ""

                        time.sleep(self._delay)
                        detail = self._fetch_detail(item_id)

                        body_parts = []
                        if detail:
                            for frag in detail.get("text") or []:
                                text = _strip_html(frag)
                                if text:
                                    body_parts.append(text)

                        abstract = " ".join([description] + body_parts).strip()
                        abstract = re.sub(r"\s+", " ", abstract)
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] Short abstract ({len(abstract)}) for '{title[:50]}', skipping.")
                            continue

                        pdf_url, original_filename = self._find_pdf(detail)

                        published_date = self._iso_date(md.get("announcementDate")) or self._iso_date(item.get("publishDate"))
                        listed_date = self._iso_date(item.get("publishDate"))

                        news_category = item.get("newsCategory") or ""
                        department = ""
                        if "_" in item_id:
                            department = item_id.split("_", 1)[0].upper()

                        topics = (detail or {}).get("topics") or item.get("topics") or []
                        keywords = ",".join(str(t) for t in topics) if topics else ""

                        meta = {
                            "posted_date": item.get("publishDate"),
                            "id": item_id,
                            "langGroupId": lang_group_id,
                            "newsCategory": news_category,
                            "kind": item.get("kind"),
                            "location": (detail or {}).get("location"),
                            "publishers_raw": (detail or {}).get("publishers"),
                            "coPublishers_raw": (detail or {}).get("coPublishers"),
                            "topics_raw": (detail or {}).get("topics"),
                        }
                        if original_filename:
                            meta["originalFilename"] = original_filename
                        meta = {k: v for k, v in meta.items() if v not in (None, [], "")}

                        m = re.search(r"(\d+)$", item_id)
                        post_number = m.group(1) if m else (item_id or None)

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": item_id,
                            "post_number": post_number,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": "",
                            "publisher": self._PUBLISHER_NAME,
                            "department": department,
                            "journal": "",
                            "url": url,
                            "pdf_url": pdf_url,
                            "keywords": keywords,
                            "category": news_category,
                            "doi": None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_id} failed: {exc}; continuing.")
                        continue

                if new_count == 0:
                    print(f"[{self.site_id}] Page {page_num} produced 0 new records. Stopping.")
                    break

                offset += self._PAGE_SIZE
                if total is not None and offset >= total:
                    print(f"[{self.site_id}] Reached end of result set ({total} total).")
                    break

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
