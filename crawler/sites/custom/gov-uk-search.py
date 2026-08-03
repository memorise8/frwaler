# -*- coding: utf-8 -*-
"""GOV.UK Research and Statistics crawler.

List API:    https://www.gov.uk/search/research-and-statistics.json
Content API: https://www.gov.uk/api/content/<path>
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SEARCH_URL = "https://www.gov.uk/search/research-and-statistics.json"
_CONTENT_API = "https://www.gov.uk/api/content"
_BASE = "https://www.gov.uk"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, timeout: int = 30) -> str | None:
    """GET via curl with exponential-backoff retry (1s, 3s, 9s).
    Returns raw text or None after 3 failures.
    """
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: application/json, text/html, */*;q=0.8",
        "-H", "Accept-Language: en-GB,en;q=0.9",
        url,
    ]
    waits = [1, 3, 9]
    for attempt in range(3):
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout + 10
            )
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            print(f"[gov-uk-search] curl error (attempt {attempt + 1}/3): {exc}")
        if attempt < 2:
            time.sleep(waits[attempt])
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(dt_str: str | None) -> str:
    """Extract YYYY-MM-DD from ISO datetime string."""
    if not dt_str:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class GovUKSearchCrawler(BaseCrawler):
    """Crawler for GOV.UK Research and Statistics (research document type)."""

    site_id = "gov-uk-search"
    site_name = "Custom: gov-uk-search"
    base_url = "https://www.gov.uk"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[gov-uk-search] 25-minute wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 200:
                print(f"[gov-uk-search] Safety cap of 200 pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[gov-uk-search] page {page}: saved {saved}/{limit_str}")

            # --- Fetch list page ---
            list_url = (
                f"{_SEARCH_URL}"
                f"?content_store_document_type=research"
                f"&order=updated-newest"
                f"&page={page}"
            )
            raw = _curl_get(list_url)
            if not raw:
                print(f"[gov-uk-search] Failed to fetch list page {page}. Stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[gov-uk-search] Invalid JSON at list page {page}. Stopping.")
                break

            html = data.get("search_results", "") or ""
            if not html:
                print(f"[gov-uk-search] Empty search_results at page {page}. Done.")
                break

            # Extract unique top-level publication paths (no sub-pages)
            raw_links = re.findall(r'href="(/government/publications/[^/"]+)"', html)
            pub_paths = list(dict.fromkeys(raw_links))

            if not pub_paths:
                print(f"[gov-uk-search] No publication links at page {page}. Done.")
                break

            new_paths = [p for p in pub_paths if p not in seen_urls]
            if not new_paths:
                print(f"[gov-uk-search] No new items at page {page} (all duplicates). Done.")
                break

            # --- Process each publication ---
            for path in pub_paths:
                if limit is not None and saved >= limit:
                    break
                if path in seen_urls:
                    continue
                seen_urls.add(path)

                try:
                    ok = self._crawl_item(path)
                    if ok:
                        saved += 1
                        print(
                            f"[gov-uk-search] saved {saved}/{limit_str} "
                            f"← {path}"
                        )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[gov-uk-search] item {path} failed: {exc}")
                    continue

                time.sleep(self._delay)

            # --- Pagination: detect end ---
            next_prev = data.get("next_and_prev_links", "") or ""
            if "govuk-pagination__next" not in next_prev:
                print(f"[gov-uk-search] No next page after page {page}. Done.")
                break

            page += 1

        print(f"[gov-uk-search] Done. Total saved: {saved}")
        return saved

    def _crawl_item(self, path: str) -> bool:
        """Fetch Content API for one publication and save it.

        Returns True if the item was saved, False if skipped.
        """
        api_url = f"{_CONTENT_API}{path}"
        raw = _curl_get(api_url)
        if not raw:
            print(f"[gov-uk-search] Detail fetch failed for {path}")
            return False

        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[gov-uk-search] Detail JSON parse failed for {path}")
            return False

        title = (doc.get("title") or "").strip()
        if not title:
            print(f"[gov-uk-search] No title for {path}, skipping")
            return False

        content_id = doc.get("content_id", "") or ""
        base_path = doc.get("base_path") or path
        description = (doc.get("description") or "").strip()
        details = doc.get("details") or {}

        # Abstract: use description if long enough, else strip body HTML
        body_html = details.get("body") or ""
        if len(description) >= 100:
            abstract = description
        elif body_html:
            stripped = _strip_html(body_html)
            abstract = stripped if len(stripped) > len(description) else description
        else:
            abstract = description

        if len(abstract) < 100:
            print(f"[gov-uk-search] Abstract <100 chars for {path}, skipping")
            return False

        # Dates
        first_pub = doc.get("first_published_at") or details.get("first_public_at") or ""
        updated = doc.get("public_updated_at") or ""
        published_date = _parse_date(first_pub)
        listed_date_raw = updated
        listed_date = _parse_date(updated)

        # Publisher / organisations
        links_data = doc.get("links") or {}
        orgs = links_data.get("organisations") or []
        publisher = "; ".join(
            o.get("title", "") for o in orgs if o.get("title")
        ) or None

        # Attachments — pick first file (PDF) attachment
        attachments = details.get("attachments") or []
        pdf_url = None
        original_filename = None
        for att in attachments:
            if att.get("attachment_type") == "file":
                att_url = att.get("url") or ""
                if att_url:
                    pdf_url = att_url
                    seg = att_url.rstrip("/").split("/")[-1].split("?")[0]
                    original_filename = seg if seg else None
                    break

        # Keywords from browse page tags
        tags = details.get("tags") or {}
        browse = tags.get("browse_pages") or []
        keywords_list = [t.get("title", "") for t in browse if t.get("title")]
        keywords = ", ".join(keywords_list) if keywords_list else None

        # Taxons
        taxons = links_data.get("taxons") or []
        taxon_titles = [t.get("title", "") for t in taxons if t.get("title")]

        # Category
        category = doc.get("document_type") or details.get("document_type_label") or None

        # Change history (keep up to 5)
        change_history = (details.get("change_history") or [])[:5]

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": content_id,
            "post_number": content_id,   # UUID is the native identifier; no sequential number
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": None,
            "publisher": publisher,
            "department": None,
            "journal": None,
            "url": f"{_BASE}{base_path}",
            "pdf_url": pdf_url,
            "doi": None,
            "keywords": keywords,
            "category": category,
            "original_filename": original_filename,
            "metadata": json.dumps({
                "posted_date": listed_date_raw,
                "originalFilename": original_filename,
                "content_id": content_id,
                "document_type": doc.get("document_type"),
                "schema_name": doc.get("schema_name"),
                "taxons": taxon_titles,
                "change_history": change_history,
                "attachment_count": len(attachments),
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
        return True
