# -*- coding: utf-8 -*-
"""Crawler for helios.eie.gr – DSpace 6.4 NHRF Helios institutional repository.

Target: journal articles (Άρθρο σε επιστημονικό περιοδικό) under handle 10442/11.
Strategy: reverse-paginate (last page first) so newer items with abstracts are
found immediately, making small limits (e.g. limit=3) fast.
"""

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler

_BASE = "https://helios.eie.gr"
_LIST_URL = f"{_BASE}/helios/handle/10442/11/simple-search"
_FILTER_VALUE = "Άρθρο σε επιστημονικό περιοδικό"
_RPP = 100
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_TIMEOUT = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # seconds
_MIN_ABSTRACT = 50          # items with shorter abstract are skipped


def _make_soup(raw):
    """Build BeautifulSoup with html5lib → lxml → html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw.decode("utf-8", errors="replace")
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl_get(url, max_time=30):
    """GET via curl with TLS 1.3 and 3 retries (1s, 3s, 9s backoff)."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(max_time),
        "-A", (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=max_time + 5)
            if r.returncode == 0 and r.stdout:
                try:
                    return r.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return r.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[helios-eie-gr-helios] curl attempt {attempt + 1}/3 failed: {exc}")
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


def _get_total(html):
    """Extract total result count from DSpace search page HTML."""
    m = re.search(r"Αποτελέσματα για \d+-\d+ από ([\d\s\xa0]+)", html)
    if m:
        try:
            return int(re.sub(r"\s", "", m.group(1)))
        except ValueError:
            pass
    return None


def _extract_en_or_el(text):
    """Prefer [EN] name from '[EL] Greek [EN] English' bilingual strings."""
    en = re.search(r"\[EN\]\s+(.+?)(?:\s*\[EL\]|\s*$)", text, re.DOTALL)
    if en:
        return en.group(1).strip()
    el = re.search(r"\[EL\]\s+(.+?)(?:\s*\[EN\]|\s*$)", text, re.DOTALL)
    if el:
        return el.group(1).strip()
    return text.strip()


class HeliosEieGrHeliosCrawler(BaseCrawler):

    site_id = "helios-eie-gr-helios"
    site_name = "Custom: helios-eie-gr-helios"
    base_url = "https://helios.eie.gr"

    # ------------------------------------------------------------------
    # List-page helpers
    # ------------------------------------------------------------------

    def _list_page_url(self, start):
        params = urlencode(
            {
                "query": "",
                "filter_field_1": "searchFilterTypeQualified",
                "filter_type_1": "equals",
                "filter_value_1": _FILTER_VALUE,
                "sort_by": "score",
                "order": "desc",
                "rpp": str(_RPP),
                "start": str(start),
                "etal": "0",
            },
            encoding="utf-8",
        )
        return f"{_LIST_URL}?{params}"

    def _parse_handles(self, html):
        """Return item handle paths from a search result page (title column only)."""
        soup = _make_soup(html)
        if not soup:
            return []

        found, seen = [], set()

        # Primary: title cells are <td headers="t3"> in the results table
        for td in soup.find_all("td", attrs={"headers": "t3"}):
            a = td.find("a", href=re.compile(r"^/helios/handle/10442/\d+$"))
            if a:
                href = a["href"]
                if href not in seen:
                    seen.add(href)
                    found.append(href)

        if found:
            return found

        # Fallback: scope to discovery-result-results div
        container = soup.find("div", class_="discovery-result-results") or soup
        for a in container.find_all("a", href=re.compile(r"^/helios/handle/10442/\d+$")):
            href = a["href"]
            if href not in seen:
                seen.add(href)
                found.append(href)
        return found

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    @staticmethod
    def _table_field(soup, css_class):
        """Get text of <td class='metadataFieldValue {css_class}'>."""
        td = soup.select_one(f"td.metadataFieldValue.{css_class}")
        if td is None:
            # Some classes use hyphens translated to underscores differently
            td = soup.find("td", class_=css_class)
        return unescape(td.get_text(separator=" ", strip=True)) if td else ""

    def _parse_detail(self, html, handle_path):
        """Parse item detail page. Returns paper dict or None."""
        soup = _make_soup(html)
        if not soup:
            return None

        tf = lambda cls: self._table_field(soup, cls)

        title = tf("dc_title")
        if not title:
            # Fallback: DC.title meta tag
            m = soup.find("meta", attrs={"name": "DC.title"})
            title = m["content"].strip() if m else ""
        if not title:
            return None

        abstract = tf("dcterms_abstract")

        publisher = tf("dc_publisher")
        date_raw = tf("dc_date")
        doi = tf("dc_identifier_doi")
        journal = tf("ekt_source_title")
        volume = tf("ekt_source_volume")
        issue = tf("ekt_source_issue")
        pages_field = tf("dcterms_extent")
        issn = tf("dc_identifier_issn")
        lang = tf("dc_language")
        peer_reviewed = tf("ekt_peerReview")

        # Authors: <td class="… dc_creator"> → <a class="author">
        authors_list = []
        creator_td = soup.find("td", class_="dc_creator")
        if creator_td:
            for a in creator_td.find_all("a", class_="author"):
                raw = a.get_text(separator=" ", strip=True)
                name = _extract_en_or_el(raw)
                if name:
                    authors_list.append(name)
        # Fallback: DC.creator meta tags
        if not authors_list:
            for m in soup.find_all("meta", attrs={"name": "DC.creator"}):
                val = m.get("content", "").strip()
                if val:
                    authors_list.append(val)

        # Keywords: prefer lom_classification_keyword, then dc_subject
        kw_td = soup.find("td", class_="lom_classification_keyword")
        if kw_td:
            keywords = ",".join(
                a.get_text(strip=True)
                for a in kw_td.find_all("a", class_="keyword")
                if a.get_text(strip=True)
            )
        else:
            subj_td = soup.find("td", class_="dc_subject")
            if subj_td:
                kws = [
                    _extract_en_or_el(a.get_text(separator=" ", strip=True))
                    for a in subj_td.find_all("a", class_="subject")
                ]
                keywords = ",".join(k for k in kws if k)
            else:
                # Fallback: DC.subject meta tags
                kws = [
                    m.get("content", "").strip()
                    for m in soup.find_all("meta", attrs={"name": "DC.subject"})
                ]
                keywords = ",".join(k for k in kws if k)

        # Category: first dc_subject entry (EN preferred)
        category = ""
        subj_td2 = soup.find("td", class_="dc_subject")
        if subj_td2:
            fa = subj_td2.find("a", class_="subject")
            if fa:
                category = _extract_en_or_el(fa.get_text(separator=" ", strip=True))

        # PDF URL
        pdf_url = None
        original_filename = None
        # Prefer citation_pdf_url meta tag
        pmeta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if pmeta and pmeta.get("content"):
            pdf_url = pmeta["content"].strip()
        else:
            for a in soup.find_all("a", href=re.compile(r"/helios/bitstream/10442/")):
                href = a.get("href", "")
                if href.lower().endswith(".pdf"):
                    pdf_url = f"{_BASE}{href}" if href.startswith("/") else href
                    break
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]

        # Handle → external_id and post_number
        m2 = re.search(r"/helios/handle/(10442/(\d+))$", handle_path)
        external_id = m2.group(1) if m2 else handle_path.lstrip("/")
        post_number = m2.group(2) if m2 else None

        # listed_date from DCTERMS.available or DCTERMS.dateAccepted meta
        listed_date = None
        for mname in ("DCTERMS.available", "DCTERMS.dateAccepted"):
            tag = soup.find("meta", attrs={"name": mname})
            if tag and tag.get("content"):
                val = tag["content"].strip()[:10]
                if re.match(r"\d{4}-\d{2}-\d{2}", val):
                    listed_date = val
                    break

        # Normalise published_date to YYYY-MM-DD
        published_date = ""
        if date_raw:
            dm = re.match(r"(\d{4})(?:[^0-9](\d{2})(?:[^0-9](\d{2}))?)?", date_raw)
            if dm:
                yr = dm.group(1)
                mo = dm.group(2) or "01"
                dy = dm.group(3) or "01"
                published_date = f"{yr}-{mo}-{dy}"
        if not published_date and listed_date:
            published_date = listed_date

        metadata = {k: v for k, v in {
            "posted_date": date_raw or None,
            "issn": issn or None,
            "volume": volume or None,
            "issue": issue or None,
            "pages": pages_field or None,
            "journal_raw": journal or None,
            "language": lang or None,
            "peer_reviewed": peer_reviewed or None,
            "originalFilename": original_filename or None,
        }.items() if v}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "authors": "; ".join(authors_list) if authors_list else None,
            "published_date": published_date or None,
            "listed_date": listed_date,
            "publisher": publisher or None,
            "journal": journal or None,
            "url": f"{_BASE}{handle_path}",
            "pdf_url": pdf_url,
            "doi": doi or None,
            "department": None,
            "keywords": keywords or None,
            "original_filename": original_filename or None,
            "category": category or None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        crawl_start = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # Step 1: fetch first page to get total count
        first_html = _curl_get(self._list_page_url(0), max_time=30)
        total = _get_total(first_html) if first_html else None
        if total is None:
            total = 9999
            print("[helios-eie-gr-helios] Could not read total count, using fallback 9999")
        else:
            print(f"[helios-eie-gr-helios] Total items in collection: {total}")

        # Step 2: build page offsets in REVERSE order.
        # Newer items (with abstracts) appear at the END of the score-sorted
        # results, so paginating end→start finds them first for small limits.
        last_start = ((total - 1) // _RPP) * _RPP
        page_starts = list(range(last_start, -1, -_RPP))

        try:
            for page_idx, start_offset in enumerate(page_starts):
                elapsed = time.time() - crawl_start
                if elapsed > _CRAWL_TIMEOUT:
                    print(
                        f"[helios-eie-gr-helios] 25-min wall budget reached "
                        f"at page {page_idx + 1}. Stopping cleanly."
                    )
                    break

                if limit is not None and saved >= limit:
                    break

                if page_idx >= _MAX_PAGES:
                    print(
                        f"[helios-eie-gr-helios] Safety cap of {_MAX_PAGES} pages reached."
                    )
                    break

                # Fetch list page with retries
                raw = None
                for attempt in range(3):
                    raw = _curl_get(self._list_page_url(start_offset), max_time=30)
                    if raw:
                        break
                    wait = [1, 3, 9][attempt]
                    print(
                        f"[helios-eie-gr-helios] list page {page_idx + 1} "
                        f"(start={start_offset}) fetch failed, retry in {wait}s"
                    )
                    time.sleep(wait)

                if not raw:
                    print(
                        f"[helios-eie-gr-helios] Skipping list page {page_idx + 1} "
                        f"(start={start_offset}) after 3 failures."
                    )
                    continue

                handles = self._parse_handles(raw)
                if not handles:
                    print(
                        f"[helios-eie-gr-helios] No items at start={start_offset}. Continuing."
                    )
                    continue

                # Dedup across pages to prevent infinite loops
                new_handles = [h for h in handles if h not in seen_urls]
                for h in new_handles:
                    seen_urls.add(h)

                if not new_handles:
                    print(
                        f"[helios-eie-gr-helios] All items at start={start_offset} "
                        f"already seen. Continuing."
                    )
                    continue

                # Within each page, also process in reverse so highest handle
                # IDs (newest items) come first
                for handle in reversed(new_handles):
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - crawl_start > _CRAWL_TIMEOUT:
                        break

                    try:
                        time.sleep(self._delay)

                        detail_html = _curl_get(f"{_BASE}{handle}", max_time=20)
                        if not detail_html:
                            print(
                                f"[helios-eie-gr-helios] fetch failed: {handle}, skipping"
                            )
                            continue

                        item = self._parse_detail(detail_html, handle)
                        if not item:
                            print(
                                f"[helios-eie-gr-helios] parse failed: {handle}, skipping"
                            )
                            continue

                        abstract = item.get("abstract") or ""
                        if len(abstract) < _MIN_ABSTRACT:
                            print(
                                f"[helios-eie-gr-helios] short abstract "
                                f"({len(abstract)} chars) for {handle}, skipping"
                            )
                            continue

                        self._save_paper(item)
                        saved += 1
                        print(
                            f"[helios-eie-gr-helios] saved {saved}/{limit_str}: "
                            f"{item['title'][:70]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[helios-eie-gr-helios] item {handle} failed: {exc}")
                        continue

                if (page_idx + 1) % 10 == 0:
                    print(
                        f"[helios-eie-gr-helios] page {page_idx + 1}: "
                        f"saved {saved}/{limit_str}"
                    )

        except KeyboardInterrupt:
            print(f"[helios-eie-gr-helios] Interrupted. Saved so far: {saved}")
            raise

        print(f"[helios-eie-gr-helios] Done. Total saved: {saved}")
        return saved
