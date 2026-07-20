# -*- coding: utf-8 -*-
"""
Crawler for Didaktorika.gr EADD (Greek National Archive of Doctoral Dissertations).
DSpace 1.8 instance — HTML-only (no REST API available).
"""

import json
import subprocess
import time
import re
import sys

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

BASE_URL = "https://www.didaktorika.gr"
SEARCH_URL = (
    "https://www.didaktorika.gr/eadd/simple-search"
    "?query=&rpp=100&sort_by=0&order=DESC"
)
CRAWL_DEADLINE_S = 25 * 60  # 25-minute wall-clock budget


def _try_bs(raw):
    """Parse HTML with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl(url, retries=3):
    """Fetch URL via curl with TLS fallback and retry/backoff. Returns bytes or None."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-L",
                 "--max-time", "30",
                 "-A",
                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/120.0.0.0 Safari/537.36",
                 url],
                capture_output=True,
                timeout=40,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception as exc:
            print(f"[didaktorika-gr-eadd] curl error attempt {attempt+1}/{retries} {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


def _decode(raw):
    """Decode bytes to str, falling back to errors='replace'."""
    if raw is None:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _parse_list_page(html):
    """Return list of unique handle IDs found in search results page."""
    soup = _try_bs(html)
    if soup is None:
        return []
    seen = set()
    ids = []
    for a in soup.find_all("a", href=True):
        m = re.match(r"/eadd/handle/10442/(\d+)$", a["href"])
        if m:
            hid = m.group(1)
            if hid not in seen:
                seen.add(hid)
                ids.append(hid)
    return ids


def _parse_detail(html, handle_id):
    """
    Parse a DSpace detail page and return a paper dict.
    Returns None if no usable abstract is found.
    """
    soup = _try_bs(html)
    if soup is None:
        return None

    def _meta_all(name):
        tags = soup.find_all("meta", attrs={"name": name})
        return [t.get("content", "").strip() for t in tags if t.get("content")]

    def _meta_lang(name, prefer_lang=None):
        tags = soup.find_all("meta", attrs={"name": name})
        # First pass: preferred language
        if prefer_lang:
            for t in tags:
                lang = (t.get("xml:lang") or t.get("lang") or "").lower()
                if lang.startswith(prefer_lang.lower()) and t.get("content", "").strip():
                    return t["content"].strip()
        # Fallback: first non-empty
        for t in tags:
            if t.get("content", "").strip():
                return t["content"].strip()
        return ""

    # --- title (prefer English) ---
    title = _meta_lang("DC.title", "en")
    if not title:
        title = _meta_lang("DC.title")
    if not title:
        title = "(untitled)"

    # --- abstract (prefer English) ---
    abstracts_en = [
        t["content"].strip()
        for t in soup.find_all("meta", attrs={"name": "DCTERMS.abstract"})
        if (t.get("xml:lang") or t.get("lang") or "").lower().startswith("en")
        and t.get("content", "").strip()
    ]
    abstracts_all = [
        t["content"].strip()
        for t in soup.find_all("meta", attrs={"name": "DCTERMS.abstract"})
        if t.get("content", "").strip()
    ]
    abstract = (abstracts_en or abstracts_all or [""])[0]

    if len(abstract) < 50:
        return None

    # --- authors (DC.creator, prefer non-Greek/Latin) ---
    creators = _meta_all("DC.creator")
    # Prefer those that are mostly ASCII (Latin script)
    latin = [c for c in creators if all(ord(ch) < 0x0370 for ch in c if ch.isalpha())]
    authors = "; ".join(latin if latin else creators)

    # --- date ---
    date_raw = _meta_lang("DC.date")
    published_date = None
    if date_raw:
        m = re.search(r"(\d{4}(?:-\d{2}-\d{2})?)", date_raw)
        if m:
            val = m.group(1)
            if len(val) == 4:
                published_date = f"{val}-01-01"
            else:
                published_date = val

    # --- identifiers: handle ID (numeric) and DOI ---
    external_id = handle_id
    doi = None
    for ident in _meta_all("DC.identifier"):
        if ident.startswith("10.") or ident.startswith("http://doi.org") or ident.startswith("https://doi.org"):
            doi = ident
        elif re.match(r"^\d+$", ident):
            external_id = ident  # numeric ID takes priority

    # --- keywords ---
    subjects = _meta_all("DC.subject")
    keywords = ", ".join(subjects) if subjects else None
    # Also try citation_keywords meta
    if not keywords:
        kw_tag = soup.find("meta", attrs={"name": "citation_keywords"})
        if kw_tag and kw_tag.get("content"):
            keywords = kw_tag["content"].strip()

    # --- publisher / institution ---
    publisher = _meta_lang("DC.publisher")
    if not publisher:
        inst_tag = soup.find("meta", attrs={"name": "citation_dissertation_institution"})
        if inst_tag:
            publisher = inst_tag.get("content", "").strip()

    # --- language ---
    language = _meta_lang("DC.language", "en")

    # --- DC.type ---
    dc_types = _meta_all("DC.type")
    category = "; ".join(dc_types) if dc_types else "Doctoral Dissertation"

    # --- contributors (advisor/committee) ---
    contributors = _meta_all("DC.contributor")

    # --- PDF URL ---
    pdf_url = None
    original_filename = None
    pdf_input = soup.find("input", attrs={"name": "pdf", "type": "hidden"})
    if pdf_input and pdf_input.get("value"):
        pdf_path = pdf_input["value"]  # e.g. /eadd/bitstream/10442/53287/1/53287.pdf
        pdf_url = BASE_URL + pdf_path
        original_filename = pdf_path.rstrip("/").split("/")[-1]

    # --- detail page URL ---
    url = f"{BASE_URL}/eadd/handle/10442/{handle_id}"

    # --- metadata dict ---
    metadata = {
        "handle_id": handle_id,
        "contributors": contributors,
        "language": language,
        "dc_types": dc_types,
        "subjects_raw": subjects,
        "all_identifiers": _meta_all("DC.identifier"),
    }
    if doi:
        metadata["doi_raw"] = doi
    if original_filename:
        metadata["originalFilename"] = original_filename

    return {
        "site_id": "didaktorika-gr-eadd",
        "external_id": external_id,
        "post_number": external_id,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "publisher": publisher,
        "url": url,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "keywords": keywords,
        "doi": doi,
        "category": category,
        "published_date": published_date,
        "listed_date": published_date,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


class DidaktorikaGrEaddCrawler(BaseCrawler):
    site_id = "didaktorika-gr-eadd"
    site_name = "Custom: didaktorika-gr-eadd"
    base_url = "https://www.didaktorika.gr"

    def crawl(self, limit=None):
        limit_inf = limit is None
        saved = 0
        seen_urls = set()
        page = 0
        max_pages = 200
        start_time = time.time()

        while True:
            # Wall-clock budget
            if time.time() - start_time > CRAWL_DEADLINE_S:
                print(f"[didaktorika-gr-eadd] 25-minute budget reached, stopping.")
                break

            if page >= max_pages:
                print(f"[didaktorika-gr-eadd] Safety cap of {max_pages} pages reached.")
                break

            offset = page * 100
            list_url = f"{SEARCH_URL}&start={offset}"
            try:
                raw = _curl(list_url)
                html = _decode(raw)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[didaktorika-gr-eadd] list page {page} fetch failed: {exc}")
                break

            handle_ids = _parse_list_page(html)
            if not handle_ids:
                print(f"[didaktorika-gr-eadd] page {page}: no items found, stopping.")
                break

            # Filter already-seen
            new_ids = [h for h in handle_ids if h not in seen_urls]
            if not new_ids:
                print(f"[didaktorika-gr-eadd] page {page}: all items already seen (loop guard), stopping.")
                break
            for h in new_ids:
                seen_urls.add(h)

            if page % 10 == 0:
                limit_display = limit if not limit_inf else "∞"
                print(f"[didaktorika-gr-eadd] page {page}: saved {saved}/{limit_display}")

            # Fetch and save each item
            for handle_id in new_ids:
                if not limit_inf and saved >= limit:
                    break

                if time.time() - start_time > CRAWL_DEADLINE_S:
                    print(f"[didaktorika-gr-eadd] 25-minute budget reached mid-page, stopping.")
                    return saved

                detail_url = f"{BASE_URL}/eadd/handle/10442/{handle_id}"
                try:
                    raw = _curl(detail_url)
                    html = _decode(raw)
                    paper = _parse_detail(html, handle_id)
                    if paper is None:
                        print(f"[didaktorika-gr-eadd] item {handle_id}: abstract too short or missing, skipping.")
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[didaktorika-gr-eadd] item {handle_id} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if not limit_inf and saved >= limit:
                break

            page += 1

        return saved
