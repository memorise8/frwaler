# -*- coding: utf-8 -*-
"""
Crawler for IRB USI (Institute for Research in Biomedicine) publications.
Starting URL: https://irb.usi.ch/publications/

Data flow:
  1. WP REST API for page 632 → current-year publications (Pods widget, server-rendered)
  2. /publications-archive/?y=XXXX → historical years (PHP server-rendered HTML)
  3. NCBI eutils bulk XML fetch for PubMed abstracts
  4. BioRxiv/medRxiv REST API for preprint abstracts
  5. Generic BeautifulSoup HTML fallback for other URLs

Publication HTML format inside <ul class="people-publications">:
  <span class="list-publications-author-title"><a href="URL">TITLE</a></span><br>
  <span class="list-publications-author-desc">AUTHORS<br>in JOURNAL (YEAR) Vol.<br></span>
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import os
import re
import time

from crawler.base_crawler import BaseCrawler


class IrbUsiChPublicationsCrawler(BaseCrawler):
    """Crawls IRB USI publications (journals + preprints) with PubMed-sourced abstracts."""

    site_id = "irb-usi-ch-publications"
    site_name = "Custom: irb-usi-ch-publications"
    base_url = "https://irb.usi.ch"

    MAX_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    MIN_ABSTRACT_CHARS = 50

    # WP REST API endpoint for page 632 — current-year publications rendered by Pods widget
    MAIN_PAGE_API = "https://irb.usi.ch/wp-json/wp/v2/pages/632?_fields=content"

    # Archive year IDs discovered from the /publications-archive/ year navigation.
    # Key = publication year, value = Pods item ID used as the ?y= query parameter.
    ARCHIVE_YEAR_IDS = {
        2026: 41819, 2025: 41817, 2024: 35922, 2023: 31520, 2022: 31428,
        2021: 20827, 2020: 14999, 2019: 14998, 2018: 14997, 2017: 14996,
        2016: 14995, 2015: 14994, 2014: 14993, 2013: 14992, 2012: 14991,
        2011: 14990, 2010: 14989, 2009: 14988, 2008: 14987, 2007: 14986,
        2006: 14985, 2005: 14984, 2004: 14983, 2003: 14982, 2002: 14981,
        2001: 14980, 2000: 14979, 1999: 21068,
    }

    _PUB_PATTERN = re.compile(
        r'<span class="list-publications-author-title">'
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'</span><br>\s*'
        r'<span class="list-publications-author-desc">(.*?)</span>',
        re.DOTALL,
    )

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        start_time = time.monotonic()
        saved = 0
        seen_urls: set = set()
        page_num = 0
        limit_str = str(limit) if limit is not None else "inf"

        # Sources: main page (current year) first, then archive years newest→oldest
        sources = [("main", None)]
        for year in sorted(self.ARCHIVE_YEAR_IDS.keys(), reverse=True):
            yid = self.ARCHIVE_YEAR_IDS[year]
            sources.append((str(year), f"{self.base_url}/publications-archive/?y={yid}"))

        for source_label, archive_url in sources:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time > self.MAX_WALL_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break

            page_num += 1
            if page_num > self.MAX_PAGES:
                print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")
                break

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            # Fetch raw publication records from this source
            try:
                if archive_url is None:
                    pubs = self._fetch_main_page()
                else:
                    pubs = self._fetch_archive_page(archive_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] source {source_label}: fetch failed: {exc}")
                continue

            if not pubs:
                continue

            # Deduplicate by external URL across all sources
            new_pubs = []
            for pub in pubs:
                key = pub["external_url"]
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                new_pubs.append(pub)

            if not new_pubs:
                continue

            # Separate PubMed publications from others for efficient batch fetching
            pmid_pubs = []
            other_pubs = []
            for pub in new_pubs:
                pmid = self._extract_pmid(pub["external_url"])
                if pmid:
                    pmid_pubs.append((pub, pmid))
                else:
                    other_pubs.append(pub)

            # Batch-fetch PubMed abstracts (one NCBI eutils call per 50 PMIDs)
            if pmid_pubs:
                pmid_list = [pmid for _, pmid in pmid_pubs]
                abstract_map = self._batch_fetch_pubmed(pmid_list)

                for pub, pmid in pmid_pubs:
                    if limit is not None and saved >= limit:
                        break
                    try:
                        abstract = re.sub(r"\s+", " ", abstract_map.get(pmid, "")).strip()
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] PMID {pmid}: "
                                f"abstract too short ({len(abstract)} chars), skipping"
                            )
                            continue
                        self._save_paper(self._build_paper(pub, abstract, external_id=pmid))
                        saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] PMID {pmid}: failed: {exc}")
                        continue

            # Handle non-PubMed publications one at a time
            for pub in other_pubs:
                if limit is not None and saved >= limit:
                    break
                try:
                    time.sleep(1.0)
                    abstract = re.sub(
                        r"\s+", " ", self._fetch_other_abstract(pub["external_url"])
                    ).strip()
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] {pub['external_url'][:70]}: "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue
                    ext_id = hashlib.sha1(pub["external_url"].encode()).hexdigest()[:16]
                    self._save_paper(self._build_paper(pub, abstract, external_id=ext_id))
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] {pub['external_url'][:70]}: failed: {exc}")
                    continue

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Source fetchers
    # ------------------------------------------------------------------

    def _fetch_main_page(self):
        """Fetch current-year publications via WP REST API (page 632, Pods widget)."""
        resp = self._request(self.MAIN_PAGE_API)
        if not resp:
            print(f"[{self.site_id}] main page REST API request failed")
            return []
        try:
            data = resp.json()
        except Exception as exc:
            print(f"[{self.site_id}] main page JSON parse failed: {exc}")
            return []
        content = (data.get("content") or {}).get("rendered", "")
        return self._parse_publications_html(content)

    def _fetch_archive_page(self, url):
        """Fetch publications from a year-specific archive page (?y=XXXX)."""
        resp = self._request(url)
        if not resp:
            return []
        raw = resp.content.decode("utf-8", errors="replace")
        return self._parse_publications_html(raw)

    # ------------------------------------------------------------------
    # HTML parser
    # ------------------------------------------------------------------

    def _parse_publications_html(self, html):
        """Parse <ul class='people-publications'> entries. Returns list of pub dicts."""
        pubs = []
        for m in self._PUB_PATTERN.finditer(html):
            try:
                external_url = html_module.unescape(m.group(1).strip())
                raw_title = re.sub(r"<[^>]+>", "", m.group(2))
                title = html_module.unescape(raw_title).strip()
                desc_html = m.group(3)

                # Desc format: "AUTHORS<br>in JOURNAL (YEAR) Vol. ...<br>"
                desc_text = re.sub(r"<br\s*/?>", "\n", desc_html)
                desc_text = re.sub(r"<[^>]+>", "", desc_text)
                desc_text = html_module.unescape(desc_text).strip()
                lines = [ln.strip() for ln in desc_text.split("\n") if ln.strip()]

                authors_str = ""
                journal_name = ""
                pub_year = ""
                vol_info = ""

                for i, line in enumerate(lines):
                    if re.match(r"in\s+\S", line) and re.search(r"\(\d{4}\)", line):
                        authors_str = " ".join(lines[:i])
                        jm = re.match(r"in\s+(.+?)\s*\((\d{4})\)\s*(.*)", line)
                        if jm:
                            journal_name = jm.group(1).strip()
                            pub_year = jm.group(2)
                            vol_info = jm.group(3).strip()
                        break
                else:
                    authors_str = " ".join(lines)

                if not title:
                    continue

                pubs.append({
                    "external_url": external_url,
                    "title": title,
                    "authors": authors_str,
                    "journal": journal_name,
                    "year": pub_year,
                    "vol_info": vol_info,
                })
            except Exception as exc:
                print(f"[{self.site_id}] parse entry failed: {exc}")
                continue
        return pubs

    # ------------------------------------------------------------------
    # Abstract fetchers
    # ------------------------------------------------------------------

    def _batch_fetch_pubmed(self, pmids):
        """Batch-fetch PubMed abstracts via NCBI eutils XML. Returns {pmid: abstract_text}."""
        if not pmids:
            return {}
        result = {}
        batch_size = 50
        for i in range(0, len(pmids), batch_size):
            batch = pmids[i: i + batch_size]
            ids_str = ",".join(batch)
            url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                f"?db=pubmed&id={ids_str}&retmode=xml&rettype=abstract"
            )
            try:
                resp = self._request(url)
                if not resp:
                    continue
                xml = resp.content.decode("utf-8", errors="replace")
                for art in re.findall(r"<PubmedArticle>(.*?)</PubmedArticle>", xml, re.DOTALL):
                    pmid_m = re.search(r"<PMID[^>]*>(\d+)</PMID>", art)
                    abs_parts = re.findall(
                        r"<AbstractText[^>]*>(.*?)</AbstractText>", art, re.DOTALL
                    )
                    if not pmid_m:
                        continue
                    pmid = pmid_m.group(1)
                    if abs_parts:
                        abstract = " ".join(abs_parts)
                        abstract = re.sub(r"<[^>]+>", "", abstract)
                        abstract = html_module.unescape(abstract)
                        result[pmid] = abstract
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{self.site_id}] PubMed batch fetch failed "
                    f"(batch {i // batch_size + 1}): {exc}"
                )
                continue
        return result

    def _fetch_other_abstract(self, url):
        """Fetch abstract from non-PubMed URL (BioRxiv/medRxiv API, then HTML fallback)."""
        # BioRxiv API
        bm = re.search(r"biorxiv\.org/content/(10\.[^/v?#\s]+)", url)
        if bm:
            abstract = self._preprint_api_abstract("biorxiv", bm.group(1))
            if abstract:
                return abstract

        # medRxiv API
        mm = re.search(r"medrxiv\.org/content/(10\.[^/v?#\s]+)", url)
        if mm:
            abstract = self._preprint_api_abstract("medrxiv", mm.group(1))
            if abstract:
                return abstract

        # Generic HTML fallback
        return self._html_abstract_fallback(url)

    def _preprint_api_abstract(self, server, doi):
        """Fetch abstract from the biorxiv.org API."""
        api_url = f"https://api.biorxiv.org/details/{server}/{doi}/json"
        try:
            resp = self._request(api_url)
            if not resp:
                return ""
            data = resp.json()
            coll = data.get("collection") or []
            if coll:
                return (coll[0].get("abstract") or "").strip()
        except Exception as exc:
            print(f"[{self.site_id}] {server} API failed for {doi}: {exc}")
        return ""

    def _html_abstract_fallback(self, url):
        """Try to extract an abstract from a web page using BeautifulSoup."""
        try:
            resp = self._request(url)
            if not resp:
                return ""
            raw = resp.content.decode("utf-8", errors="replace")

            soup = None
            for parser in ("html5lib", "lxml", "html.parser"):
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(raw, parser)
                    break
                except Exception:
                    continue
            if soup is None:
                return ""

            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            for selector in (
                "[class*='abstract']",
                "#abstract",
                ".abstract",
                "section.abstract",
                "div.abstract",
                "p.abstract",
            ):
                elem = soup.select_one(selector)
                if elem:
                    text = re.sub(r"\s+", " ", elem.get_text(" ")).strip()
                    if len(text) >= self.MIN_ABSTRACT_CHARS:
                        return text[:3000]

            meta = soup.find("meta", {"name": "description"})
            if meta:
                content = meta.get("content") or ""
                if len(content) >= self.MIN_ABSTRACT_CHARS:
                    return content[:3000]
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{self.site_id}] HTML fallback failed for {url[:70]}: {exc}")
        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_pmid(url):
        """Extract a PubMed ID from a PubMed URL, or None."""
        m = re.search(r"(?:pubmed|pubmed\.ncbi\.nlm\.nih\.gov)[/=](\d+)", url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_doi(url):
        """Try to extract a DOI from a URL, or None."""
        m = re.search(r"doi\.org/(10\.[^/\s]+/[^\s?#]+)", url)
        if m:
            return m.group(1)
        m = re.search(r"/(10\.\d{4,}/[^\s?#/]+)", url)
        if m:
            return m.group(1)
        return None

    def _build_paper(self, pub, abstract, external_id):
        """Build a paper_dict compatible with BaseCrawler._save_paper()."""
        year = pub.get("year", "")
        published_date = f"{year}-01-01" if year and year.isdigit() else None
        doi = self._extract_doi(pub["external_url"])

        metadata = {
            "source": "IRB USI Publications",
            "external_url": pub["external_url"],
            "journal_raw": pub.get("journal", ""),
            "vol_info": pub.get("vol_info", ""),
            "year": year,
        }
        if doi:
            metadata["doi"] = doi

        return {
            "external_id": external_id,
            "post_number": external_id,
            "title": pub["title"],
            "abstract": abstract,
            "authors": pub.get("authors", ""),
            "department": "IRB USI",
            "journal": pub.get("journal", ""),
            "published_date": published_date,
            "url": pub["external_url"],
            "pdf_url": None,
            "doi": doi or "",
            "keywords": "",
            "category": "Research Publication",
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
