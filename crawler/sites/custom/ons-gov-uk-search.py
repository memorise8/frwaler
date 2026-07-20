# -*- coding: utf-8 -*-
"""ONS (Office for National Statistics) search crawler.

Starting URL:
    https://www.ons.gov.uk/search?q=uk+wide&page=1&filter=time_series&filter=datasets&filter=user_requested_data

API endpoint:
    https://api.beta.ons.gov.uk/v1/search
    (JSON, supports content_type filter, offset-based pagination)
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.ons.gov.uk"
_API_BASE = "https://api.beta.ons.gov.uk"
_SITE_ID = "ons-gov-uk-search"
_PAGE_SIZE = 10
_ABSTRACT_MIN = 100   # skip items whose abstract is below this
_RATE_SLEEP = 1.0
_SAFETY_CAP = 200     # max pages before forced stop


def _curl_get(url: str, *, accept: str = "application/json",
              timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-max 1.3; retry on failure."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", str(timeout),
        "-H", f"Accept: {accept}",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        if attempt > 0:
            wait = 1 * (3 ** (attempt - 1))   # 1 s, 3 s, 9 s
            time.sleep(wait)
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            print(
                f"[{_SITE_ID}] empty response for {url}, "
                f"attempt {attempt + 1}/{retries}"
            )
        except Exception as exc:
            print(
                f"[{_SITE_ID}] curl error for {url}: {exc}, "
                f"attempt {attempt + 1}/{retries}"
            )
    return None


def _parse_date(raw: str) -> str:
    """Convert ISO datetime or '12 November 2015' to YYYY-MM-DD."""
    if not raw:
        return ""
    # ISO with fractional seconds: 2016-01-07T14:07:17.639Z
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    # "12 November 2015"
    try:
        return datetime.strptime(raw.strip(), "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    return ""


class OnsGovUkSearchCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: ons-gov-uk-search"
    base_url = _BASE

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _search_page(self, offset: int) -> dict | None:
        """Fetch one page of search results from the ONS beta API."""
        url = (
            f"{_API_BASE}/v1/search"
            f"?q=uk+wide"
            f"&limit={_PAGE_SIZE}"
            f"&offset={offset}"
            f"&content_type=dataset"
            f"&content_type=timeseries"
            f"&content_type=user_requested_data"
        )
        raw = _curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{_SITE_ID}] JSON parse error at offset {offset}: {exc}")
            return None

    def _fetch_detail(self, uri: str) -> dict | None:
        """Fetch {base}{uri}/data for downloads and description detail."""
        url = f"{_BASE}{uri}/data"
        raw = _curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _process_item(self, item: dict) -> bool:
        """Parse, enrich and save one search result item.

        Returns True if the item was saved, False if skipped.
        """
        uri = item.get("uri", "")
        if not uri:
            return False

        item_url = f"{_BASE}{uri}"

        # --- Abstract ---------------------------------------------------
        abstract = (
            item.get("summary") or item.get("meta_description") or ""
        ).strip()

        detail = None   # lazy-load once

        # If summary from search API is too short, try the /data endpoint.
        if len(abstract) < _ABSTRACT_MIN:
            detail = self._fetch_detail(uri)
            if detail:
                desc = detail.get("description", {})
                richer = (
                    desc.get("summary") or desc.get("metaDescription") or ""
                ).strip()
                if richer:
                    abstract = richer

        if len(abstract) < 50:
            print(f"[{_SITE_ID}] skip (abstract {len(abstract)} chars): {item_url}")
            return False

        if len(abstract) < _ABSTRACT_MIN:
            print(
                f"[{_SITE_ID}] skip (abstract {len(abstract)} < {_ABSTRACT_MIN} chars): "
                f"{item_url}"
            )
            return False

        # --- Detail page (for downloads / richer metadata) --------------
        if detail is None:
            detail = self._fetch_detail(uri)

        desc = {}
        downloads = []
        if detail:
            desc = detail.get("description", {})
            downloads = detail.get("downloads", [])

        # --- PDF URL ----------------------------------------------------
        pdf_url = None
        original_filename = None
        if downloads:
            fname = downloads[0].get("file", "")
            if fname:
                pdf_url = f"{item_url}/{fname}"
                original_filename = fname

        # --- IDs --------------------------------------------------------
        uri_parts = [p for p in uri.split("/") if p]
        # external_id: last two path segments (unique enough without full path)
        external_id = (
            "/".join(uri_parts[-2:]) if len(uri_parts) >= 2
            else (uri_parts[-1] if uri_parts else uri)
        )
        # post_number: dataset_id or cdid from the API item
        post_number = (
            item.get("dataset_id") or item.get("cdid") or external_id or None
        )

        # --- Dates ------------------------------------------------------
        release_raw = item.get("release_date") or desc.get("releaseDate", "")
        published_date = _parse_date(release_raw)

        # --- Keywords ---------------------------------------------------
        kw_list = item.get("keywords") or desc.get("keywords") or []
        keywords = ",".join(str(k) for k in kw_list) if kw_list else ""

        # --- Category ---------------------------------------------------
        category = uri_parts[0] if uri_parts else ""

        # --- Author / publisher -----------------------------------------
        contact = desc.get("contact") or {}
        author = contact.get("name", "") if isinstance(contact, dict) else ""

        # --- Metadata ---------------------------------------------------
        metadata = json.dumps({
            "cdid": item.get("cdid"),
            "dataset_id": item.get("dataset_id"),
            "edition": item.get("edition"),
            "type": item.get("type"),
            "topics": item.get("topics"),
            "national_statistic": desc.get("nationalStatistic"),
            "next_release": desc.get("nextRelease"),
            "contact_email": (
                contact.get("email", "") if isinstance(contact, dict) else ""
            ),
            "contact_telephone": (
                contact.get("telephone", "") if isinstance(contact, dict) else ""
            ),
            "unit": desc.get("unit"),
            "pre_unit": desc.get("preUnit"),
            "source": desc.get("source"),
            "uri": uri,
        })

        self._save_paper({
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": (item.get("title") or desc.get("title") or "").strip(),
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": author,
            "publisher": "Office for National Statistics",
            "url": item_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "original_filename": original_filename,
            "metadata": metadata,
        })
        return True

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl ONS search results and save items to DB.

        Paginates the ONS beta search API with offset-based pagination.
        Skips items whose abstract is shorter than _ABSTRACT_MIN chars.
        """
        saved = 0
        limit_or_inf = limit if limit is not None else float("inf")
        seen_urls: set[str] = set()
        start_time = time.time()
        offset = 0
        page = 0

        try:
            while saved < limit_or_inf:
                if page >= _SAFETY_CAP:
                    print(
                        f"[{_SITE_ID}] safety cap of {_SAFETY_CAP} pages reached, stopping"
                    )
                    break

                if time.time() - start_time > 25 * 60:
                    print(f"[{_SITE_ID}] 25-minute wall-clock budget exceeded, stopping")
                    break

                data = self._search_page(offset)
                if not data:
                    print(f"[{_SITE_ID}] no data at offset {offset}, stopping")
                    break

                items = data.get("items", [])
                if not items:
                    print(f"[{_SITE_ID}] empty items at offset {offset}, end of results")
                    break

                page += 1
                if page % 10 == 0:
                    print(
                        f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}"
                    )

                new_this_page = 0
                for item in items:
                    if saved >= limit_or_inf:
                        break

                    uri = item.get("uri", "")
                    item_url = f"{_BASE}{uri}"
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_this_page += 1

                    try:
                        did_save = self._process_item(item)
                        if did_save:
                            saved += 1
                            time.sleep(_RATE_SLEEP)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{_SITE_ID}] item {item_url} failed: {exc}")
                        continue

                if new_this_page == 0:
                    print(
                        f"[{_SITE_ID}] all items on page {page} already seen, stopping"
                    )
                    break

                offset += _PAGE_SIZE

        except KeyboardInterrupt:
            print(f"[{_SITE_ID}] interrupted by user after saving {saved} items")

        return saved
