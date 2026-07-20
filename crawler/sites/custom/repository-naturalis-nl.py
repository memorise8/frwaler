# -*- coding: utf-8 -*-
"""Crawler for the Naturalis Institutional Repository (articles + dissertations).

Uses OAI-PMH with MODS metadata format, which provides the PDF URL directly
in mods:location/mods:url[@access="raw object"].

Endpoint: https://repository.naturalis.nl/oai
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from xml.etree import ElementTree as ET

from crawler.base_crawler import BaseCrawler

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "mods": "http://www.loc.gov/mods/v3",
}

OAI_BASE = "https://repository.naturalis.nl/oai"
_TARGET_GENRES = frozenset({"article", "doctoralthesis", "dissertation"})
_MIN_ABSTRACT = 100
_SAFETY_PAGE_CAP = 200
_MAX_SECONDS = 25 * 60


class RepositoryNaturalisNlCrawler(BaseCrawler):
    site_id = "repository-naturalis-nl"
    site_name = "Custom: repository-naturalis-nl"
    base_url = "https://repository.naturalis.nl"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=60):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "20", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/xml,text/xml,*/*",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 15, check=False,
                )
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = f"exit={result.returncode}"
            except Exception as exc:
                last_error = str(exc)
            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl attempt {attempt + 1}/3 failed "
                    f"({last_error}); retrying in {wait}s"
                )
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _oai_url(token=None):
        if token:
            return f"{OAI_BASE}?verb=ListRecords&resumptionToken={token}"
        return f"{OAI_BASE}?verb=ListRecords&metadataPrefix=mods"

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(text):
        if not text:
            return ""
        text = unescape(str(text))
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Record parsing
    # ------------------------------------------------------------------

    def _parse_record(self, record_el):
        """Parse a MODS <record> element; return paper dict or None to skip."""
        header = record_el.find("oai:header", NS)
        if header is None:
            return None
        if header.get("status") == "deleted":
            return None

        id_el = header.find("oai:identifier", NS)
        if id_el is None or not id_el.text:
            return None
        oai_id = id_el.text.strip()
        m = re.search(r":(\d+)$", oai_id)
        pub_id = m.group(1) if m else oai_id

        ds_el = header.find("oai:datestamp", NS)
        listed_date = ""
        if ds_el is not None and ds_el.text:
            m2 = re.match(r"(\d{4}-\d{2}-\d{2})", ds_el.text.strip())
            listed_date = m2.group(1) if m2 else ""

        meta_wrap = record_el.find("oai:metadata", NS)
        if meta_wrap is None:
            return None
        mods = meta_wrap.find("mods:mods", NS)
        if mods is None:
            return None

        # Genre / type filter: only articles and dissertations
        genre_vals = [g.text.strip().lower() for g in mods.findall("mods:genre", NS) if g.text]
        if genre_vals and not any(g in _TARGET_GENRES for g in genre_vals):
            return None

        # Title
        ti = mods.find(".//mods:titleInfo/mods:title", NS)
        title = self._clean(ti.text) if ti is not None else ""
        if not title:
            return None

        # Abstract
        ab_el = mods.find("mods:abstract", NS)
        abstract = self._clean(ab_el.text) if ab_el is not None else ""

        # Authors (personal names with role aut/cre, or all personal names
        # if no role is specified)
        authors_list = []
        for name_el in mods.findall("mods:name", NS):
            if name_el.get("type") != "personal":
                continue
            role_codes = []
            for rt in name_el.findall(".//mods:roleTerm", NS):
                if rt.get("type") == "code" and rt.text:
                    role_codes.append(rt.text.strip().lower())
            if role_codes and "aut" not in role_codes and "cre" not in role_codes:
                continue
            df = name_el.find("mods:displayForm", NS)
            if df is not None and df.text:
                authors_list.append(df.text.strip())

        # Date issued
        date_el = mods.find(".//mods:originInfo/mods:dateIssued", NS)
        published_date = ""
        if date_el is not None and date_el.text:
            raw_date = date_el.text.strip()
            m3 = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
            if m3:
                published_date = m3.group(1)
            else:
                m4 = re.match(r"(\d{4})", raw_date)
                published_date = m4.group(1) if m4 else ""

        # Identifiers (uri, doi, nbn)
        pub_url = ""
        doi = ""
        nbn = ""
        for id_el2 in mods.findall("mods:identifier", NS):
            id_type = id_el2.get("type", "")
            id_val = (id_el2.text or "").strip()
            if id_type == "uri" and not pub_url:
                pub_url = id_val
            elif id_type == "doi" and not doi:
                doi = re.sub(r"^doi:\s*", "", id_val, flags=re.I)
            elif id_type == "nbn":
                nbn = id_val
        if not pub_url:
            pub_url = f"{self.base_url}/pub/{pub_id}"

        # PDF URL from mods:location/mods:url[@access="raw object"]
        pdf_url = ""
        original_filename = ""
        for loc in mods.findall("mods:location", NS):
            for url_el in loc.findall("mods:url", NS):
                if url_el.get("access") == "raw object":
                    raw_url = (url_el.text or "").strip()
                    if raw_url:
                        pdf_url = raw_url
                        original_filename = raw_url.rstrip("/").split("/")[-1].split("?")[0]
                        break

        # Keywords
        kws = []
        for subj in mods.findall("mods:subject", NS):
            for topic in subj.findall("mods:topic", NS):
                if topic.text:
                    kws.append(topic.text.strip())

        # Journal from relatedItem[@type="host"]
        journal = ""
        volume = ""
        issue = ""
        for ri in mods.findall("mods:relatedItem", NS):
            if ri.get("type") != "host":
                continue
            jti = ri.find(".//mods:titleInfo/mods:title", NS)
            if jti is not None and jti.text:
                journal = jti.text.strip()
            part = ri.find("mods:part", NS)
            if part is not None:
                for det in part.findall("mods:detail", NS):
                    num = det.find("mods:number", NS)
                    if num is not None and num.text:
                        if det.get("type") == "volume":
                            volume = num.text.strip()
                        elif det.get("type") == "issue":
                            issue = num.text.strip()
            break

        meta_dict = {"oai_identifier": oai_id}
        if nbn:
            meta_dict["nbn"] = nbn
        if listed_date:
            meta_dict["posted_date"] = listed_date
        if doi:
            meta_dict["doi"] = doi
        if original_filename:
            meta_dict["originalFilename"] = original_filename
        if volume:
            meta_dict["volume"] = volume
        if issue:
            meta_dict["issue"] = issue
        if genre_vals:
            meta_dict["genre"] = genre_vals[0]

        return {
            "site_id": self.site_id,
            "external_id": pub_id,
            "post_number": pub_id,
            "url": pub_url,
            "pdf_url": pdf_url or None,
            "original_filename": original_filename or None,
            "doi": doi or None,
            "title": title,
            "abstract": abstract,
            "authors": "; ".join(authors_list) if authors_list else None,
            "publisher": "Naturalis Biodiversity Center",
            "journal": journal or None,
            "published_date": published_date or None,
            "posted_date": listed_date or None,
            "listed_date": listed_date or None,
            "keywords": ", ".join(kws) if kws else None,
            "metadata": json.dumps(meta_dict, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls = set()
        page = 0
        token = None
        start_time = time.time()

        while True:
            if time.time() - start_time > _MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached; stopping cleanly")
                break
            if limit is not None and saved >= limit:
                break
            if page >= _SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] Safety cap of {_SAFETY_PAGE_CAP} pages reached; stopping")
                break

            url = self._oai_url(token)
            raw = self._curl(url)
            page += 1

            if not raw:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break

            try:
                root = ET.fromstring(raw.encode("utf-8", errors="replace"))
            except ET.ParseError as exc:
                print(f"[{self.site_id}] page {page}: XML parse error: {exc}; stopping")
                break

            error_el = root.find("oai:error", NS)
            if error_el is not None:
                code = error_el.get("code", "")
                msg = (error_el.text or "").strip()
                if code == "noRecordsMatch":
                    print(f"[{self.site_id}] No more records; done")
                else:
                    print(f"[{self.site_id}] OAI error: {code} – {msg}; stopping")
                break

            list_records = root.find("oai:ListRecords", NS)
            if list_records is None:
                print(f"[{self.site_id}] page {page}: no ListRecords element; stopping")
                break

            records = list_records.findall("oai:record", NS)
            if not records:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            for record_el in records:
                if limit is not None and saved >= limit:
                    break

                try:
                    paper = self._parse_record(record_el)
                    if paper is None:
                        continue

                    pub_url = paper.get("url", "")
                    if pub_url in seen_urls:
                        continue
                    seen_urls.add(pub_url)

                    abstract = paper.get("abstract") or ""
                    if len(abstract.strip()) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] skipping {paper.get('external_id', '?')}: "
                            f"abstract too short ({len(abstract.strip())} chars)"
                        )
                        continue

                    if not paper.get("title"):
                        print(f"[{self.site_id}] skipping {paper.get('external_id', '?')}: no title")
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    ext_id = "?"
                    try:
                        hdr = record_el.find("oai:header", NS)
                        if hdr is not None:
                            ie = hdr.find("oai:identifier", NS)
                            if ie is not None:
                                ext_id = (ie.text or "?").strip()
                    except Exception:
                        pass
                    print(f"[{self.site_id}] item {ext_id} failed: {exc}; continuing")

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            rt_el = list_records.find("oai:resumptionToken", NS)
            if rt_el is None or not (rt_el.text or "").strip():
                print(f"[{self.site_id}] No more pages; done")
                break
            token = rt_el.text.strip()

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
