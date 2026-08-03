# -*- coding: utf-8 -*-
"""Geoscience Australia Legacy Publications Records crawler.

Starting URL: https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1990s

Discovers all decade pages (1940s-2020s) from the parent /records index page,
collects eCatIds from each decade's HTML listing, then batch-queries the
GeoNetwork Elasticsearch API for rich metadata. No per-record detail page
fetches needed — all metadata is embedded in the ES index.

Crawl order: newest decade first (2020s, 2010s, ...) so records with real
abstracts are found quickly even at small limits.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_LIST_ENTRY = "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1990s"
_RECORDS_INDEX = "https://www.ga.gov.au/data-pubs/library/legacy-publications/records"
_ES_API = "https://ecat.ga.gov.au/geonetwork/srv/api/search/records/_search"
_DETAIL_TMPL = "https://www.ga.gov.au/metadata-gateway/metadata/record/{ecat_id}/"

MIN_ABSTRACT_CHARS = 50
BATCH_SIZE = 100
MAX_PAGES = 200
MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
BACKOFF = (1, 3, 9)


class GaGovAuDataPubsCrawler(BaseCrawler):
    site_id = "ga-gov-au-data-pubs"
    site_name = "Custom: ga-gov-au-data-pubs"
    base_url = "https://www.ga.gov.au"

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        page_count = 0
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "inf"

        decade_urls = self._get_decade_urls()
        print(f"[{self.site_id}] discovered {len(decade_urls)} decade pages")
        for u in decade_urls:
            print(f"[{self.site_id}]   {u}")

        for decade_url in decade_urls:
            if limit is not None and saved >= limit:
                break
            if page_count >= MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached; stopping")
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping")
                break

            print(f"[{self.site_id}] fetching decade page: {decade_url}")
            try:
                ecat_ids = self._get_ecat_ids(decade_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] failed to scrape {decade_url}: {exc}")
                continue

            if not ecat_ids:
                print(f"[{self.site_id}] no record IDs found on {decade_url}")
                continue

            print(f"[{self.site_id}] found {len(ecat_ids)} record IDs on {decade_url}")

            # Process in batches of BATCH_SIZE via ES API
            for batch_start in range(0, len(ecat_ids), BATCH_SIZE):
                if limit is not None and saved >= limit:
                    break
                if page_count >= MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached; stopping")
                    break
                if time.time() - start_time > MAX_SECONDS:
                    print(f"[{self.site_id}] 25-minute budget exceeded; stopping")
                    break

                page_count += 1
                batch_ids = ecat_ids[batch_start:batch_start + BATCH_SIZE]

                try:
                    hits = self._fetch_batch(batch_ids)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] ES batch fetch error: {exc}")
                    continue

                if hits is None:
                    print(f"[{self.site_id}] batch {page_count}: fetch failed; skipping")
                    continue
                if not hits:
                    print(f"[{self.site_id}] batch {page_count}: empty ES response")
                    continue

                for hit in hits:
                    if limit is not None and saved >= limit:
                        break
                    try:
                        src = hit.get("_source", {}) if isinstance(hit, dict) else {}
                        ecat_id = str(src.get("eCatId") or "").strip()
                        if not ecat_id:
                            continue

                        detail_url = _DETAIL_TMPL.format(ecat_id=ecat_id)
                        if detail_url in seen_urls:
                            continue
                        seen_urls.add(detail_url)

                        parsed = self._parse_hit(src)
                        if parsed is None:
                            continue

                        abstract = parsed["abstract"]
                        if len(abstract) < MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] eCatId={ecat_id} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        paper = {
                            "site_id": self.site_id,
                            "external_id": parsed["external_id"],
                            "post_number": parsed["post_number"],
                            "title": parsed["title"],
                            "abstract": abstract,
                            "published_date": parsed["published_date"],
                            "posted_date": parsed["posted_date"],
                            "authors": parsed["authors"],
                            "publisher": parsed["publisher"],
                            "url": detail_url,
                            "pdf_url": parsed["pdf_url"],
                            "keywords": parsed["keywords"],
                            "category": parsed["category"],
                            "original_filename": parsed["original_filename"],
                            "metadata": json.dumps(
                                parsed["metadata"], ensure_ascii=False
                            ),
                        }
                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_display}: "
                            f"{parsed['title'][:70]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        src_id = (
                            (hit.get("_source") or {}).get("eCatId", "?")
                            if isinstance(hit, dict)
                            else "?"
                        )
                        print(f"[{self.site_id}] item eCatId={src_id} failed: {exc}")
                        continue

                if page_count % 10 == 0:
                    print(
                        f"[{self.site_id}] page {page_count}: "
                        f"saved {saved}/{limit_display}"
                    )

                # Polite pause between ES batch requests
                time.sleep(0.5)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _get_decade_urls(self) -> list[str]:
        """Return decade page URLs sorted newest-first."""
        raw = self._curl_get(_RECORDS_INDEX, context="records index")
        urls: list[str] = []
        seen: set[str] = set()

        if raw:
            pattern = (
                r'href="(https://www\.ga\.gov\.au'
                r"/data-pubs/library/legacy-publications/records/[^\"#?]+)\""
            )
            for m in re.finditer(pattern, raw):
                url = m.group(1).rstrip("/")
                if url == _RECORDS_INDEX.rstrip("/"):
                    continue
                if url not in seen:
                    seen.add(url)
                    urls.append(url)

        # Always include the starting URL
        start = _LIST_ENTRY.rstrip("/")
        if start not in seen:
            urls.append(start)

        # Reverse-alphabetical sort → newest decade first.
        # "digitised-records-2020s" ('d' > '2') sorts before "2010s" etc.
        urls.sort(reverse=True)
        return urls

    def _get_ecat_ids(self, decade_url: str) -> list[str]:
        """Collect eCatIds from a decade listing page (deduplicated, ordered)."""
        raw = self._curl_get(decade_url, context=decade_url)
        if not raw:
            return []
        ids = re.findall(r"/metadata-gateway/metadata/record/(\d+)/", raw)
        seen: set[str] = set()
        result: list[str] = []
        for id_ in ids:
            if id_ not in seen:
                seen.add(id_)
                result.append(id_)
        return result

    # ------------------------------------------------------------------
    # ES batch fetch
    # ------------------------------------------------------------------

    def _fetch_batch(self, ecat_ids: list[str]):
        """Query GeoNetwork ES for a batch of eCatIds.

        Returns list of hit dicts, or None on network failure.
        """
        body = {
            "query": {"terms": {"eCatId": ecat_ids}},
            "size": len(ecat_ids),
            "_source": [
                "eCatId",
                "resourceTitleObject",
                "resourceAbstractObject",
                "author",
                "publisher",
                "publicationDateForRecord",
                "recordCreationDate",
                "resourceDate",
                "link",
                "keywords",
                "seriesName",
                "issueIdentification",
                "uuid",
                "metadataIdentifier",
                "legalconstraints",
            ],
        }
        raw = self._curl_post(_ES_API, body, context=f"batch {len(ecat_ids)} ids")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON parse error in ES response: {exc}")
            return None
        if not isinstance(data, dict):
            return None
        return (data.get("hits") or {}).get("hits") or []

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        """GET url via curl with retries. Returns decoded string or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", "30", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            url,
        ]
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=40)
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"
            except Exception as exc:
                last_error = str(exc)
            print(
                f"[{self.site_id}] {context} GET attempt {attempt + 1}/3 failed: "
                f"{last_error}"
            )
            if attempt < 2:
                time.sleep(BACKOFF[attempt])
        print(f"[{self.site_id}] {context} GET failed after 3 attempts")
        return None

    def _curl_post(self, url: str, body: dict, context: str = "request") -> str | None:
        """POST JSON body to url via curl with retries. Returns decoded string or None."""
        body_json = json.dumps(body)
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json",
            "-H", "Content-Type: application/json",
            "-d", body_json,
            url,
        ]
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"
            except Exception as exc:
                last_error = str(exc)
            print(
                f"[{self.site_id}] {context} attempt {attempt + 1}/3 failed: "
                f"{last_error}"
            )
            if attempt < 2:
                time.sleep(BACKOFF[attempt])
        print(f"[{self.site_id}] {context} failed after 3 attempts")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_hit(self, src: dict) -> dict | None:
        """Parse an ES source dict into a paper field dict.

        Returns None if title is missing (record not usable).
        """
        title_obj = src.get("resourceTitleObject") or {}
        title = ""
        if isinstance(title_obj, dict):
            title = title_obj.get("default", "") or title_obj.get("langeng", "")
        title = self._clean_text(title)
        if not title:
            return None

        abs_obj = src.get("resourceAbstractObject") or {}
        abstract_raw = ""
        if isinstance(abs_obj, dict):
            abstract_raw = abs_obj.get("default", "") or abs_obj.get("langeng", "")
        abstract = self._clean_text(abstract_raw)

        ecat_id = str(src.get("eCatId") or "").strip()

        # Author(s) — can be a string or a list
        author_raw = src.get("author", "")
        if isinstance(author_raw, list):
            authors = "; ".join(str(a).strip() for a in author_raw if a)
        elif author_raw:
            authors = str(author_raw).strip()
        else:
            authors = ""

        publisher = str(src.get("publisher") or "").strip()

        published_date = self._extract_date(src)
        creation_raw = str(src.get("recordCreationDate") or "")
        posted_date = self._normalize_date(creation_raw)

        pdf_url = self._extract_pdf_url(src)

        # Original filename from PDF URL last path segment
        original_filename = None
        if pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if "." in tail and len(tail) <= 200:
                original_filename = tail

        # Keywords
        kws_raw = src.get("keywords") or []
        kw_texts: list[str] = []
        seen_kw: set[str] = set()
        for kw in (kws_raw if isinstance(kws_raw, list) else []):
            if isinstance(kw, dict):
                text = str(kw.get("keyword") or "").strip()
            elif kw:
                text = str(kw).strip()
            else:
                text = ""
            if text and text not in seen_kw:
                seen_kw.add(text)
                kw_texts.append(text)
        keywords_str = ", ".join(kw_texts) if kw_texts else ""

        series_name = str(src.get("seriesName") or "").strip()
        issue_id = str(src.get("issueIdentification") or "").strip()
        uuid = str(src.get("uuid") or src.get("metadataIdentifier") or "").strip()

        metadata = {
            "posted_date": creation_raw,          # raw form; adapter reads this key
            "originalFilename": original_filename,
            "issueIdentification": issue_id,
            "series": series_name,
            "seriesName": series_name,
            "uuid": uuid,
            "legalconstraints": str(src.get("legalconstraints") or ""),
        }

        return {
            "external_id": ecat_id,
            "post_number": ecat_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date or None,
            "posted_date": posted_date or None,
            "authors": authors or None,
            "publisher": publisher or None,
            "pdf_url": pdf_url or None,
            "keywords": keywords_str or None,
            "category": series_name or None,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _extract_date(self, src: dict) -> str:
        """Extract best publication date (ISO YYYY-MM-DD) from ES source."""
        dates = src.get("resourceDate") or []
        pub_date = ""
        create_date = ""
        if isinstance(dates, list):
            for entry in dates:
                if not isinstance(entry, dict):
                    continue
                dtype = str(entry.get("type", "")).lower()
                normalized = self._normalize_date(str(entry.get("date", "")))
                if not normalized:
                    continue
                if dtype == "publication" and not pub_date:
                    pub_date = normalized
                elif dtype == "creation" and not create_date:
                    create_date = normalized
        if pub_date:
            return pub_date
        if create_date:
            return create_date
        return self._normalize_date(str(src.get("publicationDateForRecord") or ""))

    def _extract_pdf_url(self, src: dict) -> str:
        """Extract first PDF URL from the link array."""
        links = src.get("link") or []
        if not isinstance(links, list):
            return ""
        for link in links:
            if not isinstance(link, dict):
                continue
            url_obj = link.get("urlObject") or {}
            if isinstance(url_obj, dict):
                url = url_obj.get("default", "") or url_obj.get("langeng", "")
            else:
                url = str(url_obj)
            url = str(url).strip()
            if url.lower().endswith(".pdf") or ".pdf?" in url.lower():
                return url
        return ""

    @staticmethod
    def _normalize_date(value: str) -> str:
        if not value:
            return ""
        m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", str(value))
        if not m:
            return ""
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    @staticmethod
    def _clean_text(value) -> str:
        """Strip HTML tags, unescape entities, collapse whitespace."""
        if value is None:
            return ""
        text = str(value)
        text = html.unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#>]+", " ", text)
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
