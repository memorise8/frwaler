# -*- coding: utf-8 -*-
"""재정경제부 공공데이터개방 crawler.

Starting URL : https://mofe.go.kr/id/pdo.do?menuNo=3050000
Actual source: https://www.data.go.kr (공공데이터포털) — 재정경제부 datasets.
Total records: ~133 (129 FILE + 4 API as of 2026-05).

Strategy
--------
1. Walk paginated list pages on data.go.kr filtered by org=재정경제부, dType=FILE then API.
2. Parse dataset PKs from HTML with BeautifulSoup (html5lib fallback chain).
3. Fetch schema.org JSON catalog per PK — clean, no HTML parsing needed.
4. Save via BaseCrawler._save_paper().
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import quote

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402

_LIST_URL = "https://www.data.go.kr/tcs/dss/selectDataSetList.do"
_CATALOG_BASE = "https://www.data.go.kr/catalog"
_DETAIL_BASE = "https://www.data.go.kr/data"
_ORG = "재정경제부"
_REFERER = "https://mofe.go.kr/id/pdo.do?menuNo=3050000"
_PAGE_SIZE = 10
_DTYPES = ["FILE", "API"]


# ---------------------------------------------------------------------------
# HTML parser helper (module-level so it can be reused without self)
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Try html5lib → lxml → html.parser; raise if all fail."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No usable HTML parser found (install html5lib or lxml)")


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MofeGoKrIdCrawler(BaseCrawler):
    """Crawler for 재정경제부 공공데이터개방 (via data.go.kr)."""

    site_id = "mofe-go-kr-id"
    site_name = "Custom: mofe-go-kr-id"
    base_url = "https://mofe.go.kr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str) -> str | None:
        """GET via curl --tls-max 1.3 -sk with 3-attempt exponential backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {_REFERER}",
            url,
        ]
        _waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if result.returncode == 0 and raw:
                    return raw.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip() or f"exit {result.returncode}"
                if attempt < 2:
                    print(f"[mofe-go-kr-id] {err} (attempt {attempt+1}/3), retry in {_waits[attempt]}s…")
                    time.sleep(_waits[attempt])
                else:
                    print(f"[mofe-go-kr-id] curl failed after 3 attempts for {url[:80]}: {err}")
            except Exception as exc:
                if attempt < 2:
                    print(f"[mofe-go-kr-id] curl error: {exc} (attempt {attempt+1}/3), retry in {_waits[attempt]}s…")
                    time.sleep(_waits[attempt])
                else:
                    print(f"[mofe-go-kr-id] curl failed after 3 attempts for {url[:80]}: {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page helpers
    # ------------------------------------------------------------------

    def _list_page_url(self, page: int, dtype: str) -> str:
        return (
            f"{_LIST_URL}?org={quote(_ORG)}&orgFilter={quote(_ORG)}"
            f"&orgSearch=orgSearch&currentPage={page}&dType={dtype}"
            f"&size={_PAGE_SIZE}&sort=updtDt&sort_order=desc"
        )

    def _parse_pks(self, html: str, dtype: str) -> list[str]:
        """Return ordered, deduplicated dataset PKs from a list-page HTML."""
        suffix = "fileData" if dtype == "FILE" else "openapi"
        pattern = re.compile(rf'/data/(\d+)/{re.escape(suffix)}\.do')
        seen: set[str] = set()
        pks: list[str] = []

        try:
            soup = _make_soup(html)
            for a in soup.find_all("a", href=True):
                m = pattern.search(a["href"])
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    pks.append(m.group(1))
        except Exception:
            # regex fallback if BS4 fails completely
            for m in pattern.finditer(html):
                if m.group(1) not in seen:
                    seen.add(m.group(1))
                    pks.append(m.group(1))

        return pks

    # ------------------------------------------------------------------
    # Catalog JSON helper
    # ------------------------------------------------------------------

    def _fetch_catalog(self, pk: str, dtype: str) -> dict | None:
        """Fetch schema.org JSON catalog for one dataset.  Returns dict or None."""
        suffix = "fileData" if dtype == "FILE" else "openapi"
        url = f"{_CATALOG_BASE}/{pk}/{suffix}.json"
        raw = self._curl(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[mofe-go-kr-id] catalog JSON parse error (PK {pk}): {exc}")
            return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 재정경제부 public datasets from data.go.kr.

        Walks dType=FILE then dType=API.  Uses seen_pks for URL dedup.
        Stops early when saved >= limit, safety cap (200 pages/type),
        or 25-minute wall-clock budget reached.
        """
        start_ts = time.time()
        budget_secs = 25 * 60
        saved = 0
        seen_pks: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"

        for dtype in _DTYPES:
            if limit is not None and saved >= limit:
                break

            page = 1
            safety_cap = 200
            consecutive_empty = 0

            while True:
                # --- termination guards ---
                if limit is not None and saved >= limit:
                    break
                if page > safety_cap:
                    print(f"[mofe-go-kr-id] Safety cap {safety_cap} pages reached (dType={dtype}). Stopping type.")
                    break
                if time.time() - start_ts > budget_secs:
                    print(f"[mofe-go-kr-id] 25-min budget reached. Stopping.")
                    return saved

                # --- fetch list page ---
                page_url = self._list_page_url(page, dtype)
                html = self._curl(page_url)
                if not html:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        print(f"[mofe-go-kr-id] 3 consecutive list-page failures (dType={dtype}, p={page}). Stopping type.")
                        break
                    page += 1
                    continue
                consecutive_empty = 0

                pks = self._parse_pks(html, dtype)
                new_pks = [pk for pk in pks if pk not in seen_pks]

                if not new_pks:
                    print(f"[mofe-go-kr-id] No new items on page {page} (dType={dtype}). End of type.")
                    break

                if page % 10 == 0:
                    print(f"[mofe-go-kr-id] page {page} (dType={dtype}): saved {saved}/{limit_label}")

                # --- per-item loop ---
                for pk in new_pks:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - start_ts > budget_secs:
                        print(f"[mofe-go-kr-id] 25-min budget reached mid-page. Stopping.")
                        return saved

                    seen_pks.add(pk)

                    try:
                        time.sleep(self._delay)

                        meta = self._fetch_catalog(pk, dtype)
                        if not meta:
                            print(f"[mofe-go-kr-id] PK {pk}: no catalog JSON, skipping")
                            continue

                        title = (meta.get("name") or "").strip()
                        if not title:
                            print(f"[mofe-go-kr-id] PK {pk}: empty title, skipping")
                            continue

                        abstract = (meta.get("description") or "").strip()
                        if len(abstract) < 50:
                            print(f"[mofe-go-kr-id] PK {pk}: abstract too short ({len(abstract)} chars), skipping")
                            continue

                        # --- dates ---
                        published_date = (
                            meta.get("datePublished") or meta.get("dateCreated") or ""
                        ).strip()
                        listed_date = (meta.get("dateModified") or published_date).strip()

                        # --- provenance ---
                        creator = meta.get("creator") or {}
                        publisher = (creator.get("name") or _ORG).strip()
                        contact = creator.get("contactPoint") or {}
                        department = (contact.get("contactType") or "").strip()

                        # --- detail URL ---
                        detail_url = (
                            meta.get("url")
                            or f"{_DETAIL_BASE}/{pk}/{'fileData' if dtype == 'FILE' else 'openapi'}.do"
                        )

                        keywords = (meta.get("keywords") or "").strip()
                        category = (meta.get("additionalType") or "").strip()

                        paper = {
                            "site_id": self.site_id,
                            "external_id": pk,
                            "post_number": pk,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "url": detail_url,
                            "pdf_url": None,
                            "authors": "",
                            "publisher": publisher,
                            "department": department,
                            "journal": "",
                            "keywords": keywords,
                            "category": category,
                            "doi": "",
                            "original_filename": None,
                            "metadata": json.dumps({
                                "posted_date": listed_date,
                                "publicDataPk": pk,
                                "dType": dtype,
                                "category": category,
                                "encodingFormat": meta.get("encodingFormat") or "",
                                "datasetTimeInterval": meta.get("datasetTimeInterval") or "",
                                "license": meta.get("license") or "",
                                "alternateName": meta.get("alternateName") or "",
                                "legislation": meta.get("legislation") or "",
                                "spatialCoverage": meta.get("spatialCoverage") or "",
                                "temporalCoverage": meta.get("temporalCoverage") or "",
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[mofe-go-kr-id] Saved {saved}/{limit_label}: {title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[mofe-go-kr-id] item PK {pk} failed: {exc}")
                        continue

                page += 1

        print(f"[mofe-go-kr-id] Done. Total saved: {saved}")
        return saved
