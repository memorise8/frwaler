# -*- coding: utf-8 -*-
"""Crawler for julkaisut.valtioneuvosto.fi (DSpace 7 REST API).

Finnish Government Publications repository.
API base: https://julkaisut.valtioneuvosto.fi/server/api
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_SITE_ID = "julkaisut-valtioneuvosto-fi-search"
_BASE_URL = "https://julkaisut.valtioneuvosto.fi"
_API = f"{_BASE_URL}/server/api"
_PAGE_SIZE = 20


def _curl_json(url, max_retries=3):
    """Fetch URL via curl with TLS tolerance and JSON parse. Returns dict/list or None."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk",
                 "-H", "Accept: application/json",
                 url],
                capture_output=True, timeout=30,
            )
            raw = result.stdout
            if raw:
                return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            last_exc = exc
            break  # malformed JSON — no point retrying
        except Exception as exc:
            last_exc = exc
        if attempt < max_retries - 1:
            wait = [1, 3, 9][attempt]
            print(f"[{_SITE_ID}] curl retry {attempt + 1}/{max_retries} in {wait}s "
                  f"for {url}: {last_exc}")
            time.sleep(wait)
    print(f"[{_SITE_ID}] Failed to fetch {url}: {last_exc}")
    return None


def _meta_vals(metadata, key, all_values=False):
    """Extract value(s) from a DSpace metadata dict entry."""
    entries = metadata.get(key, [])
    vals = [e.get("value", "") for e in entries if e.get("value")]
    if all_values:
        return vals
    return vals[0] if vals else None


def _parse_multilang(val_str):
    """Parse DSpace multilingual strings: 'fi=Nimi|sv=Namn|en=Name|' → English preferred."""
    if not val_str or "=" not in val_str or "|" not in val_str:
        return val_str
    parts = {}
    for seg in val_str.split("|"):
        if "=" in seg:
            lang, _, text = seg.partition("=")
            if text.strip():
                parts[lang.strip()] = text.strip()
    return parts.get("en") or parts.get("fi") or next(iter(parts.values()), val_str)


def _get_pdf_info(item_uuid):
    """Return (pdf_url, original_filename) by walking ORIGINAL bundle → bitstreams.

    Makes at most 2 extra API calls per item.
    Returns (None, None) when no PDF is found.
    """
    data = _curl_json(f"{_API}/core/items/{item_uuid}/bundles")
    if not data:
        return None, None
    for bundle in data.get("_embedded", {}).get("bundles", []):
        if bundle.get("name") != "ORIGINAL":
            continue
        bits_href = bundle.get("_links", {}).get("bitstreams", {}).get("href")
        if not bits_href:
            continue
        bits_data = _curl_json(bits_href)
        if not bits_data:
            continue
        for bit in bits_data.get("_embedded", {}).get("bitstreams", []):
            name = bit.get("name", "")
            content_url = bit.get("_links", {}).get("content", {}).get("href", "")
            mime_entries = bit.get("metadata", {}).get("dc.format.mimetype", [])
            mime = mime_entries[0].get("value", "") if mime_entries else ""
            if content_url and (name.lower().endswith(".pdf") or "pdf" in mime.lower()):
                return content_url, name
    return None, None


class JulkaisutValtioneuvastoFiSearchCrawler(BaseCrawler):
    site_id = "julkaisut-valtioneuvosto-fi-search"
    site_name = "Custom: julkaisut-valtioneuvosto-fi-search"
    base_url = "https://julkaisut.valtioneuvosto.fi"

    def crawl(self, limit=None):
        """Crawl julkaisut.valtioneuvosto.fi via DSpace 7 REST API.

        Walks search pages sorted by dc.date.issued DESC.
        For each item, fetches PDF URL via ORIGINAL bundle → bitstreams.
        """
        saved = 0
        seen_urls = set()
        limit_val = limit if limit is not None else float("inf")
        start_time = time.time()
        max_wall_secs = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes hard cap

        page = 0
        max_pages = 200

        while True:
            # Safety exits
            if time.time() - start_time > max_wall_secs:
                print(f"[{self.site_id}] Wall-clock budget exceeded, stopping cleanly.")
                break
            if page >= max_pages:
                print(f"[{self.site_id}] Safety cap of {max_pages} pages reached, stopping.")
                break
            if saved >= limit_val:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/"
                      f"{limit if limit is not None else 'inf'}")

            url = (
                f"{_API}/discover/search/objects"
                f"?query=*&sort=dc.date.issued%2CDESC&page={page}&size={_PAGE_SIZE}"
            )
            data = _curl_json(url)
            if data is None:
                print(f"[{self.site_id}] Page {page} fetch failed, stopping.")
                break

            objects = (
                data.get("_embedded", {})
                    .get("searchResult", {})
                    .get("_embedded", {})
                    .get("objects", [])
            )

            if not objects:
                print(f"[{self.site_id}] No objects on page {page}, stopping.")
                break

            new_on_page = 0

            for obj in objects:
                if saved >= limit_val:
                    break

                try:
                    item = obj.get("_embedded", {}).get("indexableObject", {})
                    uuid = item.get("uuid", "")
                    if not uuid:
                        continue

                    handle = item.get("handle", "")  # e.g. "11111/13595"
                    detail_url = (
                        f"{self.base_url}/handle/{handle}" if handle
                        else f"{self.base_url}/items/{uuid}"
                    )

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    meta = item.get("metadata", {})

                    title = item.get("name") or _meta_vals(meta, "dc.title")
                    if not title:
                        continue

                    abstract = _meta_vals(meta, "dc.description.abstract")
                    if not abstract or len(abstract) < 50:
                        print(f"[{self.site_id}] Skip '{str(title)[:40]}': "
                              f"abstract too short ({len(abstract or '')} chars)")
                        continue

                    # Dates
                    issued_date = _meta_vals(meta, "dc.date.issued")
                    acc_raw = _meta_vals(meta, "dc.date.accessioned")
                    listed_date = acc_raw.split("T")[0] if acc_raw and "T" in acc_raw else acc_raw

                    # Authors: personal + group
                    person_authors = _meta_vals(meta, "dc.contributor.author", all_values=True) or []
                    group_authors = _meta_vals(meta, "dc.contributor.groupauthor", all_values=True) or []
                    all_authors = person_authors + group_authors
                    authors_str = "; ".join(all_authors) if all_authors else None

                    # Publisher (multilang)
                    pub_raw = _meta_vals(meta, "dc.publisher")
                    publisher_str = _parse_multilang(pub_raw) if pub_raw else None

                    # Keywords
                    kw_list = _meta_vals(meta, "dc.subject", all_values=True) or []
                    keywords_str = ", ".join(kw_list) if kw_list else None

                    # DOI
                    doi = _meta_vals(meta, "dc.identifier.doi")

                    # Document type
                    type_raw = _meta_vals(meta, "dc.type")
                    doc_type = _parse_multilang(type_raw) if type_raw else None

                    # Series / journal
                    series_raw = _meta_vals(meta, "dc.relation.ispartofseries")
                    journal = _parse_multilang(series_raw) if series_raw else None

                    # Department / organization
                    org_raw = _meta_vals(meta, "dc.contributor.organization")
                    dept = _parse_multilang(org_raw) if org_raw else None

                    # post_number: numeric tail of handle, e.g. "11111/13595" → "13595"
                    if handle:
                        post_number = handle.split("/")[-1]
                        external_id = post_number  # dedup key; handle is unique per item
                    else:
                        post_number = uuid
                        external_id = uuid

                    # PDF URL — 2 API calls (bundles + bitstreams)
                    time.sleep(self._delay)
                    pdf_url, orig_filename = _get_pdf_info(uuid)

                    # Metadata: all unmapped fields + native fields
                    skip_meta_keys = {
                        "dc.title", "dc.description.abstract",
                        "dc.date.issued", "dc.date.accessioned",
                        "dc.contributor.author", "dc.contributor.groupauthor",
                        "dc.publisher", "dc.subject", "dc.identifier.doi",
                        "dc.type", "dc.relation.ispartofseries",
                        "dc.contributor.organization",
                    }
                    extra_meta = {
                        "uuid": uuid,
                        "handle": handle,
                        "posted_date": listed_date,
                        "originalFilename": orig_filename,
                    }
                    if series_raw:
                        extra_meta["series"] = series_raw
                    if doi:
                        extra_meta["doi"] = doi
                    if type_raw:
                        extra_meta["doc_type_raw"] = type_raw
                    for k, v in meta.items():
                        if k in skip_meta_keys:
                            continue
                        vals = [e.get("value", "") for e in v if e.get("value")]
                        if vals:
                            extra_meta[k] = vals[0] if len(vals) == 1 else vals

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": issued_date,
                        "posted_date": listed_date,   # adapter reads this → posted_date col
                        "listed_date": listed_date,   # libertree v2 listed_date col
                        "authors": authors_str,
                        "publisher": publisher_str,
                        "department": dept,
                        "journal": journal,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "keywords": keywords_str,
                        "category": doc_type,
                        "doi": doi,
                        "original_filename": orig_filename,
                        "metadata": json.dumps(extra_meta, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page}, stopping.")
                break

            page += 1
            time.sleep(0.3)  # brief pause between pages

        return saved
