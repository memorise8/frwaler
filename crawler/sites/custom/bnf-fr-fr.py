# -*- coding: utf-8 -*-
"""Crawler for BnF (Bibliothèque nationale de France) espace presse.

Uses the Drupal 8 JSON API endpoint with nested media includes so all data
(metadata + PDF attachment URL) arrives in a single paginated call without
any per-item detail-page fetches.

API: https://www.bnf.fr/fr/jsonapi/node/espace_presse
Pagination: page[offset] / page[limit] via links.next in each response.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_API_BASE = "https://www.bnf.fr/fr/jsonapi/node/espace_presse"
_SITE_BASE = "https://www.bnf.fr"
_PAGE_SIZE = 50
_MAX_PAGES = 200
_MAX_SECONDS = 25 * 60
_BACKOFF = (1, 3, 9)
_MIN_ABSTRACT = 50


class BnfFrFrCrawler(BaseCrawler):
    site_id = "bnf-fr-fr"
    site_name = "Custom: bnf-fr-fr"
    base_url = "https://www.bnf.fr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3):
        """Fetch URL bytes via curl; returns bytes or None on failure."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-skL", url],
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                print(
                    f"[bnf-fr-fr] curl rc={result.returncode} attempt "
                    f"{attempt+1}/{retries} for {url}"
                )
            except Exception as exc:
                print(
                    f"[bnf-fr-fr] curl error attempt {attempt+1}/{retries}: {exc}"
                )
            if attempt < retries - 1:
                time.sleep(_BACKOFF[attempt])
        return None

    def _fetch_json(self, url: str) -> dict | None:
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"[bnf-fr-fr] JSON parse error for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html: str | None) -> str:
        if not html:
            return ""
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&[a-z]+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _parse_date(iso_str: str | None) -> str | None:
        if not iso_str:
            return None
        return iso_str[:10]

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()

        url = (
            f"{_API_BASE}"
            f"?page%5Blimit%5D={_PAGE_SIZE}"
            f"&include=field_media1,field_media1.field_document"
        )
        page_num = 0

        while url and page_num < _MAX_PAGES:
            if time.time() - start_time > _MAX_SECONDS:
                print("[bnf-fr-fr] 25-minute wall-clock budget reached, stopping.")
                break

            page_num += 1
            if page_num == 1 or page_num % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[bnf-fr-fr] page {page_num}: saved {saved}/{lim_str}")

            if saved >= limit_or_inf:
                break

            data = self._fetch_json(url)
            if not data:
                print(f"[bnf-fr-fr] Failed to fetch page {page_num}, stopping.")
                break

            # Build lookup maps from included resources
            # file_uuid -> (url_path, filename)
            file_map: dict[str, tuple[str, str]] = {}
            # media_uuid -> file_uuid
            media_to_file: dict[str, str] = {}

            for inc in data.get("included", []):
                inc_type = inc.get("type", "")
                if inc_type == "file--file":
                    fid = inc["id"]
                    uri = inc.get("attributes", {}).get("uri", {})
                    url_path = uri.get("url", "") if isinstance(uri, dict) else ""
                    fname = inc.get("attributes", {}).get("filename", "") or ""
                    file_map[fid] = (url_path, fname)
                elif inc_type == "media--documents":
                    mid = inc["id"]
                    fdoc = (
                        inc.get("relationships", {})
                        .get("field_document", {})
                        .get("data")
                    )
                    if isinstance(fdoc, dict) and fdoc.get("id"):
                        media_to_file[mid] = fdoc["id"]

            items = data.get("data", [])
            if not items:
                print(f"[bnf-fr-fr] Empty page {page_num}, stopping.")
                break

            new_on_page = 0

            for item in items:
                if saved >= limit_or_inf:
                    break

                try:
                    attrs = item.get("attributes", {}) or {}
                    nid = attrs.get("drupal_internal__nid")
                    uuid = item.get("id", "")

                    detail_url = f"{_SITE_BASE}/fr/node/{nid}"
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    title = (attrs.get("title") or "").strip()
                    if not title:
                        print(f"[bnf-fr-fr] Skipping nid={nid}: no title")
                        continue

                    # Abstract: prefer solr processed (strip HTML), fallback to field_longtext1
                    solr = attrs.get("field_index_solr") or {}
                    abstract = self._strip_html(
                        (solr.get("processed") if isinstance(solr, dict) else None) or ""
                    )
                    if len(abstract) < 100:
                        fallback = (attrs.get("field_longtext1") or "").strip()
                        if len(fallback) > len(abstract):
                            abstract = fallback

                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[bnf-fr-fr] Skipping nid={nid}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Dates
                    published_date = self._parse_date(attrs.get("created"))
                    changed_date = self._parse_date(attrs.get("changed"))

                    # PDF URL + filename via included media→file chain
                    pdf_url = None
                    pdf_name = None
                    media_rel = (
                        item.get("relationships", {})
                        .get("field_media1", {})
                        .get("data")
                    )
                    if isinstance(media_rel, dict):
                        mid = media_rel.get("id")
                        fid = media_to_file.get(mid) if mid else None
                        if fid and fid in file_map:
                            url_path, pdf_name = file_map[fid]
                            if url_path:
                                pdf_url = _SITE_BASE + url_path
                                if not pdf_name:
                                    pdf_name = url_path.rstrip("/").split("/")[-1]

                    # Category from download label field
                    category = (attrs.get("field_texte_telechargement") or "").strip() or None

                    metadata_dict = {
                        "node_id": str(nid),
                        "uuid": uuid,
                        "changed": changed_date,
                        "field_list6": attrs.get("field_list6"),
                        "moderation_state": attrs.get("moderation_state"),
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": uuid,
                        "post_number": str(nid),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": pdf_name or None,
                        "category": category,
                        "keywords": None,
                        "publisher": "Bibliothèque nationale de France",
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    nid_str = str(attrs.get("drupal_internal__nid", "?")) if "attrs" in dir() else "?"
                    print(f"[bnf-fr-fr] item {nid_str} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[bnf-fr-fr] No new items on page {page_num}, stopping.")
                break

            # Advance to next page via links.next
            next_link = data.get("links", {}).get("next", {})
            if isinstance(next_link, dict):
                url = next_link.get("href") or None
            elif isinstance(next_link, str):
                url = next_link
            else:
                url = None

            time.sleep(self._delay)

        if page_num >= _MAX_PAGES:
            print(f"[bnf-fr-fr] Safety cap of {_MAX_PAGES} pages reached, stopping.")

        lim_str = str(limit) if limit is not None else "∞"
        print(f"[bnf-fr-fr] Done: saved {saved} records (limit={lim_str}, pages={page_num}).")
        return saved
