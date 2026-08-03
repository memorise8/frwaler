# -*- coding: utf-8 -*-
"""NIST Digital Archives CONTENTdm crawler.

Target : https://nistdigitalarchives.contentdm.oclc.org/digital/collection/p16009coll6/custom/jresbyvol
API    : CONTENTdm dmwebservices  (dmQuery list + dmGetItemInfo detail)
PDF    : /utils/getfile/collection/p16009coll6/id/{ptr}/filename/{ptr}.pdf
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_COLLECTION = "p16009coll6"
_WS_BASE = "https://nistdigitalarchives.contentdm.oclc.org/digital/bl/dmwebservices/index.php"
_FIELDS = "title!descri!subjec!creato!date!source!doi!publis!volume!number!issn"
_PAGE_SIZE = 50
_MIN_ABSTRACT = 100  # chars — satisfies both "skip <50" rule and test's ">=100" assertion


class NISTDigitalArchivesCrawler(BaseCrawler):
    """Crawler for NIST Digital Archives – Journal of Research of NIST collection."""

    site_id = "nistdigitalarchives-contentdm-oclc-org-digital"
    site_name = "Custom: nistdigitalarchives-contentdm-oclc-org-digital"
    base_url = "https://nistdigitalarchives.contentdm.oclc.org"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, timeout=30):
        """GET via curl with TLS quirk workaround and 3-attempt exponential back-off."""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk",
                        "--max-time", str(timeout),
                        "-A", self.USER_AGENT,
                        url,
                    ],
                    capture_output=True,
                    timeout=timeout + 5,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt + 1}: {exc}")

            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] retrying in {wait}s…")
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    def _dmquery(self, start, page_size=_PAGE_SIZE):
        """Fetch one page from the dmQuery list API. Returns parsed dict or None."""
        url = (
            f"{_WS_BASE}?q=dmQuery/{_COLLECTION}/0/{_FIELDS}"
            f"/date/{page_size}/{start}/1/0/0/0/0/json"
        )
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] JSON parse error at start={start}: {exc}")
            return None

    def _get_item_detail(self, pointer):
        """Fetch full item metadata from dmGetItemInfo. Returns dict or None."""
        url = f"{_WS_BASE}?q=dmGetItemInfo/{_COLLECTION}/{pointer}/json"
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            # CONTENTdm returns the item dict directly; an error has a "code" key.
            if isinstance(data, dict) and "code" not in data:
                return data
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Field coercers
    # ------------------------------------------------------------------

    @staticmethod
    def _s(val):
        """Coerce any CONTENTdm field value to str (empty dict/list → '')."""
        if val is None or val == {} or val == []:
            return ""
        return str(val).strip()

    @staticmethod
    def _parse_authors(creato):
        """'Smith, J.; Jones, B.' → JSON array string."""
        if not creato:
            return json.dumps([])
        parts = [p.strip() for p in creato.split(";") if p.strip()]
        return json.dumps(parts or [creato.strip()], ensure_ascii=False)

    @staticmethod
    def _parse_keywords(subjec):
        """'a; b; c' or 'a, b, c' → JSON array string."""
        if not subjec:
            return json.dumps([])
        sep = ";" if ";" in subjec else ","
        kws = [k.strip() for k in subjec.split(sep) if k.strip()]
        return json.dumps(kws, ensure_ascii=False)

    @staticmethod
    def _parse_date(raw):
        """'1995-05' / '1995' / '1995-05-01' → 'YYYY-MM-DD' or ''."""
        if not raw:
            return ""
        m = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", raw.strip())
        if not m:
            return ""
        y, mo, d = m.groups()
        return f"{y}-{mo or '01'}-{d or '01'}"

    @staticmethod
    def _clean_doi(raw):
        """Strip URL prefix; return bare DOI string."""
        if not raw:
            return ""
        return re.sub(r"^https?://(dx\.)?doi\.org/", "", raw).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the NIST Journal of Research collection.

        Strategy: CONTENTdm dmQuery API sorts by date ascending (oldest first).
        Early papers (1904-~1945) have no abstracts; recent papers do.
        We start from the *last page* (most recent) and work backwards so that
        papers with abstracts are encountered immediately and the test (limit=3)
        returns results without iterating through thousands of abstract-less rows.
        """
        seen_urls: set[str] = set()
        saved = 0
        page_num = 0
        MAX_PAGES = 200
        MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        t0 = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        print(f"[{self.site_id}] Starting crawl (limit={limit_label})")

        # Phase 0: get total record count so we can start from the newest end.
        probe = self._dmquery(1, page_size=1)
        total = 8257  # fallback
        if probe:
            try:
                total = int(probe.get("pager", {}).get("total", total))
            except (TypeError, ValueError):
                pass
        print(f"[{self.site_id}] Total records in collection: {total}")

        # Start from the last page and decrement towards 1.
        start = max(1, total - _PAGE_SIZE + 1)

        while True:
            # ---- termination guards ----
            if limit is not None and saved >= limit:
                break
            if page_num >= MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - t0 > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget ({MAX_WALL_SECONDS}s) exceeded. Stopping.")
                break
            if start < 1:
                print(f"[{self.site_id}] Exhausted all pages. Done.")
                break

            # ---- fetch page ----
            try:
                data = self._dmquery(start)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] Page fetch error at start={start}: {exc}")
                break

            if data is None:
                print(f"[{self.site_id}] No data at start={start}. Stopping.")
                break

            records = data.get("records", [])
            if not records:
                print(f"[{self.site_id}] No more records at start={start}. Done.")
                break

            if page_num % 10 == 0 and page_num > 0:
                print(
                    f"[{self.site_id}] page {page_num}: saved {saved}/{limit_label} "
                    f"(start={start})"
                )

            # Within the page, reverse so we process newest-first.
            for rec in reversed(records):
                if limit is not None and saved >= limit:
                    break

                try:
                    pointer = rec.get("pointer")
                    if not pointer:
                        continue

                    item_url = (
                        f"{self.base_url}/digital/collection/{_COLLECTION}/id/{pointer}"
                    )
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)

                    # ---- extract list-level fields ----
                    title    = self._s(rec.get("title"))
                    abstract = self._s(rec.get("descri"))
                    subjec   = self._s(rec.get("subjec"))
                    creato   = self._s(rec.get("creato"))
                    date_raw = self._s(rec.get("date"))
                    source   = self._s(rec.get("source"))
                    doi_raw  = self._s(rec.get("doi"))
                    publisher= self._s(rec.get("publis"))
                    volume   = self._s(rec.get("volume"))
                    number   = self._s(rec.get("number"))
                    issn     = self._s(rec.get("issn"))

                    # ---- augment from detail API if abstract is short ----
                    if len(abstract) < _MIN_ABSTRACT:
                        time.sleep(self._delay)
                        detail = self._get_item_detail(pointer)
                        if detail:
                            abstract = self._s(detail.get("descri")) or abstract
                            if not creato:
                                creato = self._s(detail.get("creato"))
                            if not date_raw:
                                date_raw = self._s(detail.get("date"))
                            if not doi_raw:
                                doi_raw = self._s(detail.get("doi"))
                            if not subjec:
                                subjec = self._s(detail.get("subjec"))

                    # ---- skip items whose abstract is still too short ----
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Skipping {pointer}: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    pdf_url = (
                        f"{self.base_url}/utils/getfile/collection/{_COLLECTION}"
                        f"/id/{pointer}/filename/{pointer}.pdf"
                    )

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": str(pointer),
                        "title": title or "(untitled)",
                        "authors": self._parse_authors(creato),
                        "abstract": abstract,
                        "category": source,
                        "keywords": self._parse_keywords(subjec),
                        "published_date": self._parse_date(date_raw),
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "doi": self._clean_doi(doi_raw),
                        "department": publisher,
                        "metadata": json.dumps(
                            {
                                "volume": volume,
                                "number": number,
                                "issn": issn,
                                "source": source,
                                "publisher": publisher,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {title[:60]}")

                    # Rate-limit between items (only when we did NOT already sleep
                    # for the detail fetch above).
                    if len(self._s(rec.get("descri"))) >= _MIN_ABSTRACT:
                        time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {rec.get('pointer', '?')} failed: {exc}")
                    continue

            start -= _PAGE_SIZE
            page_num += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
