# -*- coding: utf-8 -*-
"""DORAS DCU thesis repository crawler.

Target: https://doras.dcu.ie/view/type/thesis.html
Strategy: download EPrints JSON export (all ~3 850 theses, ~18 MB in one
response), sort by eprintid descending (newest first), apply limit, save.
No per-item detail fetch is needed — the export already contains full abstracts.
"""

import json
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler


class DorasDcuIeViewCrawler(BaseCrawler):
    site_id = "doras-dcu-ie-view"
    site_name = "Custom: doras-dcu-ie-view"
    base_url = "https://doras.dcu.ie"

    _JSON_EXPORT_URL = (
        "https://doras.dcu.ie/cgi/exportview/type/thesis/JSON/thesis.json"
    )
    _PAGE_SIZE = 100          # logical "page" size for progress logging
    _MAX_PAGES = 200          # safety cap (100 * 200 = 20 000 items)
    _MAX_SECONDS = 25 * 60   # wall-clock budget
    _MIN_ABSTRACT = 100       # skip items whose abstract is shorter than this

    # ------------------------------------------------------------------
    def crawl(self, limit=None):
        limit_label = str(limit) if limit is not None else "inf"
        print(f"[{self.site_id}] starting crawl limit={limit_label}")
        start_time = time.time()

        records = self._fetch_json_export()
        if not records:
            print(f"[{self.site_id}] ERROR: could not fetch JSON export")
            return 0

        print(f"[{self.site_id}] fetched {len(records)} records")

        # Newest-first so MAX(post_number) stop logic works for incremental runs
        records.sort(key=lambda r: r.get("eprintid") or 0, reverse=True)

        saved = 0
        seen_ids: set = set()
        page_num = 0
        total = len(records)

        for batch_start in range(0, total, self._PAGE_SIZE):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > self._MAX_SECONDS:
                print(
                    f"[{self.site_id}] 25-minute budget reached; "
                    f"exiting cleanly at saved={saved}"
                )
                break

            page_num += 1
            if page_num > self._MAX_PAGES:
                print(
                    f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages "
                    f"reached; exiting at saved={saved}"
                )
                break

            batch = records[batch_start: batch_start + self._PAGE_SIZE]
            if not batch:
                print(f"[{self.site_id}] page {page_num}: empty batch, done")
                break

            if page_num % 10 == 0:
                print(
                    f"[{self.site_id}] page {page_num}: "
                    f"saved {saved}/{limit_label}"
                )

            for rec in batch:
                if limit is not None and saved >= limit:
                    break

                try:
                    ok = self._process_record(rec, seen_ids)
                    if ok:
                        saved += 1
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_label}: "
                            f"{(rec.get('title') or '')[:60]}"
                        )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item "
                        f"{rec.get('eprintid', '?')} failed: {exc}"
                    )
                    continue

        print(f"[{self.site_id}] done. total saved={saved}")
        return saved

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_json_export(self):
        """Download full JSON export with 3-attempt exponential back-off."""
        backoffs = [1, 3, 9]
        for attempt, backoff in enumerate(backoffs):
            try:
                resp = self._session.get(self._JSON_EXPORT_URL, timeout=180)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except Exception:
                    data = json.loads(
                        resp.content.decode("utf-8", errors="replace")
                    )
                if isinstance(data, list):
                    return data
                print(
                    f"[{self.site_id}] unexpected JSON type "
                    f"{type(data).__name__} on attempt {attempt + 1}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{self.site_id}] fetch attempt {attempt + 1}/3 "
                    f"failed: {exc}"
                )
            if attempt < 2:
                time.sleep(backoff)
        return None

    def _process_record(self, rec, seen_ids: set) -> bool:
        """Parse one EPrints JSON record and persist it. Returns True if saved."""
        eprintid = rec.get("eprintid")
        if not eprintid:
            return False
        if eprintid in seen_ids:
            return False
        seen_ids.add(eprintid)

        title = " ".join((rec.get("title") or "").split())
        if not title:
            return False

        abstract = (rec.get("abstract") or "").strip()
        if len(abstract) < self._MIN_ABSTRACT:
            print(
                f"[{self.site_id}] skip {eprintid}: "
                f"abstract {len(abstract)} chars < {self._MIN_ABSTRACT}"
            )
            return False

        # --- authors -------------------------------------------------------
        creators = rec.get("creators") or []
        author_parts = []
        for c in creators:
            name = c.get("name") or {}
            family = (name.get("family") or "").strip()
            given = (name.get("given") or "").strip()
            full = f"{given} {family}".strip() if given else family
            if full:
                author_parts.append(full)
        authors = "; ".join(author_parts) or None

        # --- dates ---------------------------------------------------------
        raw_date = str(rec.get("date") or "").strip()
        published_date = _normalize_date(raw_date)

        raw_datestamp = str(rec.get("datestamp") or "").strip()
        # datestamp = repository deposit timestamp → listed_date
        posted_date = raw_datestamp[:10] if len(raw_datestamp) >= 10 else None

        # --- PDF + filename ------------------------------------------------
        pdf_url, original_filename = self._extract_pdf(eprintid, rec)

        # --- keywords ------------------------------------------------------
        kw_raw = rec.get("keywords")
        if isinstance(kw_raw, list):
            keywords = ", ".join(str(k).strip() for k in kw_raw if k)
        elif isinstance(kw_raw, str) and kw_raw.strip():
            # EPrints sometimes uses semicolons; normalise to commas
            keywords = re.sub(r"\s*;\s*", ", ", kw_raw.strip()).strip(", ")
        else:
            keywords = None

        # --- thesis-specific fields ----------------------------------------
        institution = (rec.get("institution") or "Dublin City University").strip()
        thesis_type = (rec.get("thesis_type") or "").strip()
        thesis_name = (rec.get("thesis_name") or "").strip()

        supervisors = rec.get("supervisors") or []
        sup_names = []
        for s in supervisors:
            sname = s.get("name") or {}
            sg = (sname.get("given") or "").strip()
            sf = (sname.get("family") or "").strip()
            sn = f"{sg} {sf}".strip() if sg else sf
            if sn:
                sup_names.append(sn)

        # --- metadata blob -------------------------------------------------
        metadata_raw = {
            "posted_date": raw_datestamp or None,
            "originalFilename": original_filename,
            "thesis_type": thesis_type or None,
            "thesis_name": thesis_name or None,
            "subjects": rec.get("subjects") or None,
            "supervisors": sup_names or None,
            "date_type": rec.get("date_type") or None,
            "full_text_status": rec.get("full_text_status") or None,
            "use_license": rec.get("use_license") or None,
        }
        metadata_raw = {k: v for k, v in metadata_raw.items() if v is not None}

        self._save_paper({
            "site_id": self.site_id,
            "external_id": str(eprintid),
            "post_number": str(eprintid),
            "url": f"{self.base_url}/{eprintid}/",
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": posted_date,
            "authors": authors,
            "publisher": institution,
            "department": "",
            "journal": "",
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "category": thesis_type,
            "doi": "",
            "metadata": json.dumps(metadata_raw, ensure_ascii=False),
        })
        return True

    def _extract_pdf(self, eprintid: int, rec: dict) -> tuple:
        """Return (pdf_url, original_filename) for the first public PDF doc."""
        for doc in rec.get("documents") or []:
            if doc.get("security") != "public":
                continue
            fmt = doc.get("format") or doc.get("mime_type") or ""
            if "pdf" not in fmt.lower():
                continue
            placement = doc.get("placement") or doc.get("pos") or 1
            main_file = doc.get("main") or ""
            if not main_file:
                for f in doc.get("files") or []:
                    if "pdf" in (f.get("mime_type") or "").lower():
                        main_file = f.get("filename") or ""
                        break
            if main_file:
                encoded = urllib.parse.quote(main_file)
                pdf_url = f"{self.base_url}/{eprintid}/{placement}/{encoded}"
                return pdf_url, main_file
        return None, None


def _normalize_date(raw: str):
    """Return ISO date string from EPrints date field."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d", "%Y-%m", "%Y",
    ):
        try:
            dt = datetime.strptime(raw[: len(fmt)], fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw[:10] if len(raw) >= 10 else raw
