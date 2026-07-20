# -*- coding: utf-8 -*-
"""Nacionalni portal odprte znanosti (openscience.si) - advanced-search crawler.

Starting URL: https://openscience.si/NaprednoIskanje.aspx?q=0:20:212

openscience.si is a federated search portal over Slovenian institutional
repositories (mostly the "DKUM"-family platform: dk.um.si, repozitorij.upr.si,
...). Listing results link out to per-institution detail pages that embed a
citation JSON blob (``inic_citation(id, {...})``) plus Dublin Core / citation_*
meta tags, which is what we scrape for the real abstract/authors/dates.

Pagination is classic ASP.NET WebForms postback (VIEWSTATE + EVENTVALIDATION).
Repeatedly clicking the ">>" ("PageNaprej") button gets stuck after one click
(the server re-renders the same page), but clicking the numbered pager button
for "current + 1" (the pager window re-centers around the current page once
you pass page 9) reliably advances through the result set.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode, urlparse, parse_qs

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


_PAGE_BTN_RE = re.compile(r"ctl00\$Content\$Zadetki\$ctl00\$ctl\d+$")


class OpenscienceSiNaprednoIskanjeAspxCrawler(BaseCrawler):
    """Crawler for openscience.si NaprednoIskanje.aspx (advanced search)."""

    site_id = "openscience-si-naprednoiskanjeaspx"
    site_name = "Custom: openscience-si-naprednoiskanjeaspx"
    base_url = "https://openscience.si"

    _START_URL = "https://openscience.si/NaprednoIskanje.aspx?q=0:20:212"
    _MIN_ABSTRACT = 100
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds
    _NEXT_PAGE_BUTTON = "ctl00$Content$Zadetki$ctl00$PageNaprej"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        fd, self._cookie_jar = tempfile.mkstemp(prefix="openscience_si_cookies_", suffix=".txt")
        os.close(fd)

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl(self, url, method="GET", data=None):
        """GET/POST via curl with exponential-backoff retries.

        Returns decoded text, or None after 3 failed attempts.
        """
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "40",
            "-c", self._cookie_jar, "-b", self._cookie_jar,
            "-H", f"User-Agent: {self.USER_AGENT}",
        ]
        if method == "POST":
            cmd += ["-X", "POST", "-H", "Content-Type: application/x-www-form-urlencoded",
                    "--data-binary", "@-"]
        cmd.append(url)

        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                input_bytes = data.encode("utf-8") if data is not None else None
                result = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=50)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {url}: {exc}")
        return None

    def _curl_head(self, url):
        """HEAD request (for Content-Disposition filename sniffing). Best-effort."""
        cmd = [
            "curl", "-skIL", "--tls-max", "1.3", "--max-time", "20",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=25)
            return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Form / pagination helpers
    # ------------------------------------------------------------------

    def _extract_form_fields(self, soup):
        """Return the current (non-submit) form field values, for re-posting."""
        form = soup.find("form")
        if form is None:
            return None
        fields = {}
        for inp in form.find_all(["input", "select", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            typ = (inp.get("type") or "").lower()
            if inp.name == "select":
                opt = inp.find("option", selected=True) or inp.find("option")
                val = opt.get("value", "") if opt else ""
            elif inp.name == "textarea":
                val = inp.text
            else:
                if typ in ("submit", "button", "image"):
                    continue
                if typ in ("checkbox", "radio") and not inp.has_attr("checked"):
                    continue
                val = inp.get("value", "")
            fields[name] = val
        return fields

    def _goto_next_page_fields(self, fields, soup, target_page):
        """Return a fields dict that 'clicks' the numbered pager button for
        `target_page`, or None if no such button is rendered (end of results
        or pager window doesn't include it)."""
        btn_name = None
        for b in soup.select(".PageNumbers input"):
            name = b.get("name", "")
            if _PAGE_BTN_RE.match(name) and b.get("value") == str(target_page):
                btn_name = name
                break
        if btn_name is None:
            return None
        new_fields = dict(fields)
        new_fields[btn_name] = str(target_page)
        return new_fields

    def _parse_listing_items(self, soup):
        items = []
        for div in soup.select(".Zadetek"):
            try:
                title_el = div.select_one(".NaslovZadetka a")
                if title_el is None or not title_el.get("href"):
                    continue
                detail_url = title_el["href"].strip()
                title = title_el.get_text(strip=True)
                # Some source records only carry a bare "http://dx.doi.org/"
                # stub (no DOI suffix, no id= query) instead of a real
                # per-item link. These are indistinguishable from one
                # another, so they'd collapse into a single seen_urls key
                # and stall pagination / dedup. Skip them here; there's no
                # usable detail page to fetch for them anyway.
                parsed_detail = urlparse(detail_url)
                if not parsed_detail.netloc or (
                    not parsed_detail.path.strip("/") and not parsed_detail.query
                ):
                    print(f"[{self.site_id}] listing item skipped (no per-item detail URL): {title[:60]}")
                    continue
                authors = [a.get_text(strip=True) for a in div.select(".AvtorZadetka a")]
                vrsta_el = div.select_one(".VrstaDelaZadetka")
                vrsta = vrsta_el.get_text(strip=True) if vrsta_el else ""
                kljucne_el = div.select_one(".KljucneBesedeZadetka")
                kljucne = kljucne_el.get_text(" ", strip=True) if kljucne_el else ""
                kljucne = re.sub(r"^Oznake:\s*", "", kljucne).strip()
                opis_el = div.select_one(".OpisZadetka")
                opis = opis_el.get_text(strip=True) if opis_el else ""
                leto_el = div.select_one(".LetoZadetka")
                leto = leto_el.get_text(" ", strip=True) if leto_el else ""
                leto = re.sub(r"^Leto:\s*", "", leto).strip()
                vir_el = div.select_one(".VirZadetka")
                vir = vir_el.get_text(" ", strip=True) if vir_el else ""
                vir = re.sub(r"^Vir:\s*", "", vir).strip()
                items.append({
                    "detail_url": detail_url,
                    "title": title,
                    "authors": authors,
                    "vrsta": vrsta,
                    "keywords_raw": kljucne,
                    "opis": opis,
                    "leto": leto,
                    "vir": vir,
                })
            except Exception as exc:
                print(f"[{self.site_id}] listing item parse failed: {exc}; skipping")
                continue
        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html):
        soup = _make_soup(html)
        meta = {}
        for tag in soup.find_all("meta"):
            name = tag.get("name") or tag.get("property")
            content = tag.get("content")
            if not name or content is None:
                continue
            meta.setdefault(name, []).append(content)

        citation = {}
        m = re.search(r"inic_citation\(\s*\d+\s*,\s*(\{.*?\})\s*\)\s*;", html, re.DOTALL)
        if m:
            try:
                citation = json.loads(m.group(1))
            except Exception:
                citation = {}

        return meta, citation

    def _meta1(self, meta, *names):
        for name in names:
            vals = meta.get(name)
            if vals:
                return vals[0].strip()
        return None

    def _format_date(self, citation, meta):
        parts = None
        issued = citation.get("issued") if isinstance(citation, dict) else None
        if isinstance(issued, dict):
            dp = issued.get("date-parts")
            if dp and isinstance(dp, list) and dp and isinstance(dp[0], list):
                parts = dp[0]
        if parts:
            try:
                year = int(parts[0])
                month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
                return f"{year:04d}-{month:02d}-{day:02d}"
            except (ValueError, TypeError):
                pass
        year_str = self._meta1(meta, "citation_publication_date", "DC.issued", "DC.date")
        if year_str:
            m = re.match(r"(\d{4})", year_str.strip())
            if m:
                return f"{m.group(1)}-01-01"
        return None

    def _authors_from_detail(self, citation, meta):
        authors = []
        cit_authors = citation.get("author") if isinstance(citation, dict) else None
        if isinstance(cit_authors, list):
            for a in cit_authors:
                if not isinstance(a, dict):
                    continue
                given = (a.get("given") or "").strip()
                family = (a.get("family") or "").strip()
                name = f"{given} {family}".strip()
                if name:
                    authors.append(name)
        if not authors:
            for raw in meta.get("citation_author", []) + meta.get("DC.creator", []):
                raw = raw.strip()
                if not raw:
                    continue
                if "," in raw:
                    family, _, given = raw.partition(",")
                    name = f"{given.strip()} {family.strip()}".strip()
                else:
                    name = raw
                if name and name not in authors:
                    authors.append(name)
        for raw in meta.get("DC.contributor", []):
            raw = raw.strip()
            if not raw:
                continue
            if "," in raw:
                family, _, given = raw.partition(",")
                name = f"{given.strip()} {family.strip()}".strip()
            else:
                name = raw
            if name and name not in authors:
                authors.append(name)
        return authors

    def _fetch_original_filename(self, pdf_url):
        if not pdf_url:
            return None
        try:
            headers = self._curl_head(pdf_url)
            if not headers:
                return None
            m = re.search(r'filename\*?=\s*"?([^";\r\n]+)"?', headers, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        seen_urls = set()
        lim_str = str(limit) if limit is not None else "inf"

        html = self._curl(self._START_URL, method="GET")
        if not html:
            print(f"[{self.site_id}] could not fetch start page; aborting")
            return saved

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] could not parse start page: {exc}; aborting")
            return saved

        current_page = 1
        page_num = 0
        consecutive_empty_pages = 0
        # Some listing entries only carry a bare "http://dx.doi.org/" stub
        # href (see _parse_listing_items) instead of a real detail link, and
        # a page can legitimately be made up entirely of such junk entries.
        # A single page with 0 *new* records is therefore not a reliable
        # end-of-results signal by itself; require a run of consecutive
        # empty pages (real end, or the paginator looping back) before
        # stopping on that basis.
        _MAX_CONSECUTIVE_EMPTY = 5

        try:
            while True:
                page_num += 1
                elapsed = time.monotonic() - start_time
                if elapsed > self._MAX_WALL:
                    print(f"[{self.site_id}] wall-clock budget ({self._MAX_WALL}s) exceeded; stopping")
                    break
                if page_num > self._MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")
                    break
                if limit is not None and saved >= limit:
                    break

                raw_count = len(soup.select(".Zadetek"))
                if raw_count == 0:
                    print(f"[{self.site_id}] page {page_num}: empty results list; stopping")
                    break

                items = self._parse_listing_items(soup)
                new_items = [it for it in items if it["detail_url"] not in seen_urls]

                if not new_items:
                    consecutive_empty_pages += 1
                    print(f"[{self.site_id}] page {page_num}: 0 new records "
                          f"({consecutive_empty_pages}/{_MAX_CONSECUTIVE_EMPTY} consecutive)")
                    if consecutive_empty_pages >= _MAX_CONSECUTIVE_EMPTY:
                        print(f"[{self.site_id}] {_MAX_CONSECUTIVE_EMPTY} consecutive empty pages; stopping")
                        break
                else:
                    consecutive_empty_pages = 0

                for entry in new_items:
                    if limit is not None and saved >= limit:
                        break
                    detail_url = entry["detail_url"]
                    seen_urls.add(detail_url)
                    try:
                        time.sleep(self._delay)
                        detail_html = self._curl(detail_url, method="GET")
                        meta, citation = ({}, {})
                        if detail_html:
                            meta, citation = self._parse_detail(detail_html)

                        title = (self._meta1(meta, "citation_title", "DC.title")
                                 or entry["title"] or "(untitled)")

                        abstract = (citation.get("abstract") if isinstance(citation, dict) else None) or ""
                        abstract = abstract.strip()
                        if len(abstract) < self._MIN_ABSTRACT:
                            abstract = entry["opis"].strip()
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] item skipped (abstract too short): {title[:60]}")
                            continue

                        authors = self._authors_from_detail(citation, meta)
                        if not authors:
                            authors = entry["authors"]
                        authors_str = "; ".join(authors) if authors else None

                        published_date = self._format_date(citation, meta)
                        if not published_date and entry["leto"]:
                            ym = re.match(r"(\d{4})", entry["leto"])
                            if ym:
                                published_date = f"{ym.group(1)}-01-01"

                        institution = self._meta1(meta, "citation_dissertation_institution")
                        publisher = None
                        department = None
                        if institution:
                            if "," in institution:
                                publisher, _, department = institution.partition(",")
                                publisher = publisher.strip() or None
                                department = department.strip() or None
                            else:
                                publisher = institution.strip()
                        if not publisher:
                            publisher = entry["vir"] or None

                        journal = self._meta1(meta, "citation_journal_title")

                        keywords = None
                        cit_keyword = citation.get("keyword") if isinstance(citation, dict) else None
                        if cit_keyword:
                            keywords = ", ".join(
                                k.strip() for k in re.split(r"[;,]", cit_keyword) if k.strip()
                            )
                        elif entry["keywords_raw"]:
                            keywords = ", ".join(
                                k.strip() for k in entry["keywords_raw"].split(";") if k.strip()
                            )
                        elif meta.get("DC.subject"):
                            keywords = ", ".join(
                                k.strip() for k in re.split(r"[;,]", meta["DC.subject"][0]) if k.strip()
                            )

                        category = entry["vrsta"] or self._meta1(meta, "DC.type") or citation.get("genre")

                        doi = self._meta1(meta, "citation_doi")

                        pdf_url = self._meta1(meta, "citation_pdf_url")
                        original_filename = self._fetch_original_filename(pdf_url)

                        parsed = urlparse(detail_url)
                        domain = parsed.netloc
                        qs = parse_qs(parsed.query)
                        native_id = (qs.get("id") or [None])[0]
                        post_number = native_id if native_id and native_id.isdigit() else None
                        external_id = f"{domain}:{native_id}" if native_id else detail_url

                        metadata = {
                            "posted_date": None,
                            "originalFilename": original_filename,
                            "journal_raw": journal,
                            "series": None,
                            "volume": None,
                            "issue": None,
                            "node_id": native_id,
                            "domain": domain,
                            "og_site": self._meta1(meta, "og:site"),
                            "genre": citation.get("genre") if isinstance(citation, dict) else None,
                            "archive_place": citation.get("archive-place") if isinstance(citation, dict) else None,
                            "publisher_place": citation.get("publisher-place") if isinstance(citation, dict) else None,
                            "dc_type": self._meta1(meta, "DC.type"),
                            "vrsta_dela": entry["vrsta"],
                            "vir": entry["vir"],
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": post_number,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": None,
                            "authors": authors_str,
                            "publisher": publisher,
                            "department": department,
                            "journal": journal,
                            "url": detail_url,
                            "pdf_url": pdf_url,
                            "keywords": keywords,
                            "category": category,
                            "doi": doi,
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item failed (url={detail_url}): {exc}; continuing")
                        continue

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

                if limit is not None and saved >= limit:
                    break

                fields = self._extract_form_fields(soup)
                if fields is None:
                    print(f"[{self.site_id}] could not extract form fields; stopping")
                    break

                target_page = current_page + 1
                next_fields = self._goto_next_page_fields(fields, soup, target_page)
                if next_fields is None:
                    print(f"[{self.site_id}] no pager button for page {target_page}; end of results")
                    break

                body = urlencode(next_fields)
                html = self._curl(self._START_URL, method="POST", data=body)
                if not html:
                    print(f"[{self.site_id}] page {target_page} fetch failed; stopping")
                    break
                try:
                    soup = _make_soup(html)
                except Exception as exc:
                    print(f"[{self.site_id}] could not parse page {target_page}: {exc}; stopping")
                    break
                current_page = target_page

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
