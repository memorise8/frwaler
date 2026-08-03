# -*- coding: utf-8 -*-
"""MRC LMB Cambridge — Published Research crawler.

Source:    https://www2.mrc-lmb.cam.ac.uk/research/published-research/
Canonical: https://mrclmb.ac.uk/publications/

Publications are rendered by a custom Klug WordPress plugin (recent block).
Full searchable database (~10k+ entries) is via Formidable Forms AJAX.
Abstracts are fetched from PubMed E-utilities (free, no API key needed).
"""

import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, Optional, Set

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

SITE_ID = "www2-mrc-lmb-cam-ac-uk-research"
LISTING_URL = "https://mrclmb.ac.uk/publications/"
AJAX_URL = "https://mrclmb.ac.uk/wp-admin/admin-ajax.php"
PUBMED_EFETCH = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    "?db=pubmed&retmode=xml&id={pmid}"
)
MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """BeautifulSoup with fallback chain: html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl(url: str, method: str = "GET", data: Optional[Dict] = None,
          retries: int = 3) -> Optional[str]:
    """Fetch URL with curl, retry up to `retries` times (1s/3s/9s backoff)."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "-L",
        "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (compatible; research-crawler/1.0)",
        "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    ]
    if method == "POST":
        cmd += ["-X", "POST"]
        if data:
            for k, v in (data or {}).items():
                cmd += ["--data-urlencode", f"{k}={v}"]
    cmd.append(url)

    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=40)
            raw = r.stdout
            if not raw:
                raise ValueError("empty response")
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("utf-8", errors="replace")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                print(f"[{SITE_ID}] curl retry {attempt + 1}: {exc}, sleeping {wait}s")
                time.sleep(wait)
            else:
                print(f"[{SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


def _parse_date(text: str) -> Optional[str]:
    """Extract ISO date from strings like 'May 2026' or 'Epub 2026 May 11'."""
    # 'Month YYYY'
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})", text)
    if m:
        return f"{m.group(2)}-{MONTH_MAP[m.group(1)]}-01"
    # 'YYYY Mon DD'
    m2 = re.search(r"(\d{4})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})", text)
    if m2:
        return f"{m2.group(1)}-{MONTH_MAP[m2.group(2)]}-{m2.group(3).zfill(2)}"
    return None


def _fetch_pubmed_xml(pmid: str) -> Optional[str]:
    return _curl(PUBMED_EFETCH.format(pmid=pmid))


def _parse_pubmed_xml(xml: str) -> Dict:
    """Parse PubMed efetch XML into a dict with abstract, authors, doi, etc."""
    result: Dict = {}

    # Abstract (structured or simple)
    parts = re.findall(
        r'<AbstractText(?:[^>]*Label="([^"]*)")?[^>]*>(.*?)</AbstractText>',
        xml, re.DOTALL,
    )
    if parts:
        chunks = []
        for label, text in parts:
            clean = re.sub(r"<[^>]+>", "", text).strip()
            if not clean:
                continue
            chunks.append(f"{label}: {clean}" if label else clean)
        if chunks:
            result["abstract"] = " ".join(chunks)
    if not result.get("abstract"):
        m = re.search(r"<AbstractText>(.*?)</AbstractText>", xml, re.DOTALL)
        if m:
            result["abstract"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # Authors
    author_blocks = re.findall(r"<Author[^>]*>(.*?)</Author>", xml, re.DOTALL)
    if author_blocks:
        authors = []
        for block in author_blocks:
            last_m = re.search(r"<LastName>([^<]+)</LastName>", block)
            first_m = re.search(r"<ForeName>([^<]+)</ForeName>", block)
            if last_m:
                name = last_m.group(1).strip()
                if first_m:
                    name = f"{name}, {first_m.group(1).strip()}"
                authors.append(name)
        if authors:
            result["authors"] = "; ".join(authors)

    # DOI
    doi_m = re.search(r'<ArticleId IdType="doi">([^<]+)</ArticleId>', xml)
    if doi_m:
        result["doi"] = doi_m.group(1).strip()

    # Published date (prefer PubDate, fall back to ArticleDate)
    pubdate_m = re.search(r"<PubDate>(.*?)</PubDate>", xml, re.DOTALL)
    if pubdate_m:
        pd = pubdate_m.group(1)
        year_m = re.search(r"<Year>(\d{4})</Year>", pd)
        month_m = re.search(r"<Month>([^<]+)</Month>", pd)
        day_m = re.search(r"<Day>(\d+)</Day>", pd)
        if year_m:
            year = year_m.group(1)
            month = "01"
            if month_m:
                raw_m = month_m.group(1).strip()
                month = MONTH_MAP.get(raw_m, raw_m.zfill(2) if raw_m.isdigit() else "01")
            day = day_m.group(1).zfill(2) if day_m else "01"
            result["published_date"] = f"{year}-{month}-{day}"

    # Volume / Issue
    vol_m = re.search(r"<Volume>([^<]+)</Volume>", xml)
    if vol_m:
        result["volume"] = vol_m.group(1).strip()
    iss_m = re.search(r"<Issue>([^<]+)</Issue>", xml)
    if iss_m:
        result["issue"] = iss_m.group(1).strip()

    # Keywords (MeSH or author keywords)
    kw_matches = re.findall(r"<Keyword[^>]*>([^<]+)</Keyword>", xml)
    if kw_matches:
        result["keywords"] = ", ".join(k.strip() for k in kw_matches)

    # Journal ISO abbreviation
    jrn_m = re.search(r"<ISOAbbreviation>([^<]+)</ISOAbbreviation>", xml)
    if jrn_m:
        result["journal"] = jrn_m.group(1).strip()
    if not result.get("journal"):
        jrn_m2 = re.search(r"<Title>([^<]+)</Title>", xml)
        if jrn_m2:
            result["journal"] = jrn_m2.group(1).strip()

    return result


# ---------------------------------------------------------------------------
# crawler
# ---------------------------------------------------------------------------

class MrcLmbResearchCrawler(BaseCrawler):
    site_id = SITE_ID
    site_name = "Custom: www2-mrc-lmb-cam-ac-uk-research"
    base_url = "https://www2.mrc-lmb.cam.ac.uk"

    def crawl(self, limit=None):
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: Set[str] = set()
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        # --- Page 1: parse from initial HTML ---
        html = _curl(LISTING_URL)
        if not html:
            print(f"[{SITE_ID}] Failed to fetch listing page")
            return saved

        soup = _make_soup(html)
        if not soup:
            print(f"[{SITE_ID}] Failed to parse listing page HTML")
            return saved

        # Extract Formidable Forms nonce for AJAX pagination
        nonce = None
        nonce_m = re.search(r'"nonce"\s*:\s*"([a-f0-9]{8,})"', html)
        if nonce_m:
            nonce = nonce_m.group(1)

        p = 1
        while saved < limit_or_inf:
            if time.time() - start_time > MAX_WALL:
                print(f"[{SITE_ID}] Wall clock budget reached at page {p}, stopping")
                break
            if p > 200:
                print(f"[{SITE_ID}] Safety cap of 200 pages reached")
                break
            if p % 10 == 0:
                print(f"[{SITE_ID}] page {p}: saved {saved}/{limit_or_inf}")

            # Get publication divs for this page
            if p == 1:
                pub_divs = soup.find_all("div", class_="wp-block-klug-publication")
            else:
                page_html = self._fetch_ajax_page(p, nonce)
                if not page_html:
                    break
                page_soup = _make_soup(page_html)
                if not page_soup:
                    break
                pub_divs = page_soup.find_all("div", class_="wp-block-klug-publication")

            if not pub_divs:
                break

            new_this_page = 0
            for pub_div in pub_divs:
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > MAX_WALL:
                    print(f"[{SITE_ID}] Wall clock budget reached mid-page, stopping")
                    return saved

                try:
                    ok = self._process_pub(pub_div, seen_urls)
                    if ok:
                        saved += 1
                        new_this_page += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{SITE_ID}] item failed: {exc}")
                    continue

                time.sleep(1.0)  # rate-limit between PubMed fetches

            if new_this_page == 0:
                # All items on this page were already seen → end of pagination
                break

            p += 1

        return saved

    # ------------------------------------------------------------------

    def _fetch_ajax_page(self, page: int, nonce: Optional[str]) -> Optional[str]:
        """Try multiple Formidable Forms AJAX patterns for page N."""
        _bad = {"0", "-1", "false", "null", ""}

        # Attempt 1: frm_entries_list
        d1: Dict = {"action": "frm_entries_list", "form_id": "2", "frm_page": str(page)}
        if nonce:
            d1["nonce"] = nonce
        r1 = _curl(AJAX_URL, method="POST", data=d1)
        if r1 and r1.strip() not in _bad:
            return r1

        # Attempt 2: frm_filter_entries
        d2: Dict = {"action": "frm_filter_entries", "form": "2", "page": str(page)}
        if nonce:
            d2["nonce"] = nonce
        r2 = _curl(AJAX_URL, method="POST", data=d2)
        if r2 and r2.strip() not in _bad:
            return r2

        # Attempt 3: frm_ajax_submit with empty search fields
        d3: Dict = {
            "action": "frm_ajax_submit",
            "form_id": "2",
            "frm_page_order[2]": str(page),
            "item_meta[6]": "",
            "item_meta[7]": "",
            "item_meta[8]": "",
            "item_meta[9]": "",
            "item_meta[10]": "",
        }
        if nonce:
            d3["frm_submit_entry_2"] = nonce
        r3 = _curl(AJAX_URL, method="POST", data=d3)
        if r3 and r3.strip() not in _bad:
            return r3

        return None

    # ------------------------------------------------------------------

    def _process_pub(self, pub_div, seen_urls: Set[str]) -> bool:
        """Parse one Klug publication div and save it. Returns True if saved."""
        # Title is on the <a> tag itself (class klug-publication__title)
        title_tag = pub_div.find(class_="klug-publication__title")
        if not title_tag:
            return False

        title = title_tag.get_text(strip=True)
        if not title:
            return False

        # The title element is the link; fall back to first <a> if needed
        if title_tag.name == "a":
            link_tag = title_tag
        else:
            link_tag = pub_div.find("a")
        if not link_tag:
            return False

        pub_url = (link_tag.get("href") or "").strip()
        if not pub_url or pub_url in seen_urls:
            return False
        seen_urls.add(pub_url)

        # PMID from PubMed URL
        pmid_m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", pub_url)
        pmid = pmid_m.group(1) if pmid_m else None

        # Authors from listing
        authors_tag = pub_div.find("span", class_="klug-publication__authors")
        authors_listing = authors_tag.get_text(strip=True) if authors_tag else ""

        # Journal info from listing
        journal_tag = pub_div.find("span", class_="klug-publication__journal")
        journal_raw = ""
        journal_name = None
        if journal_tag:
            journal_raw = journal_tag.get_text(separator=" • ", strip=True)
            strong = journal_tag.find("strong")
            if strong:
                journal_name = strong.get_text(strip=True)

        doi_m = re.search(r"doi:\s*(10\.\S+)", journal_raw, re.IGNORECASE)
        doi = doi_m.group(1).rstrip(".,;") if doi_m else None

        listed_date = _parse_date(journal_raw)
        published_date = listed_date

        # Enrich from PubMed
        abstract = None
        authors = authors_listing
        volume = None
        issue = None
        keywords = None

        if pmid:
            xml_text = _fetch_pubmed_xml(pmid)
            if xml_text:
                pm = _parse_pubmed_xml(xml_text)
                abstract = pm.get("abstract")
                if pm.get("authors"):
                    authors = pm["authors"]
                if pm.get("published_date"):
                    published_date = pm["published_date"]
                if not doi and pm.get("doi"):
                    doi = pm["doi"]
                volume = pm.get("volume")
                issue = pm.get("issue")
                keywords = pm.get("keywords")
                if not journal_name and pm.get("journal"):
                    journal_name = pm["journal"]

        if not abstract or len(abstract) < 50:
            print(f"[{SITE_ID}] skipping (abstract too short/missing): {title[:60]}")
            return False

        metadata: Dict = {"pmid": pmid, "journal_raw": journal_raw}
        if volume:
            metadata["volume"] = volume
        if issue:
            metadata["issue"] = issue
        if doi:
            metadata["doi"] = doi

        paper = {
            "site_id": self.site_id,
            "external_id": pmid if pmid else pub_url,
            "post_number": pmid,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": "MRC Laboratory of Molecular Biology",
            "journal": journal_name,
            "url": pub_url,
            "pdf_url": None,
            "keywords": keywords,
            "doi": doi,
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
        self._save_paper(paper)
        return True
