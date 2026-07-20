# -*- coding: utf-8 -*-
"""Crawler for Research@THEA (research.thea.ie) — "Browse by Title" index.

The site's own HTML (browse pages, handle detail pages, and even bitstream
downloads) sits behind an Anubis JavaScript proof-of-work bot-gate, so plain
HTTP requests only ever receive a "Making sure you're not a bot!" challenge
page. Note for future maintainers: the challenge page embeds a hidden
honeypot link (``/.within.website/x/cmd/anubis/api/honeypot/<uuid>/init``)
designed purely to bait scrapers — it is NOT a real content URL and must
never be treated as one.

The repository is DSpace-based and exposes an open, unprotected OAI-PMH
endpoint (``/oai/request`` — explicitly excluded from robots.txt's
``Disallow: /browse`` rule and not gated by Anubis) which is the standard,
sanctioned way to harvest the full item list with real metadata:

  https://research.thea.ie/oai/request?verb=ListRecords&metadataPrefix=oai_dc
  Pagination via resumptionToken.  ~3100 records repository-wide, which is
  the OAI equivalent of the "browse by title" index (no per-collection set
  filter = whole repository, sorted by internal id).

oai_dc records carry title/abstract/authors/dates/publisher/journal
(dc:source)/subjects/rights/identifiers, but never a bitstream (PDF) URL.
For that we do one lightweight "detail" call per item:

  https://research.thea.ie/oai/request?verb=GetRecord&metadataPrefix=mets&identifier=<oai-id>

whose <fileSec> lists the actual bitstream download URLs.
"""

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_OAI_BASE = "https://research.thea.ie/oai/request"

_NS = {
    "oai":  "http://www.openarchives.org/OAI/2.0/",
    "dc":   "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "mets": "http://www.loc.gov/METS/",
    "xlink": "http://www.w3.org/1999/xlink",
}

_SAFETY_CAP = 200        # max list pages before forced stop
_BUDGET_SECS = 25 * 60   # 25-minute wall-clock cap
_DETAIL_SLEEP = 1.0      # seconds between per-item detail (mets) fetches
_MIN_ABSTRACT_LEN = 50

_BACKOFFS = (1, 3, 9)    # exponential backoff schedule for network retries

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;\"'<>)]+")


def _curl_get(url, retries=3, timeout=30):
    """GET via curl with exponential backoff (1s, 3s, 9s). Returns bytes or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "-sk", "--tls-max", "1.3", "--max-time", str(timeout),
                 "-A", "Mozilla/5.0", url],
                capture_output=True,
                timeout=timeout + 5,
            )
            data = result.stdout
            if data and data.strip():
                return data
        except Exception as exc:
            print(f"[research-thea-ie-browse] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            time.sleep(_BACKOFFS[min(attempt, len(_BACKOFFS) - 1)])
    return None


def _parse_xml(raw):
    """Parse OAI XML from bytes, tolerating odd encodings. Returns Element or None."""
    if not raw:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        try:
            text = raw.decode("utf-8", errors="replace")
            return ET.fromstring(text)
        except Exception:
            return None


def _text(el):
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_date(raw):
    """Normalize a date string to 'YYYY-MM-DD'. Returns None if unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)[:10]
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        return f"{raw}-01"
    m = re.match(r"^(\d{4})$", raw)
    if m:
        return f"{raw}-01-01"
    return None


class ResearchTheaIeBrowseCrawler(BaseCrawler):
    """Crawler for Research@THEA (DSpace) via its open OAI-PMH endpoint."""

    site_id = "research-thea-ie-browse"
    site_name = "Custom: research-thea-ie-browse"
    base_url = "https://research.thea.ie"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        page = 0
        resumption_token = None
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        while True:
            if time.time() - start_time > _BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= _SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {_SAFETY_CAP} pages reached. Stopping.")
                break

            if resumption_token is None:
                url = f"{_OAI_BASE}?verb=ListRecords&metadataPrefix=oai_dc"
            else:
                url = f"{_OAI_BASE}?verb=ListRecords&resumptionToken={resumption_token}"

            page += 1
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            raw = _curl_get(url)
            root = _parse_xml(raw)
            if root is None:
                print(f"[{self.site_id}] Empty/unparseable response on page {page}. Stopping.")
                break

            list_records = root.find("oai:ListRecords", _NS)
            if list_records is None:
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
                    header = record.find("oai:header", _NS)
                    if header is not None:
                        id_el = header.find("oai:identifier", _NS)
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

            rt_el = list_records.find("oai:resumptionToken", _NS)
            if rt_el is not None and rt_el.text and rt_el.text.strip():
                resumption_token = rt_el.text.strip()
            else:
                print(f"[{self.site_id}] No more pages after page {page}. Done.")
                break

            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} were duplicates/skipped. Stopping.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------

    def _process_record(self, record, seen_urls):
        """Parse one oai_dc record, enrich with a per-item detail (mets) fetch, and save."""
        header = record.find("oai:header", _NS)
        if header is None:
            return False
        if header.get("status") == "deleted":
            return False

        oai_id = _text(header.find("oai:identifier", _NS))
        datestamp = _text(header.find("oai:datestamp", _NS))

        metadata_el = record.find("oai:metadata", _NS)
        if metadata_el is None:
            return False
        dc = metadata_el.find("oai_dc:dc", _NS)
        if dc is None:
            return False

        title = _text(dc.find("dc:title", _NS))
        if not title:
            return False

        abstract = _text(dc.find("dc:description", _NS))
        if len(abstract) < _MIN_ABSTRACT_LEN:
            print(f"[{self.site_id}] Skipping '{title[:50]}': abstract too short ({len(abstract)} chars)")
            return False

        creators = [_text(el) for el in dc.findall("dc:creator", _NS)]
        authors = "; ".join(c for c in creators if c)

        contributors = [_text(el) for el in dc.findall("dc:contributor", _NS)]

        # dc:date holds a mix of ISO accession timestamps and a bare
        # publication year/date — separate them out.
        raw_dates = [_text(el) for el in dc.findall("dc:date", _NS) if _text(el)]
        iso_dates = sorted(d for d in raw_dates if "T" in d)
        plain_dates = [d for d in raw_dates if "T" not in d]

        published_date = None
        for d in plain_dates:
            published_date = _parse_date(d)
            if published_date:
                break

        listed_date_raw = iso_dates[0] if iso_dates else datestamp
        listed_date = _parse_date(listed_date_raw) or _parse_date(datestamp)

        publishers = [_text(el) for el in dc.findall("dc:publisher", _NS)]
        publisher = "; ".join(p for p in publishers if p)

        journal = _text(dc.find("dc:source", _NS))

        subjects = [_text(el) for el in dc.findall("dc:subject", _NS)]
        keywords = ", ".join(s for s in subjects if s)

        type_values = [_text(el) for el in dc.findall("dc:type", _NS)]
        category = next((t for t in type_values if t and not t.lower().startswith("info")), None)
        if not category and type_values:
            category = type_values[0]

        rights_list = [_text(el) for el in dc.findall("dc:rights", _NS)]
        rights = "; ".join(r for r in rights_list if r)

        identifiers = [_text(el) for el in dc.findall("dc:identifier", _NS) if _text(el)]

        page_url = None
        for ident in identifiers:
            m = re.search(r"https?://research\.thea\.ie/handle/\S+", ident)
            if m:
                page_url = "https://" + m.group(0).split("://", 1)[1]
                break
        if not page_url:
            # Fall back to constructing it from the handle embedded in the OAI id.
            m = re.search(r"oai:research\.thea\.ie:(\S+)", oai_id)
            if m:
                page_url = f"{self.base_url}/handle/{m.group(1)}"
        if not page_url:
            return False

        if page_url in seen_urls:
            return False
        seen_urls.add(page_url)

        doi = None
        for ident in identifiers:
            m = _DOI_RE.search(ident)
            if m:
                doi = m.group(0).rstrip(".")
                break

        handle = None
        m = re.search(r"/handle/(\S+)$", page_url)
        if m:
            handle = m.group(1)
        external_id = handle or oai_id.rsplit(":", 1)[-1]

        post_number = None
        if handle:
            m2 = re.search(r"/(\d+)$", handle)
            if m2:
                post_number = m2.group(1)

        # --- per-item detail fetch: bitstream (PDF) URL via mets format ---
        pdf_url = None
        original_filename = None
        try:
            detail = self._fetch_bitstream_detail(oai_id)
            if detail:
                pdf_url = detail.get("pdf_url")
                original_filename = detail.get("original_filename")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{self.site_id}] detail fetch failed for {oai_id}: {exc}")
        finally:
            time.sleep(_DETAIL_SLEEP)

        meta = {
            "oai_identifier": oai_id,
            "datestamp": datestamp,
            "posted_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": journal or None,
            "rights": rights or None,
            "dc_type_all": type_values or None,
            "contributors": "; ".join(c for c in contributors if c) or None,
        }

        paper = {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "journal": journal,
            "url": page_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps({k: v for k, v in meta.items() if v}, ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f"[{self.site_id}] Saved: {title[:70]}")
        return True

    def _fetch_bitstream_detail(self, oai_id):
        """Fetch the mets record for oai_id and extract the primary bitstream URL.

        Retries up to 3 times with exponential backoff. Returns a dict with
        ``pdf_url``/``original_filename`` (either may be None), or None on
        total failure — callers treat that as "no enrichment available" and
        keep the already-parsed oai_dc data.
        """
        url = (f"{_OAI_BASE}?verb=GetRecord&metadataPrefix=mets"
               f"&identifier={oai_id}")
        raw = _curl_get(url)
        if raw is None:
            return None

        root = _parse_xml(raw)
        if root is None:
            return None

        get_record = root.find("oai:GetRecord", _NS)
        if get_record is None:
            return None
        record = get_record.find("oai:record", _NS)
        if record is None:
            return None
        metadata_el = record.find("oai:metadata", _NS)
        if metadata_el is None:
            return None
        mets_el = metadata_el.find("mets:mets", _NS)
        if mets_el is None:
            return None

        file_sec = mets_el.find("mets:fileSec", _NS)
        if file_sec is None:
            return None

        pdf_url = None
        any_url = None
        for file_grp in file_sec.findall("mets:fileGrp", _NS):
            if file_grp.get("USE") != "ORIGINAL":
                continue
            for file_el in file_grp.findall("mets:file", _NS):
                flocat = file_el.find("mets:FLocat", _NS)
                if flocat is None:
                    continue
                href = flocat.get("{http://www.w3.org/1999/xlink}href")
                if not href:
                    continue
                if any_url is None:
                    any_url = href
                if file_el.get("MIMETYPE") == "application/pdf":
                    pdf_url = href
                    break
            if pdf_url:
                break

        chosen = pdf_url or any_url
        original_filename = None
        if chosen:
            fname = unquote(urlparse(chosen).path.rsplit("/", 1)[-1])
            if fname and "." in fname and len(fname) <= 255:
                original_filename = fname

        return {"pdf_url": chosen, "original_filename": original_filename}
