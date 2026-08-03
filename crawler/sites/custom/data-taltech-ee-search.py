# -*- coding: utf-8 -*-
"""TalTech Data Repository crawler (InvenioRDM JSON API).

Target:
  https://data.taltech.ee/search?q=&f=access_status:open&f=file_type:pdf
    &f=resource_type:dataset&f=resource_type:publication&f=is_published:true
    &l=list&p=1&s=10&sort=newest

The site runs InvenioRDM 13.  All metadata is available via the REST API at
/api/records without any authentication.  File metadata (including the PDF
filename) is embedded in the search response under record["files"]["entries"],
so no per-record detail fetch is required.
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import quote, urlencode

from crawler.base_crawler import BaseCrawler


class DataTaltechEeSearchCrawler(BaseCrawler):
    """Crawler for TalTech Data Repository (InvenioRDM)."""

    site_id = "data-taltech-ee-search"
    site_name = "Custom: data-taltech-ee-search"
    base_url = "https://data.taltech.ee"

    _API_BASE = "https://data.taltech.ee/api/records"
    _PAGE_SIZE = 25
    _WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------ #
    # Network                                                              #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url):
        """GET *url* via curl; return decoded text or None after 3 attempts."""
        cmd = [
            "curl", "-sk", "--max-time", "30",
            "-H", "Accept: application/json",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        delays = [1, 3, 9]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = res.stdout
                if raw and raw.strip():
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    print(f"[{self.site_id}] Empty response, retry in {delays[attempt]}s…")
                    time.sleep(delays[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {delays[attempt]}s…")
                    time.sleep(delays[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # Parsing                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_html(html):
        """Strip HTML tags and normalise whitespace."""
        text = re.sub(r"<[^>]+>", " ", html or "")
        for entity, repl in [("&nbsp;", " "), ("&amp;", "&"),
                              ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')]:
            text = text.replace(entity, repl)
        text = re.sub(r"&#?\w+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_date(dt_str):
        """Return the leading YYYY-MM-DD (or YYYY-MM / YYYY) from any date string."""
        if not dt_str:
            return None
        m = re.match(r"(\d{4}(?:-\d{2}(?:-\d{2})?)?)", str(dt_str))
        return m.group(1) if m else None

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        start_ts = time.time()
        saved = 0
        page = 1
        seen_ids = set()
        limit_label = str(limit) if limit is not None else "inf"

        while True:
            # --- guard rails ---
            if time.time() - start_ts > self._WALL_BUDGET:
                print(f"[{self.site_id}] 25-minute wall budget reached. Stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if page > 200:
                print(f"[{self.site_id}] Safety cap of 200 pages reached. Stopping.")
                break

            # --- fetch list page ---
            qs = urlencode([
                ("q", ""),
                ("access_status", "open"),
                ("resource_type", "dataset"),
                ("resource_type", "publication"),
                ("is_published", "true"),
                ("sort", "newest"),
                ("size", str(self._PAGE_SIZE)),
                ("page", str(page)),
            ])
            list_url = f"{self._API_BASE}?{qs}"

            raw = self._curl_get(list_url)
            if raw is None:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[{self.site_id}] JSON parse error at page {page}: {exc}. Stopping.")
                break

            hits = data.get("hits", {})
            records = hits.get("hits", [])

            if page == 1:
                total = hits.get("total", {})
                total_val = total.get("value") if isinstance(total, dict) else total
                print(f"[{self.site_id}] Total records on server: {total_val}")

            if not records:
                print(f"[{self.site_id}] No records on page {page}. Done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            # --- process records ---
            for rec in records:
                if limit is not None and saved >= limit:
                    break

                try:
                    rec_id = rec.get("id", "")
                    if not rec_id or rec_id in seen_ids:
                        continue
                    seen_ids.add(rec_id)

                    meta = rec.get("metadata") or {}
                    title = (meta.get("title") or "").strip()

                    # Abstract: HTML description → plain text
                    abstract = self._strip_html(meta.get("description") or "")
                    if len(abstract) < 100:
                        print(f"[{self.site_id}] Skip {rec_id} — abstract too short "
                              f"({len(abstract)} chars): {title[:50]!r}")
                        continue

                    # Dates
                    pub_date = self._parse_date(meta.get("publication_date"))
                    listed_date = self._parse_date(rec.get("created", ""))

                    # Authors (semicolon-separated)
                    creators = meta.get("creators") or []
                    author_names = []
                    affiliation_names = []
                    for c in creators:
                        poo = c.get("person_or_org") or {}
                        name = (poo.get("name") or "").strip()
                        if name:
                            author_names.append(name)
                        for aff in c.get("affiliations") or []:
                            aff_name = (aff.get("name") or "").strip()
                            if aff_name and aff_name not in affiliation_names:
                                affiliation_names.append(aff_name)
                    authors_str = ";".join(author_names) or None

                    publisher = (meta.get("publisher") or "").strip() or None
                    if not publisher and affiliation_names:
                        publisher = ";".join(affiliation_names)

                    # Keywords (comma-separated)
                    subjects = meta.get("subjects") or []
                    kw_list = [s.get("subject", "").strip() for s in subjects
                               if s.get("subject")]
                    keywords_str = ",".join(kw_list) or None

                    # DOI
                    doi = ((rec.get("pids") or {})
                           .get("doi", {}).get("identifier")) or None

                    # Category from resource_type
                    res_type = meta.get("resource_type") or {}
                    category = ((res_type.get("title") or {}).get("en")
                                or res_type.get("id") or None)

                    # PDF info from embedded files.entries (dict keyed by filename)
                    files_field = rec.get("files") or {}
                    pdf_url = None
                    orig_filename = None
                    if files_field.get("enabled"):
                        entries = files_field.get("entries") or {}
                        for fname, fmeta in entries.items():
                            ext = (fmeta.get("ext") or "").lower()
                            mime = (fmeta.get("mimetype") or "").lower()
                            if ext == "pdf" or "pdf" in mime:
                                orig_filename = fname
                                encoded_name = quote(fname, safe="")
                                pdf_url = (f"{self._API_BASE}/{rec_id}"
                                           f"/files/{encoded_name}/content")
                                break

                    # Detail page URL
                    detail_url = (rec.get("links") or {}).get("self_html") or ""

                    # Metadata: all unmapped raw fields
                    metadata_dict = {
                        "posted_date": rec.get("created"),
                        "updated": rec.get("updated"),
                        "resource_type_id": res_type.get("id"),
                        "is_latest": (rec.get("versions") or {}).get("is_latest"),
                        "versions_index": (rec.get("versions") or {}).get("index"),
                        "languages": [
                            lang.get("id")
                            for lang in (meta.get("languages") or [])
                            if lang.get("id")
                        ] or None,
                        "rights": [
                            r.get("id")
                            for r in (meta.get("rights") or [])
                            if r.get("id")
                        ] or None,
                        "dates": meta.get("dates") or None,
                        "affiliations": affiliation_names or None,
                        "oai_identifier": ((rec.get("pids") or {})
                                           .get("oai", {}).get("identifier")),
                        "originalFilename": orig_filename,
                        "doi": doi,
                        "category": category,
                    }
                    metadata_dict = {
                        k: v for k, v in metadata_dict.items()
                        if v not in (None, [], "", {})
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": rec_id,
                        "post_number": rec_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "listed_date": listed_date,
                        "authors": authors_str,
                        "publisher": publisher,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": orig_filename,
                        "keywords": keywords_str,
                        "doi": doi,
                        "category": category,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_label}: {title[:60]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {rec.get('id', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
