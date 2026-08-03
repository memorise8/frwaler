# -*- coding: utf-8 -*-
"""Crawler for Natural England / DEFRA Open Data ArcGIS Hub (dataset collection).

API: https://naturalengland-defra.opendata.arcgis.com/api/search/v1/collections/dataset/items
Pagination: follows 'next' link with ?limit=N&startindex=M
Total: ~183 datasets (as of 2026-05)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from html import unescape

from crawler.base_crawler import BaseCrawler

_SITE_ID = "naturalengland-defra-opendata-arcgis-com-search"
_BASE_URL = "https://naturalengland-defra.opendata.arcgis.com"
_API_BASE = f"{_BASE_URL}/api/search/v1/collections/dataset/items"
_PAGE_SIZE = 10
_SAFETY_CAP_PAGES = 200
_MIN_ABSTRACT = 100


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    # Block-level tags → newlines for readability
    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(
        r"</?(p|div|h[1-6]|li|tr|td|th|ul|ol|blockquote)[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode entities
    text = unescape(text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _ms_to_date(ms) -> str | None:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _curl_json(url: str, retries: int = 3):
    """Fetch URL via curl with exponential backoff. Returns parsed JSON or None."""
    for attempt in range(retries):
        if attempt > 0:
            wait = 3 ** attempt  # 3s, 9s
            print(f"[{_SITE_ID}] retry {attempt}/{retries - 1} in {wait}s")
            time.sleep(wait)
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "60", url],
                capture_output=True,
                timeout=65,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")[:200]
                print(f"[{_SITE_ID}] curl rc={result.returncode}: {stderr}")
                continue
            raw = result.stdout.decode("utf-8", errors="replace")
            if not raw.strip():
                print(f"[{_SITE_ID}] empty response from {url}")
                continue
            return json.loads(raw)
        except subprocess.TimeoutExpired:
            print(f"[{_SITE_ID}] curl timed out: {url}")
        except json.JSONDecodeError as exc:
            print(f"[{_SITE_ID}] JSON parse error: {exc}")
        except Exception as exc:
            print(f"[{_SITE_ID}] fetch error: {exc}")
    return None


class NaturalEnglandArcgisSearchCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: naturalengland-defra-opendata-arcgis-com-search"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        saved = 0
        seen_ids: set[str] = set()
        start_time = time.time()
        limit_val = limit if limit is not None else float("inf")

        page = 0
        next_url: str | None = f"{_API_BASE}?limit={_PAGE_SIZE}"

        while next_url and page < _SAFETY_CAP_PAGES:
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-min wall-clock budget reached, stopping.")
                break

            if saved >= limit_val:
                break

            page += 1
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_val}")

            data = _curl_json(next_url)
            if data is None:
                print(f"[{_SITE_ID}] page {page} fetch failed, stopping.")
                break

            features = data.get("features", [])
            if not features:
                print(f"[{_SITE_ID}] page {page}: no features, end of results.")
                break

            # Advance pagination before processing so we don't lose it
            next_url = None
            for link in data.get("links", []):
                if link.get("rel") == "next":
                    next_url = link.get("href")
                    break

            for feature in features:
                if saved >= limit_val:
                    break

                item_id = feature.get("id", "")
                try:
                    if not item_id:
                        continue

                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    props = feature.get("properties") or {}
                    title = (props.get("title") or "").strip()
                    if not title:
                        print(f"[{_SITE_ID}] item {item_id}: no title, skipping.")
                        continue

                    # Abstract: strip HTML from description, fall back to snippet
                    desc_html = props.get("description") or ""
                    abstract = _strip_html(desc_html)

                    if len(abstract) < _MIN_ABSTRACT:
                        snippet = props.get("snippet") or ""
                        snippet_text = _strip_html(snippet)
                        if len(snippet_text) > len(abstract):
                            abstract = snippet_text

                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{_SITE_ID}] item {item_id}: abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    tags = props.get("tags") or []
                    keywords = ", ".join(str(t) for t in tags if t) if tags else None

                    categories = props.get("categories") or []
                    cat_names = []
                    for c in categories:
                        if c and isinstance(c, str):
                            if "/" in c:
                                cat_names.append(c.rstrip("/").split("/")[-1])
                            else:
                                cat_names.append(c)
                    category = ", ".join(cat_names) if cat_names else None

                    created_ms = props.get("created")
                    modified_ms = props.get("modified")
                    published_date = _ms_to_date(created_ms)
                    listed_date = _ms_to_date(modified_ms)

                    owner = props.get("owner") or ""
                    publisher = owner or None
                    detail_url = f"{_BASE_URL}/datasets/{item_id}"

                    metadata_dict: dict = {
                        "item_type": props.get("type"),
                        "orgId": props.get("orgId"),
                        "owner": owner,
                        "accessInformation": props.get("accessInformation"),
                        "numViews": props.get("numViews"),
                        "numRatings": props.get("numRatings"),
                        "avgRating": props.get("avgRating"),
                        "size": props.get("size"),
                        "typeKeywords": props.get("typeKeywords"),
                        "categories_raw": categories,
                        "snippet": props.get("snippet"),
                        "created_ms": created_ms,
                        "modified_ms": modified_ms,
                        "posted_date": listed_date,
                    }

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": item_id,
                        "post_number": item_id,
                        "title": title,
                        "abstract": abstract,
                        "url": detail_url,
                        "pdf_url": None,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": None,
                        "publisher": publisher,
                        "department": None,
                        "journal": None,
                        "keywords": keywords,
                        "category": category,
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    time.sleep(0.1)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_id or '?'} failed: {exc}")
                    continue

        if page >= _SAFETY_CAP_PAGES:
            print(f"[{_SITE_ID}] Safety cap of {_SAFETY_CAP_PAGES} pages reached.")

        print(f"[{_SITE_ID}] Done. Saved {saved} items.")
        return saved
