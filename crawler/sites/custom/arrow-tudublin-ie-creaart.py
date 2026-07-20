# -*- coding: utf-8 -*-
"""Crawler for ARROW@TU Dublin – Creative Arts (creaart) collection.

Uses the OAI-PMH endpoint which provides structured XML with full metadata
(title, abstract, authors, DOI, PDF URL, keywords) without per-item detail fetches.

OAI-PMH endpoint:
  https://arrow.tudublin.ie/do/oai/?verb=ListRecords&metadataPrefix=oai_dc&set=publication:creaart
  Pagination via resumptionToken.  Total ~231 records.
"""

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_OAI_BASE = "https://arrow.tudublin.ie/do/oai/"

# XML namespaces used by the OAI-PMH response
_NS = {
    "oai":    "http://www.openarchives.org/OAI/2.0/",
    "dc":     "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}

_SAFETY_CAP = 200       # max pages before forced stop
_BUDGET_SECS = 25 * 60  # 25-minute wall-clock cap


def _curl_get(url, retries=3):
    """GET via curl with exponential backoff. Returns bytes or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
                 "-A", "Mozilla/5.0", url],
                capture_output=True,
                timeout=35,
            )
            data = result.stdout
            if data and data.strip():
                return data
        except Exception as exc:
            print(f"[arrow-tudublin-ie-creaart] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = (attempt + 1) ** 2  # 1s, 4s
            time.sleep(wait)
    return None


def _text(el):
    """Return stripped text of an ElementTree element, or empty string if None."""
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_date(raw):
    """Parse ISO datetime like '2021-01-25T08:00:00Z' → 'YYYY-MM-DD', or None."""
    if not raw:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw.strip())
    return m.group(1) if m else None


class ArrowTudublinIeCreaartCrawler(BaseCrawler):
    """Crawler for ARROW@TU Dublin Creative Arts articles (creaart collection)."""

    site_id   = "arrow-tudublin-ie-creaart"
    site_name = "Custom: arrow-tudublin-ie-creaart"
    base_url  = "https://arrow.tudublin.ie"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        page = 0
        resumption_token = None
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "∞"

        while True:
            # Time budget guard
            if time.time() - start_time > _BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= _SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {_SAFETY_CAP} pages reached. Stopping.")
                break

            # Build OAI-PMH request URL
            if resumption_token is None:
                url = (f"{_OAI_BASE}?verb=ListRecords"
                       f"&metadataPrefix=oai_dc&set=publication:creaart")
            else:
                url = f"{_OAI_BASE}?verb=ListRecords&resumptionToken={resumption_token}"

            page += 1
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            raw = _curl_get(url)
            if not raw:
                print(f"[{self.site_id}] Empty response on page {page}. Stopping.")
                break

            try:
                root = ET.fromstring(raw)
            except ET.ParseError as exc:
                print(f"[{self.site_id}] XML parse error on page {page}: {exc}. Stopping.")
                break

            list_records = root.find("oai:ListRecords", _NS)
            if list_records is None:
                # May be an OAI error element
                err = root.find("oai:error", _NS)
                msg = _text(err) if err is not None else "(unknown)"
                print(f"[{self.site_id}] OAI error on page {page}: {msg}. Stopping.")
                break

            records = list_records.findall("oai:record", _NS)
            if not records:
                print(f"[{self.site_id}] No records on page {page}. Done.")
                break

            new_on_page = 0
            for record in records:
                if limit is not None and saved >= limit:
                    break

                oai_id = "(unknown)"
                try:
                    hdr = record.find("oai:header", _NS)
                    if hdr is not None:
                        id_el = hdr.find("oai:identifier", _NS)
                        if id_el is not None:
                            oai_id = _text(id_el)

                    ok = self._process_record(record, seen_urls)
                    if ok:
                        saved += 1
                        new_on_page += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {oai_id} failed: {exc}")
                    continue

            # Check for next page
            rt_el = list_records.find("oai:resumptionToken", _NS)
            if rt_el is not None and rt_el.text and rt_el.text.strip():
                resumption_token = rt_el.text.strip()
            else:
                print(f"[{self.site_id}] No more pages after page {page}. Done.")
                break

            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} were duplicates. Stopping.")
                break

            time.sleep(1.0)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def _process_record(self, record, seen_urls):
        """Parse one OAI record and persist it. Returns True if saved, False otherwise."""
        header = record.find("oai:header", _NS)
        if header is None:
            return False
        if header.get("status") == "deleted":
            return False

        oai_id   = _text(header.find("oai:identifier", _NS))
        datestamp = _text(header.find("oai:datestamp", _NS))
        listed_date = _parse_date(datestamp)

        metadata_el = record.find("oai:metadata", _NS)
        if metadata_el is None:
            return False
        dc = metadata_el.find("oai_dc:dc", _NS)
        if dc is None:
            return False

        # Core fields
        title = _text(dc.find("dc:title", _NS))
        if not title:
            return False

        abstract = _text(dc.find("dc:description", _NS))
        if len(abstract) < 100:
            print(f"[{self.site_id}] Skipping '{title[:50]}': abstract too short ({len(abstract)} chars)")
            return False

        # Authors
        creators = [_text(el) for el in dc.findall("dc:creator", _NS)]
        authors = "; ".join(c for c in creators if c)

        # Dates
        pub_date_raw = _text(dc.find("dc:date", _NS))
        published_date = _parse_date(pub_date_raw)

        # Publisher, journal/source
        publisher = _text(dc.find("dc:publisher", _NS))
        journal   = _text(dc.find("dc:source", _NS))

        # Keywords
        subjects = [_text(el) for el in dc.findall("dc:subject", _NS)]
        keywords = ", ".join(s for s in subjects if s)

        # Identifiers: page URL / PDF URL / DOI
        identifiers = [_text(el) for el in dc.findall("dc:identifier", _NS)]
        page_url = None
        pdf_url  = None
        doi      = None

        for ident in identifiers:
            if not ident:
                continue
            if ident.startswith("info:doi/"):
                doi = ident[len("info:doi/"):]
            elif "viewcontent" in ident or (ident.startswith("http") and ident.endswith(".pdf")):
                pdf_url = ident
            elif re.match(r"https?://arrow\.tudublin\.ie/\w+/\d+$", ident):
                page_url = ident

        # Fallback: any ARROW URL that isn't the PDF
        if not page_url:
            for ident in identifiers:
                if ident and ident.startswith("https://arrow.tudublin.ie/") and "viewcontent" not in ident:
                    page_url = ident
                    break

        if not page_url:
            return False

        # Dedup
        if page_url in seen_urls:
            return False
        seen_urls.add(page_url)

        # external_id: last component of OAI identifier, e.g. "creaart-1000"
        external_id = oai_id.rsplit(":", 1)[-1] if oai_id else None

        # post_number: trailing number from page URL (display sequence)
        post_number = None
        m = re.search(r"/(\d+)$", page_url)
        if m:
            post_number = m.group(1)

        # original_filename from PDF URL
        original_filename = None
        if pdf_url:
            fname = urlparse(pdf_url).path.rsplit("/", 1)[-1].split("?")[0]
            if fname and "." in fname and len(fname) <= 255:
                original_filename = fname

        # Internal article number from OAI ID (e.g. "creaart-1000" → "1000")
        article_num = None
        if external_id:
            m2 = re.search(r"-(\d+)$", external_id)
            if m2:
                article_num = m2.group(1)

        # Rights
        rights_list = [_text(el) for el in dc.findall("dc:rights", _NS)]
        rights = "; ".join(r for r in rights_list if r)

        meta = {
            "oai_identifier":  oai_id,
            "article_number":  article_num,
            "datestamp":       datestamp,
            "source":          journal,
            "rights":          rights,
            "dc_type":         _text(dc.find("dc:type", _NS)),
            "dc_format":       _text(dc.find("dc:format", _NS)),
            "posted_date":     listed_date,   # for libertree_adapter extraction
        }

        paper = {
            "site_id":           self.site_id,
            "external_id":       external_id,
            "title":             title,
            "abstract":          abstract,
            "published_date":    published_date,
            "posted_date":       listed_date,
            "authors":           authors,
            "publisher":         publisher,
            "journal":           journal,
            "url":               page_url,
            "pdf_url":           pdf_url,
            "doi":               doi,
            "keywords":          keywords,
            "original_filename": original_filename,
            "metadata":          json.dumps({k: v for k, v in meta.items() if v},
                                            ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f"[{self.site_id}] Saved: {title[:70]}")
        return True
