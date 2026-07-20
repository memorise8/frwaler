# -*- coding: utf-8 -*-
"""Crawler for CIGI Online Publications (cigionline.org).

Discovery: The JSON search API at /api/search/ is accessible without
Cloudflare interception (unlike /api/publications/). Intercepted via
Playwright network tracing during page load. Returns all publication
types with pagination via offset parameter.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler


class CigionlineOrgPublicationsCrawler(BaseCrawler):
    site_id = "cigionline-org-publications"
    site_name = "Custom: cigionline-org-publications"
    base_url = "https://www.cigionline.org"

    _SEARCH_API = "https://www.cigionline.org/api/search/"
    _PAGE_SIZE = 24
    _SUBTYPES = [
        "Books",
        "CIGI Papers",
        "Conference Reports",
        "Essays",
        "Essay Series",
        "Policy Briefs",
        "Policy Memos",
        "Quick Insights",
        "Special Reports",
    ]
    _FIELDS = ["authors", "pdf_download", "publishing_date", "topics", "contentsubtype"]
    _PUBLISHER = "Centre for International Governance Innovation"

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _build_search_url(self, offset: int) -> str:
        pairs: list[tuple[str, str]] = [
            ("limit", str(self._PAGE_SIZE)),
            ("offset", str(offset)),
            ("sort", "date"),
            ("contenttype", "Publication"),
        ]
        for st in self._SUBTYPES:
            pairs.append(("contentsubtype", st))
        for f in self._FIELDS:
            pairs.append(("field", f))
        return self._SEARCH_API + "?" + urlencode(pairs)

    def _fetch_search_page(self, offset: int) -> dict | None:
        """Fetch one page from the search API with retries."""
        url = self._build_search_url(offset)
        for attempt in range(3):
            try:
                resp = self._session.get(
                    url,
                    timeout=30,
                    headers={
                        "Referer": "https://www.cigionline.org/publications/",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest",
                        # Override base session's Accept-Encoding: requests doesn't
                        # natively decode Brotli (br), so restrict to gzip/deflate.
                        "Accept-Encoding": "gzip, deflate",
                    },
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] search API error (attempt {attempt + 1}/3): {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return None

    @staticmethod
    def _parse_date(raw: str) -> str:
        """'2026-05-07T04:00:00Z' → '2026-05-07'."""
        return raw[:10] if raw and len(raw) >= 10 else ""

    @staticmethod
    def _extract_filename(path: str) -> str:
        if not path:
            return ""
        tail = path.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        return tail if "." in tail else ""

    # ------------------------------------------------------------------ #
    # crawl                                                                #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        offset = 0
        page_num = 0
        MAX_PAGES = 200
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # ---- termination guards ----
            if limit is not None and saved >= limit:
                break
            if page_num >= MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached, stopping")
                break
            if time.time() - start_time > 23 * 60:
                print(f"[{self.site_id}] 23-minute time budget reached, stopping")
                break

            data = self._fetch_search_page(offset)
            if data is None:
                print(f"[{self.site_id}] Failed to fetch at offset {offset}, stopping")
                break

            items = data.get("items") or []
            if not items:
                print(f"[{self.site_id}] No more items at offset {offset}, done")
                break

            page_num += 1

            if page_num == 1:
                total = (data.get("meta") or {}).get("total_count")
                if total is not None:
                    print(f"[{self.site_id}] Total publications on server: {total}")

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    rel_url = (item.get("url") or "").strip()
                    if not rel_url:
                        continue

                    full_url = self.base_url + rel_url
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    title = (item.get("title") or "").strip()
                    if not title:
                        continue

                    snippet = (item.get("snippet") or "").strip()
                    if len(snippet) < 50:
                        print(
                            f"[{self.site_id}] abstract too short "
                            f"({len(snippet)} chars), skipping: {title[:60]}"
                        )
                        continue

                    item_id = item.get("id")
                    external_id = str(item_id) if item_id is not None else None

                    # Authors — semicolon separated
                    author_list = item.get("authors") or []
                    authors = "; ".join(
                        a["title"] for a in author_list if a.get("title")
                    ) or None

                    # PDF
                    pdf_path = (item.get("pdf_download") or "").strip()
                    pdf_url = (self.base_url + pdf_path) if pdf_path else None
                    original_filename = self._extract_filename(pdf_path) or None

                    # Dates
                    raw_date = item.get("publishing_date") or ""
                    published_date = self._parse_date(raw_date)

                    # Keywords from topics
                    topics = item.get("topics") or []
                    keywords = ", ".join(
                        t["title"] for t in topics if t.get("title")
                    ) or None

                    # Category
                    category = (item.get("contentsubtype") or "").strip() or None

                    # Metadata: all raw fields not mapped above
                    metadata = json.dumps(
                        {
                            "posted_date": raw_date,
                            "originalFilename": original_filename,
                            "node_id": str(item_id) if item_id is not None else None,
                            "contentsubtype": category,
                            "elevated": item.get("elevated"),
                            "topics_raw": [
                                {"id": t.get("id"), "title": t.get("title")}
                                for t in topics
                            ],
                            "authors_raw": [
                                {"id": a.get("id"), "title": a.get("title")}
                                for a in author_list
                            ],
                        },
                        ensure_ascii=False,
                    )

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "abstract": snippet,
                        "published_date": published_date,
                        "url": full_url,
                        "pdf_url": pdf_url,
                        "authors": authors,
                        "publisher": self._PUBLISHER,
                        "keywords": keywords,
                        "category": category,
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "original_filename": original_filename,
                        "metadata": metadata,
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item failed "
                        f"({item.get('url', '?')}): {exc}"
                    )
                    continue

            offset += self._PAGE_SIZE
            time.sleep(1.0)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
