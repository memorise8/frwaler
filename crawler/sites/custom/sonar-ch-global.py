# -*- coding: utf-8 -*-
"""SONAR.ch global theses crawler.

Collects theses/dissertations (COAR coar:c_7a1f) from the Swiss Open Access
Repository via its public Invenio REST API.

Starting URL reference:
  https://sonar.ch/global/search/documents?q=&page=1&size=10&sort=newest&document_type=coar:c_7a1f
"""

import json
import os
import sys
import time
import urllib.parse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler

_API_URL = "https://sonar.ch/api/documents/"
_DOC_TYPE = "coar:c_7a1f"
_PAGE_SIZE = 50
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_LEN = 100   # match test assertion
_RATE_SLEEP = 1.0          # seconds between page fetches


class SonarChGlobalCrawler(BaseCrawler):
    site_id = "sonar-ch-global"
    site_name = "Custom: sonar-ch-global"
    base_url = "https://sonar.ch"
    DELIVERY_ORDER = "newest_first"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _fetch_page(self, page: int, size: int) -> dict | None:
        """Fetch one result page from the Invenio API. Returns parsed JSON or None."""
        # Use document_type as a standalone param (not inside q=) — that's what
        # the Invenio API actually accepts for this filter.
        params = {
            "view": "global",
            "q": "",
            "page": page,
            "size": size,
            "sort": "newest",
            "document_type": _DOC_TYPE,
        }
        url = f"{_API_URL}?{urllib.parse.urlencode(params)}"
        for attempt in range(3):
            try:
                resp = self._session.get(
                    url, timeout=30,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                wait = 1 * (3 ** attempt)
                if attempt < 2:
                    print(
                        f"[{self.site_id}] page {page} error "
                        f"(attempt {attempt + 1}/3): {exc}, retrying in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] page {page} failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Field extractors
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_title(metadata: dict) -> str:
        titles = metadata.get("title", [])
        for lang_pref in ("eng", "fre", "ger", "ita", None):
            for t in titles:
                for mt in t.get("mainTitle", []):
                    if lang_pref is None or mt.get("language") == lang_pref:
                        val = mt.get("value", "").strip()
                        if val:
                            return val
        return ""

    @staticmethod
    def _extract_abstract(metadata: dict) -> str:
        abstracts = metadata.get("abstracts", [])
        for lang_pref in ("eng", "fre", "ger", "ita", None):
            for ab in abstracts:
                if lang_pref is None or ab.get("language") == lang_pref:
                    val = ab.get("value", "").strip()
                    if val:
                        return val
        return ""

    @staticmethod
    def _extract_authors(metadata: dict) -> str:
        names = []
        for contrib in metadata.get("contribution", []):
            if "cre" in contrib.get("role", []):
                name = contrib.get("agent", {}).get("preferred_name", "").strip()
                if name:
                    names.append(name)
        return "; ".join(names)

    @staticmethod
    def _extract_publisher(metadata: dict) -> str:
        names = [
            org.get("name", "").strip()
            for org in metadata.get("organisation", [])
            if org.get("name", "").strip()
        ]
        return "; ".join(names)

    @staticmethod
    def _extract_journal(metadata: dict) -> str:
        for part in metadata.get("partOf", []):
            title = part.get("document", {}).get("title", "").strip()
            if title:
                return title
        return ""

    @staticmethod
    def _extract_published_date(metadata: dict) -> str:
        for act in metadata.get("provisionActivity", []):
            if act.get("type") == "bf:Publication":
                date = act.get("startDate", "").strip()
                if date:
                    return date
        return ""

    @staticmethod
    def _extract_doi(metadata: dict) -> str | None:
        for ident in metadata.get("identifiedBy", []):
            if ident.get("type") in ("bf:Doi", "doi"):
                val = ident.get("value", "").strip()
                if val:
                    return val
        for part in metadata.get("partOf", []):
            for ident in part.get("document", {}).get("identifiedBy", []):
                if ident.get("type") in ("bf:Doi", "doi"):
                    val = ident.get("value", "").strip()
                    if val:
                        return val
        return None

    @staticmethod
    def _extract_keywords(metadata: dict) -> str:
        kws = []
        for subj in metadata.get("subjects", []):
            label = subj.get("label", {})
            values = label.get("value", [])
            if isinstance(values, list):
                kws.extend(v.strip() for v in values if isinstance(v, str) and v.strip())
            elif isinstance(values, str) and values.strip():
                kws.append(values.strip())
        return ", ".join(kws)

    @staticmethod
    def _extract_pdf_info(doc_id: str, metadata: dict) -> tuple:
        # In list responses mimetype is None; detect PDF by .pdf key extension.
        # Fall back to URL construction from pid + key if links.download absent.
        for f in metadata.get("_files", []):
            key = f.get("key", "")
            if not key.lower().endswith(".pdf"):
                continue
            dl_path = f.get("links", {}).get("download", "")
            if dl_path:
                encoded = urllib.parse.quote(dl_path, safe="/:")
                pdf_url = f"https://sonar.ch{encoded}"
            else:
                pdf_url = f"https://sonar.ch/documents/{doc_id}/files/{urllib.parse.quote(key)}"
            filename = key.strip() or None
            return pdf_url, filename
        return None, None

    # ------------------------------------------------------------------
    # Record parser
    # ------------------------------------------------------------------

    def _parse_record(self, hit: dict) -> dict | None:
        doc_id = hit.get("id", "")
        metadata = hit.get("metadata", {})

        title = self._extract_title(metadata)
        if not title:
            print(f"[{self.site_id}] skipping {doc_id}: no title")
            return None

        abstract = self._extract_abstract(metadata)
        if len(abstract) < _MIN_ABSTRACT_LEN:
            print(
                f"[{self.site_id}] skipping {doc_id}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        pid = metadata.get("pid") or doc_id
        permalink = (
            metadata.get("permalink")
            or f"https://sonar.ch/global/documents/{doc_id}"
        )
        pdf_url, original_filename = self._extract_pdf_info(doc_id, metadata)

        published_date = self._extract_published_date(metadata)
        created = hit.get("created", "")
        listed_date = created[:10] if created else None  # YYYY-MM-DD

        # partOf extras for metadata
        part_of_list = metadata.get("partOf", [])
        journal_raw = part_of_list[0].get("text", "") if part_of_list else ""
        volume = ""
        issue = ""
        series = ""
        for part in part_of_list:
            volume = volume or part.get("numberingVolume", "")
            issue = issue or part.get("numberingIssue", "")
            series = series or part.get("series", "")

        extra: dict = {
            "oai_id": metadata.get("_oai", {}).get("id"),
            "document_type": metadata.get("documentType"),
            "language": [lg.get("value") for lg in metadata.get("language", [])],
            "organisation": [o.get("name") for o in metadata.get("organisation", [])],
        }
        if listed_date:
            extra["posted_date"] = created  # raw form of listed_date
        if journal_raw:
            extra["journal_raw"] = journal_raw
        if series:
            extra["series"] = series
        if volume:
            extra["volume"] = volume
        if issue:
            extra["issue"] = issue
        if original_filename:
            extra["originalFilename"] = original_filename

        return {
            "id": f"{self.site_id}:{pid}",
            "site_id": self.site_id,
            "external_id": pid,
            "post_number": pid,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,     # listed_date → posted_date → adapter → listed_date
            "authors": self._extract_authors(metadata),
            "publisher": self._extract_publisher(metadata),
            "journal": self._extract_journal(metadata),
            "url": permalink,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": self._extract_keywords(metadata),
            "doi": self._extract_doi(metadata),
            "category": _DOC_TYPE,
            "metadata": json.dumps(extra, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl SONAR.ch global journal articles.

        Paginates the Invenio REST API (newest-first) until ``limit`` items are
        saved, no more pages remain, or the 200-page / 25-minute safety cap is hit.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"
        start_page = (self.delivery_cursor or {}).get("page", 1)

        for page in range(start_page, _MAX_PAGES + 1):
            if time.monotonic() - start_time > _CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            data = self._fetch_page(page, _PAGE_SIZE)
            if data is None:
                print(f"[{self.site_id}] failed to fetch page {page}, stopping.")
                break

            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                print(f"[{self.site_id}] page {page}: no results, stopping.")
                break

            new_on_page = 0
            for hit in hits:
                if limit is not None and saved >= limit:
                    break

                self_url = hit.get("links", {}).get("self") or hit.get("id", "")
                if self_url in seen_urls:
                    continue
                seen_urls.add(self_url)
                new_on_page += 1

                try:
                    paper = self._parse_record(hit)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    doc_id = hit.get("id", "?")
                    print(f"[{self.site_id}] item {doc_id} failed: {exc}")
                    continue

            self._advance_cursor({"page": page + 1}, items_done=len(hits))

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen, stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached, stopping.")

            if limit is None or saved < limit:
                time.sleep(_RATE_SLEEP)

        print(f"[{self.site_id}] crawl complete: saved {saved} documents.")
        return saved
