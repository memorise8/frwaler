# -*- coding: utf-8 -*-
"""Crawler for the Swiss Federal Statistical Office (BFS) news/publications.

List source:
    AEM/Vue news list component on https://www.bfs.admin.ch/bfs/de/home.html

Detail sources:
    https://dam-api.bfs.admin.ch/hub/api/dam/packages/{gnp}
    https://dam-api.bfs.admin.ch/hub/api/dam/packages/{gnp}/assets/de
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import quote, unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


class BfsAdminChBfsCrawler(BaseCrawler):
    site_id = "bfs-admin-ch-bfs"
    site_name = "Custom: bfs-admin-ch-bfs"
    base_url = "https://www.bfs.admin.ch"

    START_URL = "https://www.bfs.admin.ch/bfs/de/home.html"
    FALLBACK_LIST_API = (
        "https://www.bfs.admin.ch/content/bfs/de/home/jcr:content/root/main/"
        "section_1270692185/container/grid/grid_par_1/container/nip.model.json"
    )
    PACKAGE_API = "https://dam-api.bfs.admin.ch/hub/api/dam/packages/{gnp}"
    ASSETS_API = "https://dam-api.bfs.admin.ch/hub/api/dam/packages/{gnp}/assets/de"

    PAGE_SIZE_FALLBACK = 7
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl BFS records from the real AEM list API and DAM detail APIs."""
        saved = 0
        page = 1
        page_size = self.PAGE_SIZE_FALLBACK
        seen_urls = set()
        started_at = time.time()
        limit_label = str(limit) if limit is not None else "inf"

        list_api = self._discover_list_api()
        print(f"[{self.site_id}] list endpoint: {list_api}")

        try:
            while page <= self.SAFETY_PAGE_CAP:
                if limit is not None and saved >= limit:
                    break
                if self._budget_nearly_exhausted(started_at):
                    print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                    break

                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

                skip = (page - 1) * page_size
                payload = self._fetch_list_page(list_api, skip)
                if payload is None:
                    print(f"[{self.site_id}] list page {page} failed; stopping")
                    break

                page_size = self._safe_int(
                    (payload.get("paginationData") or {}).get("pageSize"),
                    default=page_size,
                )
                records = payload.get("data") or []
                if not records:
                    print(f"[{self.site_id}] list page {page} returned no records; stopping")
                    break

                new_on_page = 0
                for idx, record in enumerate(records, start=1):
                    if limit is not None and saved >= limit:
                        break
                    if self._budget_nearly_exhausted(started_at):
                        print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                        return saved

                    gnp = self._get_nested(record, "ids", "gnp")
                    detail_url = self._detail_url(gnp)
                    if not detail_url:
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    item_label = f"page {page} item {idx}"
                    try:
                        time.sleep(self.detail_delay)
                        parsed = self._parse_record(record, list_api)
                        abstract = parsed.get("abstract") or ""
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_label} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        paper = self._to_paper(parsed)
                        self._save_paper(paper)
                        saved += 1
                        counter = f"{saved}/{limit}" if limit is not None else str(saved)
                        print(f"[{self.site_id}] saved {counter}: {paper['title'][:90]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page} had no unseen URLs; stopping")
                    break

                pagination = payload.get("paginationData") or {}
                total = self._safe_int(pagination.get("totalResults"))
                returned_skip = self._safe_int(pagination.get("skip"), default=skip)
                if total is not None and returned_skip + len(records) >= total:
                    break

                page += 1

            if page > self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] reached safety page cap ({self.SAFETY_PAGE_CAP}); stopping")
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Discovery/list/detail
    # ------------------------------------------------------------------

    def _discover_list_api(self):
        raw = self._curl_get_text(
            self.START_URL,
            context="start page",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        if not raw:
            return self.FALLBACK_LIST_API

        soup = self._make_soup(raw, context="start page")
        if soup is not None:
            node = soup.find("wgl-news-list")
            api = node.get("api") if node else None
            if api:
                endpoint = urljoin(self.base_url, api)
                if not endpoint.endswith(".model.json"):
                    endpoint = endpoint.rstrip("/") + ".model.json"
                return endpoint

        match = re.search(r'<wgl-news-list\b[^>]*\sapi=["\']([^"\']+)["\']', raw)
        if match:
            endpoint = urljoin(self.base_url, unescape(match.group(1)))
            if not endpoint.endswith(".model.json"):
                endpoint = endpoint.rstrip("/") + ".model.json"
            return endpoint

        print(f"[{self.site_id}] could not discover list API; using fallback")
        return self.FALLBACK_LIST_API

    def _fetch_list_page(self, list_api, skip):
        sep = "&" if "?" in list_api else "?"
        url = f"{list_api}{sep}skip={skip}" if skip else list_api
        return self._curl_json(url, context=f"list skip={skip}", referer=self.START_URL)

    def _parse_record(self, record, list_api):
        ids = record.get("ids") or {}
        gnp = ids.get("gnp") or ids.get("uuid") or str(ids.get("damId") or "")
        if not gnp:
            raise RuntimeError("record has no gnp/uuid/damId")

        package_url = self.PACKAGE_API.format(gnp=quote(str(gnp), safe="-_"))
        assets_url = self.ASSETS_API.format(gnp=quote(str(gnp), safe="-_"))

        package = self._curl_json(
            package_url,
            context=f"package {gnp}",
            referer=self.START_URL,
        )
        if not isinstance(package, dict):
            package = self._fetch_detail_html_package(gnp) or record

        assets_payload = self._curl_json(
            assets_url,
            context=f"assets {gnp}",
            referer=package_url,
        )
        if not isinstance(assets_payload, dict):
            assets_payload = {"data": [], "total": 0}

        selected_asset, pdf_candidates = self._select_pdf_asset(assets_payload, package)
        source = package if isinstance(package, dict) else record
        source_desc = source.get("description") or {}
        source_bfs = source.get("bfs") or {}

        selected_desc = (selected_asset or {}).get("description") or {}
        selected_bfs = (selected_asset or {}).get("bfs") or {}

        selected_model = self._get_nested(selected_asset or {}, "bfs", "articleModel", "name")
        selected_title = self._get_nested(selected_asset or {}, "description", "titles", "main")
        package_title = self._get_nested(source, "description", "titles", "main")
        title = package_title or selected_title or "(untitled)"
        if selected_title and selected_model and self._is_primary_document_model(selected_model):
            title = selected_title

        listed_raw = source_bfs.get("embargo") or record.get("bfs", {}).get("embargo")
        published_raw = selected_bfs.get("embargo") or listed_raw
        listed_date = self._parse_date(listed_raw)
        published_date = self._parse_date(published_raw) or listed_date

        pdf_url, original_filename = self._extract_pdf(selected_asset)

        abstract = self._best_abstract(selected_desc, source_desc)
        category = self._category(source, selected_asset)
        keywords = self._keywords(source, selected_asset)
        publisher = self._publisher(source, selected_asset)
        authors = self._authors(source, selected_asset)
        bibliography = selected_desc.get("bibliography") or source_desc.get("bibliography") or {}
        doi = bibliography.get("doi") or bibliography.get("persistentIdentifier")

        post_number = str(ids.get("damId")) if ids.get("damId") else str(gnp)
        detail_url = self._detail_url(gnp)
        canonical_url = f"https://www.bfs.admin.ch/news/de/{gnp}" if gnp else detail_url

        metadata = self._build_metadata(
            record=record,
            package=source,
            selected_asset=selected_asset,
            pdf_candidates=pdf_candidates,
            list_api=list_api,
            package_url=package_url,
            assets_url=assets_url,
            detail_url=detail_url,
            canonical_url=canonical_url,
            listed_raw=listed_raw,
            original_filename=original_filename,
            category=category,
            keywords=keywords,
        )

        return {
            "external_id": str(gnp),
            "post_number": post_number,
            "title": self._clean_text(title) or "(untitled)",
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": "Bundesamt für Statistik",
            "journal": bibliography.get("journal") or bibliography.get("journalName"),
            "url": canonical_url,
            "detail_url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _fetch_detail_html_package(self, gnp):
        detail_url = self._detail_url(gnp)
        if not detail_url:
            return None
        raw = self._curl_get_text(
            detail_url,
            context=f"detail HTML {gnp}",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self.START_URL,
        )
        if not raw:
            return None
        soup = self._make_soup(raw, context=f"detail HTML {gnp}")
        if soup is None:
            return None
        node = soup.find("wgl-news-package-detail")
        if not node:
            return None
        raw_attr = node.get("packagegnpdata") or node.get(":packagegnpdata")
        if not raw_attr:
            return None
        try:
            return json.loads(unescape(raw_attr))
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] detail HTML JSON parse failed for {gnp}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def _to_paper(self, parsed):
        external_id = parsed["external_id"]
        metadata = parsed.get("metadata") or {}
        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("posted_date"),
            "authors": parsed.get("authors"),
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department"),
            "journal": parsed.get("journal"),
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def _build_metadata(
        self,
        *,
        record,
        package,
        selected_asset,
        pdf_candidates,
        list_api,
        package_url,
        assets_url,
        detail_url,
        canonical_url,
        listed_raw,
        original_filename,
        category,
        keywords,
    ):
        ids = package.get("ids") or record.get("ids") or {}
        selected_ids = (selected_asset or {}).get("ids") or {}
        selected_desc = (selected_asset or {}).get("description") or {}
        selected_bib = selected_desc.get("bibliography") or {}
        package_desc = package.get("description") or {}
        package_bib = package_desc.get("bibliography") or {}
        bibliography = selected_bib or package_bib
        categorization = selected_desc.get("categorization") or package_desc.get("categorization") or {}

        return {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": bibliography.get("journal") or bibliography.get("journalName"),
            "series": self._names(categorization.get("serie")) or bibliography.get("series"),
            "volume": bibliography.get("volume"),
            "issue": bibliography.get("issue"),
            "gnp": ids.get("gnp"),
            "damId": ids.get("damId"),
            "uuid": ids.get("uuid"),
            "contentId": ids.get("contentId"),
            "languageCopyId": ids.get("languageCopyId"),
            "post_number": str(ids.get("damId")) if ids.get("damId") else ids.get("gnp"),
            "asset_dam_id": selected_ids.get("damId"),
            "asset_uuid": selected_ids.get("uuid"),
            "asset_content_id": selected_ids.get("contentId"),
            "asset_orderNr": self._get_nested(selected_asset or {}, "shop", "orderNr"),
            "category": category,
            "keywords": keywords,
            "articleModel": package.get("bfs", {}).get("articleModel"),
            "articleModelGroup": package.get("bfs", {}).get("articleModelGroup"),
            "selectedArticleModel": (selected_asset or {}).get("bfs", {}).get("articleModel"),
            "selectedArticleModelGroup": (selected_asset or {}).get("bfs", {}).get("articleModelGroup"),
            "categorization": categorization,
            "bibliography": bibliography,
            "links": package.get("links") or [],
            "selected_asset_links": (selected_asset or {}).get("links") or [],
            "pdf_candidates": pdf_candidates,
            "list_api_url": list_api,
            "detail_api_url": package_url,
            "assets_api_url": assets_url,
            "detail_page_url": detail_url,
            "canonical_url": canonical_url,
            "raw_record": self._compact_raw(record),
            "raw_package": self._compact_raw(package),
            "raw_selected_asset": self._compact_raw(selected_asset or {}),
        }

    # ------------------------------------------------------------------
    # Field parsers
    # ------------------------------------------------------------------

    def _select_pdf_asset(self, assets_payload, package):
        candidates = []
        for asset in assets_payload.get("data") or []:
            master_link = None
            for link in asset.get("links") or []:
                href = link.get("href") or ""
                fmt = (link.get("format") or "").lower()
                rel = (link.get("rel") or "").lower()
                if fmt == "pdf" or href.lower().endswith(".pdf") or (rel == "master" and fmt == "pdf"):
                    master_link = link
                    break
            if not master_link:
                continue

            model = self._get_nested(asset, "bfs", "articleModel", "name") or ""
            model_group = self._get_nested(asset, "bfs", "articleModelGroup", "name") or ""
            model_code = self._get_nested(asset, "bfs", "articleModel", "code") or ""
            title = self._get_nested(asset, "description", "titles", "main") or ""
            order_nr = self._get_nested(asset, "shop", "orderNr") or ""
            gnp = self._get_nested(package, "ids", "gnp") or self._get_nested(asset, "ids", "gnp") or ""

            haystack = " ".join([model, model_group, model_code, title, order_nr]).lower()
            score = 0
            if "medienmitteilung" in haystack or model_code == "MM":
                score += 100
            if "publikation" in haystack or "publication" in haystack:
                score += 80
            if gnp and str(order_nr).startswith(str(gnp)):
                score += 50
            if "infografik" in haystack or "diagramm" in haystack or "karte" in haystack:
                score -= 25
            score += min(self._safe_int(master_link.get("size"), 0) or 0, 2_000_000) // 100_000

            candidates.append({
                "score": score,
                "asset": asset,
                "pdf_url": master_link.get("href"),
                "format": master_link.get("format"),
                "size": master_link.get("size"),
                "damId": self._get_nested(asset, "ids", "damId"),
                "gnp": self._get_nested(asset, "ids", "gnp"),
                "title": title,
                "model": model,
                "model_code": model_code,
                "model_group": model_group,
                "orderNr": order_nr,
            })

        if not candidates:
            return None, []

        candidates.sort(key=lambda item: item["score"], reverse=True)
        compact = [
            {k: v for k, v in cand.items() if k != "asset"}
            for cand in candidates[:10]
        ]
        return candidates[0]["asset"], compact

    def _extract_pdf(self, selected_asset):
        if not selected_asset:
            return None, None
        pdf_url = None
        for link in selected_asset.get("links") or []:
            href = link.get("href") or ""
            fmt = (link.get("format") or "").lower()
            rel = (link.get("rel") or "").lower()
            if fmt == "pdf" or href.lower().endswith(".pdf") or (rel == "master" and fmt == "pdf"):
                pdf_url = href
                break
        if not pdf_url:
            return None, None

        order_nr = self._get_nested(selected_asset, "shop", "orderNr")
        filename = f"{order_nr}.pdf" if order_nr else None
        if not filename:
            tail = unquote(urlparse(pdf_url).path.rstrip("/").split("/")[-1])
            filename = tail if tail and "." in tail and tail.lower() != "master" else None
        return pdf_url, filename

    def _best_abstract(self, *descriptions):
        candidates = []
        for desc in descriptions:
            if not isinstance(desc, dict):
                continue
            for key in ("summary", "shortSummary", "shortTextGnp"):
                value = desc.get(key)
                if isinstance(value, dict):
                    text = value.get("raw") or value.get("html")
                else:
                    text = value
                text = self._clean_text(text)
                if text:
                    candidates.append(text)
            if desc.get("abstractShort"):
                candidates.append(self._clean_text(desc.get("abstractShort")))
            abstract_parts = []
            for part in desc.get("abstract") or []:
                if isinstance(part, dict):
                    text = self._clean_text(part.get("text"))
                    if text:
                        abstract_parts.append(text)
                elif part:
                    abstract_parts.append(self._clean_text(part))
            if abstract_parts:
                candidates.append(" ".join(abstract_parts))
        candidates = [c for c in candidates if c]
        if not candidates:
            return ""
        return max(candidates, key=len)

    def _category(self, package, selected_asset):
        selected_model = self._get_nested(selected_asset or {}, "bfs", "articleModel", "name")
        package_model = self._get_nested(package, "bfs", "articleModel", "name")
        prodima = self._names(
            self._get_nested(package, "description", "categorization", "prodima")
        )
        parts = [p for p in (selected_model, package_model, prodima) if p]
        return " | ".join(dict.fromkeys(parts)) if parts else None

    def _keywords(self, package, selected_asset):
        items = []
        for source in (package, selected_asset or {}):
            cat = self._get_nested(source, "description", "categorization") or {}
            for key in ("tags", "prodima", "inquiry", "spatialdivision", "contentArticleModels"):
                items.extend(self._names_list(cat.get(key)))
            seo = source.get("seo") or {}
            for kw in seo.get("metaKeywords") or []:
                if kw:
                    items.append(str(kw).strip())
        return ", ".join(dict.fromkeys([x for x in items if x])) or None

    def _publisher(self, package, selected_asset):
        items = []
        for source in (selected_asset or {}, package):
            cat = self._get_nested(source, "description", "categorization") or {}
            items.extend(self._names_list(cat.get("publisher")))
            items.extend(self._names_list(cat.get("institution")))
        if not items:
            items.append("Bundesamt für Statistik")
        return "; ".join(dict.fromkeys([x for x in items if x])) or None

    def _authors(self, package, selected_asset):
        items = []
        for source in (selected_asset or {}, package):
            bib = self._get_nested(source, "description", "bibliography") or {}
            author = bib.get("author") or bib.get("authors")
            if isinstance(author, list):
                items.extend(str(a).strip() for a in author if str(a).strip())
            elif author:
                items.extend(re.split(r";|,\s+(?=[A-ZÄÖÜ])", str(author)))
        return "; ".join(dict.fromkeys([a.strip() for a in items if a.strip()])) or None

    def _detail_url(self, gnp):
        if not gnp:
            return None
        return f"{self.base_url}/bfs/de/home.gnpdetail.{gnp}.html"

    @staticmethod
    def _is_primary_document_model(model_name):
        lowered = (model_name or "").lower()
        return "medienmitteilung" in lowered or "publikation" in lowered or "publication" in lowered

    # ------------------------------------------------------------------
    # Network/HTML helpers
    # ------------------------------------------------------------------

    def _curl_json(self, url, context, referer=None):
        raw = self._curl_get_text(
            url,
            context=context,
            referer=referer,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid JSON for {context}: {exc}")
            return None

    def _curl_get_text(self, url, context, referer=None, accept=None):
        cmd = [
            "curl",
            "-skL",
            "--tls-max",
            "1.3",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
        ]
        if accept:
            cmd.extend(["-H", f"Accept: {accept}"])
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                err = result.stderr.decode("utf-8", errors="replace").strip()
                if attempt == len(self.BACKOFF_SECONDS):
                    print(
                        f"[{self.site_id}] curl failed for {context} "
                        f"after {attempt} attempts: {err or 'empty response'}"
                    )
                    return None
                print(f"[{self.site_id}] curl issue for {context}; retry in {wait}s")
                time.sleep(wait)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if attempt == len(self.BACKOFF_SECONDS):
                    print(f"[{self.site_id}] curl failed for {context} after {attempt} attempts: {exc}")
                    return None
                print(f"[{self.site_id}] curl error for {context}: {exc}; retry in {wait}s")
                time.sleep(wait)
        return None

    def _make_soup(self, raw, context="HTML"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup

                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                continue
        print(f"[{self.site_id}] {context}: BeautifulSoup failed: {last_exc}")
        return None

    def _clean_text(self, value):
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        value = unescape(value)
        if "<" in value and ">" in value:
            soup = self._make_soup(value, context="text fragment")
            if soup is not None:
                value = soup.get_text(" ", strip=True)
            else:
                value = re.sub(r"<[^>]+>", " ", value)
        value = value.replace("\xa0", " ")
        return re.sub(r"\s+", " ", value).strip()

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw):
        if not raw:
            return None
        text = str(raw).strip()
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if match:
            day, month, year = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return None

    @staticmethod
    def _safe_int(value, default=None):
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _get_nested(obj, *keys):
        current = obj
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    @classmethod
    def _names(cls, value):
        names = cls._names_list(value)
        return "; ".join(dict.fromkeys(names)) if names else None

    @staticmethod
    def _names_list(value):
        if not value:
            return []
        if isinstance(value, dict):
            value = [value]
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict):
                    label = item.get("name") or item.get("label") or item.get("code")
                    code = item.get("code")
                    if label and code and code not in str(label):
                        result.append(f"{label} {code}".strip())
                    elif label:
                        result.append(str(label).strip())
                elif item:
                    result.append(str(item).strip())
            return [r for r in result if r]
        return [str(value).strip()]

    @staticmethod
    def _compact_raw(value):
        """Keep useful native fields without storing huge full package lists."""
        if not isinstance(value, dict):
            return value
        keep = {}
        for key in ("ids", "bfs", "description", "shop", "seo", "links"):
            if key in value:
                keep[key] = value[key]
        if "paginationData" in value:
            keep["paginationData"] = value["paginationData"]
        return keep

    def _budget_nearly_exhausted(self, started_at):
        return time.time() - started_at > self.WALL_CLOCK_BUDGET_SECONDS - 30


if __name__ == "__main__":
    from crawler import db as dbm

    conn = dbm.get_db()
    dbm.init_db(conn)
    crawler = BfsAdminChBfsCrawler(db_conn=conn)
    dbm.register_site(conn, crawler.site_id, crawler.site_name, crawler.base_url)
    crawler.crawl()
