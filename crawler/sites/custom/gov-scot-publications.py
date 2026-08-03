# -*- coding: utf-8 -*-
"""Scottish Government publications crawler.

Starting URL: https://www.gov.scot/publications/
Pagination:   ?page=N  (10 items per page, ~49 000+ total)
Detail pages: HTML with JSON-LD + structured metadata
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import unquote, urljoin

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.gov.scot"
_LIST_URL = "https://www.gov.scot/publications/"
_SITE_ID = "gov-scot-publications"
_PAGE_SIZE = 10
_ABSTRACT_MIN = 100   # skip items whose abstract is below this threshold
_RATE_SLEEP = 1.0
_SAFETY_CAP = 200     # max list pages before forced stop
_MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute total wall-clock budget (seconds)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Low-level HTTP helper
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch *url* via curl with TLS-max 1.3; exponential backoff on failure."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", str(timeout),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", f"User-Agent: {_USER_AGENT}",
        url,
    ]
    for attempt in range(retries):
        if attempt > 0:
            wait = 1 * (3 ** (attempt - 1))   # 1 s → 3 s → 9 s
            time.sleep(wait)
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout + 5
            )
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


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Build a BeautifulSoup from *html*, trying multiple parsers."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str:
    """Normalise various date strings to YYYY-MM-DD; return '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    # ISO: 2026-05-13T15:58:00+01:00
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    # "13 May 2026" / "13 May 26"
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class GovScotPublicationsCrawler(BaseCrawler):
    """Crawler for https://www.gov.scot/publications/."""

    site_id = _SITE_ID
    site_name = "Custom: gov-scot-publications"
    base_url = _BASE

    # ------------------------------------------------------------------
    # Private: list-page parser
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict]:
        """Return a list of stub dicts from the publications index page."""
        url = f"{_LIST_URL}?page={page}"
        raw = _curl_get(url)
        if not raw:
            return []

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] soup error on list page {page}: {exc}")
            return []
        if not soup:
            return []

        stubs = []
        for li in soup.select("li.ds_search-result"):
            try:
                a_tag = li.select_one("a.ds_search-result__link")
                if not a_tag:
                    continue
                href = a_tag.get("href", "").strip()
                if not href:
                    continue
                title = a_tag.get_text(strip=True)
                full_url = urljoin(_BASE, href)

                summary_tag = li.select_one("p.ds_search-result__summary")
                summary = summary_tag.get_text(strip=True) if summary_tag else ""

                category = ""
                listed_date = ""
                for meta_item in li.select("div.ds_metadata__item"):
                    k_tag = meta_item.select_one("dt.ds_metadata__key")
                    v_tag = meta_item.select_one("dd.ds_metadata__value")
                    if not k_tag or not v_tag:
                        continue
                    key = k_tag.get_text(strip=True).lower()
                    val = v_tag.get_text(strip=True)
                    if key == "format":
                        category = val
                    elif key == "date":
                        listed_date = _parse_date(val)

                # slug = last non-empty path segment → external_id / post_number
                slug = [s for s in href.split("/") if s][-1] if href else ""

                stubs.append({
                    "url": full_url,
                    "title": title,
                    "summary": summary,
                    "category": category,
                    "listed_date": listed_date,
                    "slug": slug,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list-item parse error: {exc}")
                continue

        return stubs

    # ------------------------------------------------------------------
    # Private: detail-page parser
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict:
        """Fetch and parse a publication detail page; return a metadata dict."""
        raw = _curl_get(url)
        if not raw:
            return {}

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] soup error on detail {url}: {exc}")
            return {}
        if not soup:
            return {}

        result: dict = {}

        # JSON-LD — most reliable source for dates and full description
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "Article":
                    result["date_published"] = _parse_date(
                        data.get("datePublished", "")
                    )
                    result["date_modified"] = _parse_date(
                        data.get("dateModified", "")
                    )
                    result["ld_description"] = data.get("description", "")
                    break
            except Exception:
                continue

        # Structured metadata block in the page header
        pub_type_tag = soup.select_one("#sg-meta__publication-type")
        result["pub_type"] = (
            pub_type_tag.get_text(strip=True) if pub_type_tag else ""
        )

        for meta_item in soup.select(
            "dl.ds_page-header__metadata div.ds_metadata__item"
        ):
            k_tag = meta_item.select_one("dt.ds_metadata__key")
            v_tag = meta_item.select_one("dd.ds_metadata__value")
            if not k_tag or not v_tag:
                continue
            key = k_tag.get_text(strip=True).lower()
            val = v_tag.get_text(strip=True)
            if key == "published":
                result["published_date"] = _parse_date(val)
            elif key in ("last updated", "last updated date"):
                result["last_updated"] = _parse_date(val)
            elif key == "directorate":
                result["directorate"] = val
            elif key == "topic":
                result["topic"] = val
            elif key in ("subject", "subjects"):
                result["subject"] = val
            elif key == "date of meeting":
                result["meeting_date"] = _parse_date(val)

        # Abstract: JSON-LD description + leader intro + body text
        ld_desc = result.get("ld_description", "")

        leader_tag = soup.select_one(".ds_leader")
        leader_text = (
            leader_tag.get_text(separator=" ", strip=True) if leader_tag else ""
        )

        body_tag = soup.select_one(".body-content.publication-body")
        body_text = (
            body_tag.get_text(separator=" ", strip=True) if body_tag else ""
        )

        abstract_parts: list[str] = []
        if ld_desc:
            abstract_parts.append(ld_desc)
        if leader_text and leader_text not in abstract_parts:
            abstract_parts.append(leader_text)
        if body_text and len(body_text) > 50:
            # Truncate very long bodies to keep things reasonable
            body_snippet = body_text[:4000]
            if body_snippet not in abstract_parts:
                abstract_parts.append(body_snippet)

        result["abstract"] = "\n\n".join(abstract_parts)

        # PDF URL — first PDF attachment found
        result["pdf_url"] = None
        result["original_filename"] = None
        for a_tag in soup.select(
            "a.ds_file-download__title, a.ds_file-download__thumbnail-link"
        ):
            href = (a_tag.get("href") or "").strip()
            if not href:
                continue
            if re.search(r"\.pdf", href, re.I) or "govscot%3Adocument" in href:
                if href.startswith("/"):
                    href = _BASE + href
                if re.search(r"\.pdf", href, re.I):
                    result["pdf_url"] = href
                    # Original filename from URL: ...govscot%3Adocument/Name.pdf
                    fname_m = re.search(
                        r"govscot%3Adocument/(.+?)(?:\?|$)", href
                    )
                    if fname_m:
                        result["original_filename"] = unquote(
                            fname_m.group(1)
                        ).replace("+", " ")
                    else:
                        result["original_filename"] = href.rsplit("/", 1)[-1]
                    break

        # Collection membership
        coll_tag = soup.select_one("a.sg-meta__collection")
        result["collection"] = (
            coll_tag.get_text(strip=True) if coll_tag else ""
        )

        return result

    # ------------------------------------------------------------------
    # Public: main crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl gov.scot publications, save to DB, return saved count."""
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        page = 1
        while True:
            # --- stop conditions ---
            if limit is not None and saved >= limit:
                break

            if page > _SAFETY_CAP:
                print(
                    f"[{_SITE_ID}] safety cap of {_SAFETY_CAP} pages reached. Stopping."
                )
                break

            elapsed = time.time() - crawl_start
            if elapsed > _MAX_WALL:
                print(
                    f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping."
                )
                break

            if page % 10 == 0:
                print(
                    f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}"
                )

            # --- fetch list page ---
            stubs = self._fetch_list_page(page)
            if not stubs:
                print(f"[{_SITE_ID}] no items on page {page}. Done.")
                break

            new_on_page = 0
            for stub in stubs:
                if limit is not None and saved >= limit:
                    break

                url = stub["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                # --- fetch + parse detail ---
                try:
                    time.sleep(_RATE_SLEEP)
                    detail = self._fetch_detail(url)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({url}): {exc}")
                    continue

                # --- build abstract ---
                abstract = detail.get("abstract", "")
                if not abstract:
                    abstract = stub.get("summary", "")

                if len(abstract) < _ABSTRACT_MIN:
                    print(
                        f"[{_SITE_ID}] abstract too short "
                        f"({len(abstract)} chars), skipping: "
                        f"{stub['title'][:60]}"
                    )
                    continue

                # --- resolve dates ---
                published_date = (
                    detail.get("published_date")
                    or detail.get("date_published")
                    or detail.get("last_updated")
                    or ""
                )
                listed_date = stub.get("listed_date", "")

                # --- category / department ---
                category = (
                    detail.get("topic")
                    or detail.get("pub_type")
                    or stub.get("category", "")
                )
                department = detail.get("directorate", "")

                slug = stub["slug"]

                # --- metadata blob ---
                meta: dict = {}
                for k, v in {
                    "posted_date": stub.get("listed_date", ""),
                    "pub_type": detail.get("pub_type", ""),
                    "last_updated": detail.get("last_updated", ""),
                    "meeting_date": detail.get("meeting_date", ""),
                    "collection": detail.get("collection", ""),
                    "subject": detail.get("subject", ""),
                    "date_modified": detail.get("date_modified", ""),
                    "list_category": stub.get("category", ""),
                    "originalFilename": detail.get("original_filename", ""),
                }.items():
                    if v:
                        meta[k] = v

                paper = {
                    "id": None,
                    "site_id": _SITE_ID,
                    "external_id": slug,
                    "post_number": slug,
                    "title": stub["title"],
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": listed_date,
                    "url": url,
                    "pdf_url": detail.get("pdf_url") or "",
                    "original_filename": detail.get("original_filename") or "",
                    "authors": "",
                    "publisher": "Scottish Government",
                    "department": department,
                    "category": category,
                    "keywords": "",
                    "doi": "",
                    "journal": "",
                    "metadata": json.dumps(meta, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{_SITE_ID}] saved {saved}/{limit_str}: "
                        f"{stub['title'][:60]}"
                    )
                except Exception as exc:
                    print(f"[{_SITE_ID}] save error for {url}: {exc}")
                    continue

            # end-of-pagination: all items on this page were already seen
            if new_on_page == 0:
                print(
                    f"[{_SITE_ID}] all items on page {page} already seen. Done."
                )
                break

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
