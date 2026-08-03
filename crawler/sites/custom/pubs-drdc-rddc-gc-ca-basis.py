# -*- coding: utf-8 -*-
"""Crawler for DRDC PCANDID publications database (Defence Research and Development Canada).

Primary: https://pubs.drdc-rddc.gc.ca/BASIS/pcandid/www/engpub/  (Battelle BASIS system)
Fallback: OpenAlex API — used automatically when the BASIS site is unreachable.

The BASIS site is hosted on a National Defence Canada network (128.43.226.8) and may be
unreachable from non-Canadian IPs.  When a TCP connection cannot be established within
the connect timeout the crawler transparently falls back to the OpenAlex metadata API,
which indexes 6 000+ DRDC publications with full abstracts and author information.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse, urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PubsDrdcRddcGcCaBasisCrawler(BaseCrawler):
    site_id = "pubs-drdc-rddc-gc-ca-basis"
    site_name = "Custom: pubs-drdc-rddc-gc-ca-basis"
    base_url = "https://pubs.drdc-rddc.gc.ca"

    # --- BASIS list-page URL ---
    _SDF_PATH = "/BASIS/pcandid/www/engpub/SDF"
    _SDF_PARAMS = (
        ("KEYWORDS_O", "contains all"),
        ("KEYWORDS",   ""),
        ("TITLE_O",    "contains all"),
        ("TITLE",      ""),
        ("AUTHOR",     ""),
        ("AUTHOR_O",   "contains the phrase"),
        ("CA_CODE",    ""),
        ("CA_CODE_O",  "equals"),
        ("CA_NAME",    ""),
        ("CA_NAME_O",  "contains the phrase"),
        ("CA_RPNUM",   ""),
        ("CA_RPNUM_O", "equals"),
        ("REPDATE",    ""),
        ("PROJNUM",    ""),
        ("PROJNUM_O",  "includes"),
        ("FORM_C",     "and"),
        ("FORM_OB",    "Repdate"),
        ("FORM_SO",    "Descend"),
    )

    # --- OpenAlex fallback ---
    _OPENALEX_ROR = "00hgy8d33"          # Defence Research and Development Canada
    _OPENALEX_BASE = "https://api.openalex.org"
    _OPENALEX_EMAIL = "contact@financenow.co.kr"

    # --- Crawl limits ---
    _CONNECT_TIMEOUT = 5                 # TCP probe timeout (seconds)
    _CURL_TIMEOUT = 45                   # curl per-request timeout
    _MIN_ABSTRACT_CHARS = 50
    _PAGE_CAP = 200
    _MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))            # 25-minute budget

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------ #
    # Network helpers                                                       #
    # ------------------------------------------------------------------ #

    def _site_reachable(self) -> bool:
        """Return True when a TCP connection to the BASIS host succeeds."""
        try:
            sock = socket.create_connection(
                ("pubs.drdc-rddc.gc.ca", 443), timeout=self._CONNECT_TIMEOUT
            )
            sock.close()
            return True
        except (socket.timeout, OSError):
            return False

    def _curl(self, url: str, *, timeout: int = None) -> str | None:
        """Fetch *url* via curl with up to 3 retries and exponential back-off.

        Returns the decoded response body or None on persistent failure.
        """
        if timeout is None:
            timeout = self._CURL_TIMEOUT
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout", str(self._CONNECT_TIMEOUT),
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 15, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} {stderr[:120]}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl attempt {attempt+1}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------ #
    # HTML parsing helpers                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_html(raw: str | bytes) -> BeautifulSoup | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[pubs-drdc-rddc-gc-ca-basis] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value: object) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value: object) -> str:
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @classmethod
    def _tag_text(cls, tag, sep: str = " ") -> str:
        if not tag:
            return ""
        return cls._one_line(tag.get_text(sep, strip=True))

    @staticmethod
    def _parse_date(raw: object) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        m = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", text)
        if m:
            return m.group(0)
        m = re.search(r"\b(19|20)\d{2}/\d{2}/\d{2}\b", text)
        if m:
            return m.group(0).replace("/", "-")
        months_re = (
            r"\b(\d{1,2})\s+"
            r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)\s+((19|20)\d{2})\b"
        )
        m = re.search(months_re, text, re.I)
        if m:
            day, mon_str, year = m.group(1), m.group(2)[:3].lower(), m.group(3)
            mon_map = {
                "jan": "01", "feb": "02", "mar": "03", "apr": "04",
                "may": "05", "jun": "06", "jul": "07", "aug": "08",
                "sep": "09", "oct": "10", "nov": "11", "dec": "12",
            }
            return f"{year}-{mon_map.get(mon_str, '01')}-{day.zfill(2)}"
        m = re.search(r"\b(19|20)\d{2}\b", text)
        return m.group(0) if m else ""

    @staticmethod
    def _pdf_filename(url: str) -> str | None:
        """Extract original filename from a PDF URL."""
        if not url:
            return None
        path = urlparse(url).path
        tail = path.rstrip("/").split("/")[-1].split("?")[0]
        if tail.lower().endswith(".pdf"):
            return tail
        return None

    # ------------------------------------------------------------------ #
    # BASIS site parsing                                                    #
    # ------------------------------------------------------------------ #

    def _sdf_url(self) -> str:
        return (
            self.base_url
            + self._SDF_PATH
            + "?"
            + urlencode(self._SDF_PARAMS)
        )

    def _parse_list_page(self, raw: str) -> list[dict]:
        """Return [{url, external_id, post_number, title, raw_date}, ...] from the SDF page."""
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        seen_urls: set[str] = set()

        # Find all DDW links — each is a link to an individual publication detail page.
        for link in soup.find_all("a", href=re.compile(r"/BASIS/pcandid/www/engpub/DDW")):
            href = link.get("href", "")
            if not href:
                continue
            full_url = urljoin(self.base_url, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            # Extract M (match/record number) from the DDW query string.
            m = re.search(r"[?&]M=(\d+)", href, re.I)
            match_num = m.group(1) if m else None

            # Best-effort title from link text or surrounding container.
            link_text = self._one_line(link.get_text())
            title = link_text
            container = link.find_parent(["section", "article", "div", "li", "td"])
            if container:
                first_p = container.find("p")
                if first_p:
                    candidate = self._tag_text(first_p)
                    if len(candidate) > len(title):
                        title = candidate

            # Best-effort date from sibling span.text-primary or nearby text.
            raw_date = ""
            if container:
                date_span = container.find("span", class_="text-primary")
                if date_span:
                    raw_date = self._tag_text(date_span)
                else:
                    date_re = re.search(r"(19|20)\d{2}", container.get_text())
                    if date_re:
                        raw_date = date_re.group(0)

            items.append({
                "url": full_url,
                "external_id": match_num,
                "post_number": match_num,
                "title": title,
                "raw_date": raw_date,
            })

        return items

    def _parse_detail_page(self, raw: str, list_item: dict) -> dict:
        """Parse a DDW detail page for all metadata fields."""
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("could not parse detail page HTML")

        # ---- Helper: find a labeled field value (dt/dd or similar patterns) ----
        def _field(label_re: str) -> str:
            for dt in soup.find_all(["dt", "th", "label", "strong", "b"]):
                if re.search(label_re, dt.get_text(), re.I):
                    sibling = dt.find_next_sibling(["dd", "td", "p", "span"])
                    if sibling:
                        return self._clean(sibling.get_text())
            # Fallback: look for "Label: value" patterns in text nodes
            for elem in soup.find_all(string=re.compile(label_re, re.I)):
                parent = elem.find_parent()
                if parent:
                    nxt = parent.find_next_sibling()
                    if nxt:
                        text = self._clean(nxt.get_text())
                        if text:
                            return text
            return ""

        # ---- Title ----
        title = ""
        for sel in ["h1", "h2", "h3", ".title", ".pub-title"]:
            tag = soup.select_one(sel)
            if tag:
                t = self._tag_text(tag)
                if len(t) > 5:
                    title = t
                    break
        if not title:
            # Try the page <title> element as last resort.
            pg_title = soup.find("title")
            if pg_title:
                title = self._one_line(pg_title.get_text())
        if not title:
            title = list_item.get("title", "")

        # ---- Abstract ----
        abstract = ""
        abstract_candidates: list[str] = []

        # Method A: dt/dd labeled "Abstract"
        for dt in soup.find_all(["dt", "th", "label", "strong", "b"]):
            if re.search(r"abstract", dt.get_text(), re.I):
                dd = dt.find_next_sibling(["dd", "td", "p", "div"])
                if dd:
                    text = self._clean(dd.get_text())
                    if len(text) >= 30:
                        abstract_candidates.append(text)

        # Method B: any element with id/class containing "abstract"
        for tag in soup.find_all(class_=re.compile(r"abstract", re.I)):
            text = self._clean(tag.get_text())
            if len(text) >= 30:
                abstract_candidates.append(text)
        for tag in soup.find_all(id=re.compile(r"abstract", re.I)):
            text = self._clean(tag.get_text())
            if len(text) >= 30:
                abstract_candidates.append(text)

        # Method C: longest <p> on the page (last resort)
        if not abstract_candidates:
            for p in soup.find_all("p"):
                text = self._clean(p.get_text())
                if len(text) >= 100:
                    abstract_candidates.append(text)

        if abstract_candidates:
            abstract = max(abstract_candidates, key=len)

        # ---- Authors ----
        authors_text = _field(r"author")
        if not authors_text:
            # Try <meta name="citation_author">
            metas = soup.find_all("meta", attrs={"name": re.compile(r"citation_author", re.I)})
            if metas:
                authors_text = "; ".join(
                    m.get("content", "").strip() for m in metas if m.get("content")
                )

        # ---- Published date ----
        pub_date = ""
        for label_re in (r"pub(?:lication)?\s*date", r"report\s*date", r"date"):
            raw_d = _field(label_re)
            if raw_d:
                pub_date = self._parse_date(raw_d)
                if pub_date:
                    break
        if not pub_date:
            pub_date = self._parse_date(list_item.get("raw_date", ""))

        # ---- PDF URL ----
        pdf_url = ""
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if href.lower().endswith(".pdf") or re.search(r"\.pdf\b", href, re.I):
                pdf_url = urljoin(self.base_url, href)
                break
        if not pdf_url:
            # Look for PDF in meta tags
            meta_pdf = soup.find("meta", attrs={"name": re.compile(r"citation_pdf_url", re.I)})
            if meta_pdf:
                pdf_url = meta_pdf.get("content", "").strip()

        # ---- Keywords ----
        kw_text = _field(r"keyword|subject|descriptor|topic")
        keywords = None
        if kw_text:
            kw_parts = [k.strip() for k in re.split(r"[;,]+", kw_text) if k.strip()]
            keywords = ", ".join(kw_parts) if kw_parts else None

        # ---- Report number (for metadata) ----
        report_num = _field(r"report\s*num|rpnum|document\s*num")

        # ---- DOI ----
        doi = ""
        for link in soup.find_all("a", href=re.compile(r"doi\.org", re.I)):
            href = link.get("href", "")
            m = re.search(r"(10\.\d{4,9}/\S+)", href)
            if m:
                doi = m.group(1).rstrip(".,;)")
                break
        if not doi:
            doi_field = _field(r"\bDOI\b")
            m = re.search(r"(10\.\d{4,9}/\S+)", doi_field)
            if m:
                doi = m.group(1).rstrip(".,;)")

        # ---- Publisher / Department ----
        corp = _field(r"corp(?:orate)?|org(?:anization)?|department|publisher")
        publisher = corp or "Defence Research and Development Canada"

        # ---- Original filename ----
        original_filename = self._pdf_filename(pdf_url)

        metadata: dict = {"report_number": report_num} if report_num else {}
        metadata["source"] = "pubs.drdc-rddc.gc.ca"
        if doi:
            metadata["doi"] = doi

        return {
            "site_id": self.site_id,
            "external_id": list_item.get("external_id"),
            "post_number": list_item.get("post_number"),
            "url": list_item.get("url", ""),
            "title": title,
            "abstract": abstract,
            "authors": authors_text or None,
            "publisher": publisher,
            "department": None,
            "published_date": pub_date or None,
            "listed_date": pub_date or None,
            "pdf_url": pdf_url or None,
            "keywords": keywords,
            "doi": doi or None,
            "original_filename": original_filename,
            "metadata": json.dumps({k: v for k, v in metadata.items() if v}, ensure_ascii=False),
        }

    def _crawl_basis(self, limit: int | None) -> int:
        """Crawl the real DRDC BASIS site and return the count of saved records."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        page = 0

        list_url = self._sdf_url()
        print(f"[{self.site_id}] Fetching SDF list: {list_url}")
        raw = self._curl(list_url, timeout=60)
        if not raw:
            print(f"[{self.site_id}] SDF list page failed to load")
            return 0

        items = self._parse_list_page(raw)
        if not items:
            print(f"[{self.site_id}] No DDW links found on SDF page")
            return 0

        page += 1
        print(f"[{self.site_id}] page {page}: found {len(items)} detail links")

        # SDF pages are single-result-set; check if there's a "next page" link.
        soup = self._parse_html(raw)
        next_url: str | None = None
        if soup:
            for link in soup.find_all("a", href=True):
                text = self._one_line(link.get_text()).lower()
                if "next" in text or "suivant" in text:
                    next_url = urljoin(self.base_url, link.get("href", ""))
                    break

        while True:
            for idx, item in enumerate(items):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL_SECS:
                    print(f"[{self.site_id}] Wall-clock budget exceeded; stopping")
                    return saved

                url = item.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl(url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {idx+1} failed: detail fetch failed for {url}")
                        continue

                    paper = self._parse_detail_page(detail_raw, item)
                    abstract = (paper.get("abstract") or "").strip()
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {idx+1} skipped: abstract too short "
                            f"({len(abstract)} chars) for {paper.get('title', '')[:60]}"
                        )
                        continue

                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {idx+1} skipped: missing title")
                        continue

                    if not paper.get("external_id"):
                        print(f"[{self.site_id}] item {idx+1} skipped: missing external_id")
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx+1} failed: {exc}")
                    continue

            # Pagination: follow "next page" link if present and more records needed.
            if limit is not None and saved >= limit:
                break
            if not next_url:
                break
            if page >= self._PAGE_CAP:
                print(f"[{self.site_id}] Reached page cap ({self._PAGE_CAP}); stopping")
                break

            raw = self._curl(next_url, timeout=60)
            if not raw:
                break
            items = self._parse_list_page(raw)
            if not items:
                break
            page += 1
            if page % 10 == 0:
                count_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{count_str}")

            # Look for next "next" link on this new page.
            next_url = None
            soup = self._parse_html(raw)
            if soup:
                for link in soup.find_all("a", href=True):
                    text = self._one_line(link.get_text()).lower()
                    if "next" in text or "suivant" in text:
                        next_url = urljoin(self.base_url, link.get("href", ""))
                        break

        print(f"[{self.site_id}] BASIS crawl done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------ #
    # OpenAlex fallback                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _reconstruct_abstract(inv_index: dict) -> str:
        """Rebuild plain text from an OpenAlex abstract_inverted_index dict."""
        if not inv_index:
            return ""
        try:
            all_positions: list[int] = [
                pos for positions in inv_index.values() for pos in positions
            ]
            if not all_positions:
                return ""
            max_pos = max(all_positions)
            words: list[str] = [""] * (max_pos + 1)
            for word, positions in inv_index.items():
                for pos in positions:
                    words[pos] = word
            return " ".join(w for w in words if w)
        except Exception:
            return ""

    def _crawl_openalex(self, limit: int | None) -> int:
        """Fallback: retrieve DRDC publications via the OpenAlex works API."""
        print(f"[{self.site_id}] OpenAlex fallback activated (BASIS site unreachable)")

        saved = 0
        cursor = "*"
        page = 0
        seen_ids: set[str] = set()
        start_time = time.time()

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > self._MAX_WALL_SECS:
                print(f"[{self.site_id}] Wall-clock budget exceeded in OpenAlex fallback")
                break
            if page >= self._PAGE_CAP:
                print(f"[{self.site_id}] Page cap ({self._PAGE_CAP}) reached in OpenAlex fallback")
                break

            params = {
                "filter": f"institutions.ror:{self._OPENALEX_ROR}",
                "sort": "publication_date:desc",
                "per_page": "25",
                "cursor": cursor,
                "select": (
                    "id,title,abstract_inverted_index,publication_date,"
                    "authorships,primary_location,open_access,doi,keywords"
                ),
                "mailto": self._OPENALEX_EMAIL,
            }
            api_url = f"{self._OPENALEX_BASE}/works?" + urlencode(params)

            raw = self._curl(api_url, timeout=30)
            if not raw:
                print(f"[{self.site_id}] OpenAlex request failed on page {page+1}")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] OpenAlex JSON parse error: {exc}")
                break

            results = data.get("results", [])
            if not results:
                print(f"[{self.site_id}] OpenAlex: no more results at page {page+1}")
                break

            page += 1
            if page % 10 == 0:
                count_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{count_str}")

            for work in results:
                if limit is not None and saved >= limit:
                    break

                try:
                    work_id: str = work.get("id") or ""
                    # Strip the URL prefix to get a numeric ID.
                    id_match = re.search(r"W(\d+)$", work_id)
                    openalex_num = id_match.group(1) if id_match else work_id

                    if openalex_num in seen_ids:
                        continue
                    seen_ids.add(openalex_num)

                    title: str = work.get("title") or ""
                    if not title:
                        continue

                    # Reconstruct abstract from inverted index.
                    abstract = self._reconstruct_abstract(
                        work.get("abstract_inverted_index") or {}
                    )
                    if len(abstract.strip()) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipped: abstract too short "
                            f"({len(abstract.strip())} chars) — {title[:60]}"
                        )
                        continue

                    pub_date: str = work.get("publication_date") or ""

                    # Authors — OpenAlex authorships list.
                    author_names = [
                        (a.get("author") or {}).get("display_name") or ""
                        for a in (work.get("authorships") or [])
                    ]
                    authors_str = "; ".join(n for n in author_names if n) or None

                    # URLs from primary_location and open_access.
                    primary = work.get("primary_location") or {}
                    landing_url: str = primary.get("landing_page_url") or ""
                    oa_url: str = (work.get("open_access") or {}).get("oa_url") or ""
                    pdf_url_str: str = primary.get("pdf_url") or oa_url or ""

                    doi_full: str = work.get("doi") or ""
                    doi_short = ""
                    if doi_full:
                        m = re.search(r"(10\.\d{4,9}/\S+)", doi_full)
                        doi_short = m.group(1).rstrip(".,;)") if m else ""

                    record_url = (
                        landing_url
                        or (f"https://doi.org/{doi_short}" if doi_short else "")
                        or work_id
                    )

                    # Keywords from the OpenAlex keywords field.
                    kw_list = [
                        k.get("display_name", "") for k in (work.get("keywords") or [])
                        if k.get("display_name")
                    ]
                    keywords_str = ", ".join(kw_list) if kw_list else None

                    # Original filename from pdf URL.
                    original_filename = self._pdf_filename(pdf_url_str) if pdf_url_str else None

                    metadata = {
                        "source": "openalex",
                        "openalex_id": work_id,
                        "basis_site_blocked": True,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": openalex_num,
                        "post_number": openalex_num,
                        "url": record_url,
                        "title": title,
                        "abstract": abstract,
                        "authors": authors_str,
                        "publisher": "Defence Research and Development Canada",
                        "department": None,
                        "published_date": pub_date or None,
                        "listed_date": pub_date or None,
                        "pdf_url": pdf_url_str or None,
                        "keywords": keywords_str,
                        "doi": doi_short or None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {title[:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] OpenAlex item failed: {exc}")
                    continue

            # Advance cursor for next page.
            meta = data.get("meta") or {}
            next_cursor = meta.get("next_cursor")
            if not next_cursor:
                print(f"[{self.site_id}] OpenAlex: exhausted all pages")
                break
            cursor = next_cursor

        print(f"[{self.site_id}] OpenAlex fallback done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------ #
    # Main entry point                                                      #
    # ------------------------------------------------------------------ #

    def crawl(self, limit: int | None = None) -> int:
        """Crawl publications and return the count of saved records.

        Attempts the primary BASIS site first; falls back to the OpenAlex
        metadata API if the site is unreachable (e.g. from non-Canadian IPs).
        """
        if self._site_reachable():
            print(f"[{self.site_id}] BASIS site reachable — crawling directly")
            return self._crawl_basis(limit)

        print(f"[{self.site_id}] BASIS site unreachable — switching to OpenAlex fallback")
        return self._crawl_openalex(limit)
