# -*- coding: utf-8 -*-
"""Crawler for Australian Government Transparency Portal — publications.

Data source: Azure Cognitive Search index (credentials embedded in the site's
public JS bundle) that backs the React SPA at transparency.gov.au/publications.
Covers three publication types: Annual Reports, Corporate Plans, and Portfolio
Budget Statements.
"""

from __future__ import annotations

import os
import json
import time
from datetime import datetime
from typing import Optional
from urllib.parse import unquote

from crawler.base_crawler import BaseCrawler


class TransparencyGovAuPublicationsCrawler(BaseCrawler):
    site_id = "transparency-gov-au-publications"
    site_name = "Custom: transparency-gov-au-publications"
    base_url = "https://www.transparency.gov.au"

    # Azure Cognitive Search — read-only credentials extracted from the site's
    # public JS bundle (acs-dof-shared-aue-01 / prod-content-index).
    _SEARCH_ENDPOINT = (
        "https://acs-dof-shared-aue-01.search.windows.net"
        "/indexes/prod-content-index/docs/search"
        "?api-version=2024-05-01-preview"
    )
    _SEARCH_API_KEY = os.environ.get("TRANSPARENCY_GOV_AU_PUBLICATIONS_KEY", "")
    # OData filter matching the publications page logic (FILTER_IN from JS bundle)
    _PUB_FILTER = "search.in(ContentType, 'annual_report|corp_plan|pbs', '|')"

    _PAGE_SIZE = 50
    _SAFETY_CAP = 200
    _MIN_ABSTRACT = 50
    _WALL_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    _PUB_TYPE_LABELS = {
        "annual_report": "Annual Report",
        "corp_plan": "Corporate Plan",
        "pbs": "Portfolio Budget Statement",
    }
    _URL_PREFIXES = {
        "annual_report": "/annual-reports/",
        "corp_plan": "/corporate-plans/",
        "pbs": "/portfolio-budget-statements/",
    }

    def crawl(self, limit=None):
        saved = 0
        seen_ids: set = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "∞"

        page = 0
        while True:
            if page >= self._SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP} pages reached")
                break

            if time.time() - start_time > self._WALL_BUDGET_S:
                print(f"[{self.site_id}] 25-min wall budget reached; stopping")
                break

            if limit is not None and saved >= limit:
                break

            skip = page * self._PAGE_SIZE
            top = self._PAGE_SIZE
            if limit is not None:
                top = min(top, limit - saved)

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            try:
                items = self._fetch_page(skip, top)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} fetch failed: {exc}")
                break

            if not items:
                print(f"[{self.site_id}] page {page}: empty response, stopping")
                break

            new_in_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_id = item.get("ID", "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                new_in_page += 1

                try:
                    paper = self._build_paper(item)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    title = item.get("Title", "?")[:60]
                    print(f"[{self.site_id}] item '{title}' failed: {exc}")
                    continue

            if new_in_page == 0:
                print(f"[{self.site_id}] page {page}: no new items, stopping")
                break

            # Last page: fewer items than requested
            if len(items) < self._PAGE_SIZE:
                break

            page += 1
            time.sleep(self._delay)

        print(f"[{self.site_id}] done: saved {saved}")
        return saved

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_page(self, skip: int, top: int) -> list:
        """POST to Azure Search and return the value list. Retries 3×."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "api-key": self._SEARCH_API_KEY,
        }
        body = {
            "search": "",
            "searchMode": "all",
            "filter": self._PUB_FILTER,
            "orderby": "ReportingYear desc",
            "top": top,
            "skip": skip,
            "count": True,
            "queryType": "simple",
        }
        backoff = [1, 3, 9]
        for attempt in range(3):
            try:
                resp = self._session.post(
                    self._SEARCH_ENDPOINT,
                    headers=headers,
                    json=body,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("value", [])
            except Exception as exc:
                wait = backoff[attempt]
                print(
                    f"[{self.site_id}] fetch attempt {attempt + 1}/3 failed: {exc}; "
                    f"retrying in {wait}s"
                )
                time.sleep(wait)
        return []

    def _build_paper(self, item: dict) -> Optional[dict]:
        title = (item.get("Title") or "").strip()
        if not title:
            return None

        item_id = item.get("ID", "") or ""
        if not item_id:
            return None

        codename = item.get("CodeName", "") or ""
        content_type = item.get("ContentType", "") or ""
        url_slug = item.get("UrlSlug", "") or ""
        entity = item.get("Entity", "") or ""
        portfolio = item.get("Portfolio", "") or ""
        body_type = item.get("BodyType", "") or ""
        reporting_year = item.get("ReportingYear", "") or ""
        acronym = item.get("Acronym", "") or ""
        pub_date_raw = item.get("PublicationDate", "") or ""

        url, pdf_url = self._parse_url(content_type, url_slug)
        original_filename = self._extract_filename(pdf_url)
        published_date = self._parse_date(pub_date_raw)

        # Publisher: entity + portfolio, "; " separated
        publisher_parts = [p for p in [entity, portfolio] if p]
        publisher = "; ".join(publisher_parts) if publisher_parts else None

        category = self._PUB_TYPE_LABELS.get(content_type, content_type)

        abstract = self._build_abstract(
            title, content_type, entity, portfolio,
            reporting_year, body_type, acronym,
        )
        if len(abstract) < self._MIN_ABSTRACT:
            print(
                f"[{self.site_id}] skipping '{title[:50]}' — "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        metadata = {
            "ContentType": content_type,
            "CodeName": codename,
            "ReportingYear": reporting_year,
            "BodyType": body_type,
            "Acronym": acronym,
            "Portfolio": portfolio,
            "PortfolioCodename": item.get("PortfolioCodename") or "",
            "EntityCodename": item.get("EntityCodename") or "",
            "EntityUrlSlug": item.get("EntityUrlSlug") or "",
            "StatusText": item.get("StatusText") or "",
            "posted_date": published_date,
            "PublicationDate_raw": pub_date_raw,
        }

        return {
            "site_id": self.site_id,
            "external_id": item_id,
            "post_number": item_id,  # UUID (no numeric ID on this site)
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "authors": entity or None,
            "publisher": publisher,
            "department": body_type or None,
            "journal": None,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_url(self, content_type: str, url_slug: str):
        """Return (url, pdf_url).

        For corp_plan and pbs, UrlSlug from the search index contains the
        direct PDF asset URL. For annual_report, it's a URL path slug.
        """
        if url_slug.startswith("http"):
            # PDF asset URL — use as both the canonical URL and pdf_url
            return url_slug, url_slug
        if url_slug:
            prefix = self._URL_PREFIXES.get(content_type, "/publications/")
            url = self.base_url + prefix + url_slug
            return url, None
        return self.base_url + "/publications", None

    def _extract_filename(self, pdf_url: Optional[str]) -> Optional[str]:
        if not pdf_url:
            return None
        try:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
            tail = unquote(tail)
            if "." in tail and len(tail) <= 200:
                return tail
        except Exception:
            pass
        return None

    def _parse_date(self, raw: str) -> Optional[str]:
        """Parse the site's DD/MM/YYYY date strings to ISO YYYY-MM-DD."""
        if not raw:
            return None
        raw = raw.strip()
        # transparency.gov.au uses Australian day-first format confirmed by
        # dates like "13/11/2024" (month 13 is impossible in M/D order).
        formats = [
            "%d/%m/%Y %I:%M:%S %p",  # 13/11/2024 4:58:37 AM
            "%d/%m/%Y %H:%M:%S",     # 03/01/2026 23:45:53
            "%d/%m/%Y",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _build_abstract(
        self,
        title: str,
        content_type: str,
        entity: str,
        portfolio: str,
        period: str,
        body_type: str,
        acronym: str,
    ) -> str:
        """Synthesise an abstract from available metadata fields.

        The search index has no free-text content field; abstracts are built
        from structured fields. The trailing sentence alone exceeds 100 chars.
        """
        type_label = self._PUB_TYPE_LABELS.get(content_type, content_type or "Publication")
        parts = [f"{title}."]

        if entity:
            acro = acronym.strip()
            publisher_str = entity
            if acro and acro not in entity and len(acro) < 60:
                publisher_str += f" ({acro})"
            parts.append(f"Published by {publisher_str}.")

        if portfolio:
            parts.append(f"Portfolio: {portfolio}.")

        if period:
            parts.append(f"Reporting period: {period}.")

        if body_type:
            parts.append(f"Entity type: {body_type}.")

        parts.append(
            f"This {type_label} is published on the Australian Government "
            "Transparency Portal (transparency.gov.au), fulfilling Commonwealth "
            "government accountability and reporting requirements."
        )

        return " ".join(parts)
