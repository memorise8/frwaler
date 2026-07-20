# -*- coding: utf-8 -*-
"""NETL DOE FECM External R&D Final Technical Reports crawler.

Starting URL: https://netl.doe.gov/FECM-External-R-and-D-Final-Technical-Reports
(301 → https://netl.doe.gov/HGEO-External-R-and-D-Final-Technical-Reports)

The list page embeds an iframe:
  https://netl.doe.gov/projects/project-Final-Report-List.aspx
All ~492 records are served as inline HTML rows in a DataTables <table>.
No pagination — one page contains everything.

OSTI API (https://www.osti.gov/api/v1/records/{osti_id}) enriches each row
with authors, subjects (keywords), doi, and richer description.
"""

import json
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # absolute import — no package ctx

_LIST_URL = "https://netl.doe.gov/projects/project-Final-Report-List.aspx"
_OSTI_API = "https://www.osti.gov/api/v1/records/{osti_id}"
_PAGE_CAP = 200   # safety guard — this site is a single page, so rarely hit
_TIME_BUDGET = 25 * 60  # 25 minutes in seconds


class NETLFECMCrawler(BaseCrawler):
    site_id = "netl-doe-gov-fecm-external-r-and-"
    site_name = "Custom: netl-doe-gov-fecm-external-r-and-"
    base_url = "https://netl.doe.gov"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, follow: bool = True) -> str | None:
        """GET via curl; returns response body (UTF-8, errors=replace) or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
        ]
        if follow:
            cmd.append("-L")
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    return text
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] Empty response for {url}, retry in {wait}s...")
                time.sleep(wait)
            except Exception as exc:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _bs(raw: str):
        """Build a BeautifulSoup with html5lib → lxml → html.parser fallback."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _strip_tags(html_text: str) -> str:
        """Strip HTML tags and normalize whitespace."""
        text = re.sub(r"<[^>]+>", " ", html_text or "")
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Convert MM/DD/YYYY → YYYY-MM-DD; pass through ISO dates."""
        raw = raw.strip()
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
        if m:
            return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
        return raw

    def _parse_table_rows(self, html: str) -> list[dict]:
        """Parse the #projects DataTable and return list of row dicts."""
        soup = self._bs(html)
        if soup is None:
            print(f"[{self.site_id}] BeautifulSoup failed — trying regex fallback")
            return self._parse_table_rows_regex(html)

        table = soup.find("table", {"id": "projects"})
        if not table:
            print(f"[{self.site_id}] #projects table not found")
            return []

        rows = []
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 7:
                continue
            try:
                award_no = tds[0].get_text(" ", strip=True)
                performer = tds[1].get_text(" ", strip=True)
                state = tds[2].get_text(" ", strip=True)

                title_td = tds[3]
                title_link = title_td.find("a")
                title = title_link.get_text(" ", strip=True) if title_link else title_td.get_text(" ", strip=True)
                osti_href = title_link["href"] if title_link and title_link.get("href") else ""

                date_raw = tds[4].get_text(" ", strip=True)
                category = tds[5].get_text(" ", strip=True)
                abstract = tds[6].get_text(" ", strip=True)
                osti_url = tds[7].get_text(" ", strip=True).strip() if len(tds) > 7 else osti_href

                # Normalise the OSTI URL
                if not osti_url and osti_href:
                    osti_url = osti_href
                osti_url = osti_url.strip()

                # Extract numeric OSTI ID from URL
                osti_id_m = re.search(r"/(\d+)$", osti_url)
                osti_id = osti_id_m.group(1) if osti_id_m else ""

                rows.append({
                    "award_no": award_no,
                    "performer": performer,
                    "state": state,
                    "title": title,
                    "date_raw": date_raw,
                    "category": category,
                    "abstract": abstract,
                    "osti_url": osti_url,
                    "osti_id": osti_id,
                })
            except Exception as exc:
                print(f"[{self.site_id}] Row parse error: {exc}")
                continue
        return rows

    def _parse_table_rows_regex(self, html: str) -> list[dict]:
        """Regex-based fallback table parser."""
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
            if len(tds) < 7:
                continue
            try:
                award_no = self._strip_tags(tds[0])
                performer = self._strip_tags(tds[1])
                state = self._strip_tags(tds[2])
                osti_href_m = re.search(r"href=['\"]([^'\"]+)['\"]", tds[3])
                osti_href = osti_href_m.group(1) if osti_href_m else ""
                title_m = re.search(r">([^<]+)<", tds[3])
                title = self._strip_tags(tds[3]) if not title_m else title_m.group(1).strip()
                date_raw = self._strip_tags(tds[4])
                category = self._strip_tags(tds[5])
                abstract = self._strip_tags(tds[6])
                osti_url = self._strip_tags(tds[7]).strip() if len(tds) > 7 else osti_href
                if not osti_url:
                    osti_url = osti_href
                osti_id_m = re.search(r"/(\d+)$", osti_url)
                osti_id = osti_id_m.group(1) if osti_id_m else ""
                rows.append({
                    "award_no": award_no, "performer": performer, "state": state,
                    "title": title, "date_raw": date_raw, "category": category,
                    "abstract": abstract, "osti_url": osti_url, "osti_id": osti_id,
                })
            except Exception:
                continue
        return rows

    # ------------------------------------------------------------------
    # OSTI enrichment
    # ------------------------------------------------------------------

    def _fetch_osti(self, osti_id: str) -> dict:
        """Fetch OSTI API record; returns dict of enriched fields (best-effort)."""
        if not osti_id:
            return {}
        url = _OSTI_API.format(osti_id=osti_id)
        raw = self._curl_get(url)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                data = data[0]
            if not isinstance(data, dict):
                return {}

            # Authors: strip affiliation info in brackets
            raw_authors = data.get("authors") or []
            authors = []
            for a in raw_authors:
                name = re.sub(r"\s*\[.*?\]", "", a).strip()
                name = re.sub(r"\s*\(ORCID:\w+\)", "", name).strip()
                if name:
                    authors.append(name)

            subjects = data.get("subjects") or []
            keywords = [re.sub(r"^\d+\s+", "", s).strip() for s in subjects]

            doi = data.get("doi") or ""
            description = data.get("description") or ""
            pub_date_raw = data.get("publication_date") or ""
            pub_date = pub_date_raw[:10] if pub_date_raw else ""  # ISO YYYY-MM-DD

            # Try purl for pdf_url (HEAD only, follow redirect)
            pdf_url = None
            purl = f"https://www.osti.gov/servlets/purl/{osti_id}"
            try:
                purl_cmd = [
                    "curl", "-skL", "--tls-max", "1.3", "--max-time", "15",
                    "-I", "-H", f"User-Agent: {self.USER_AGENT}", purl,
                ]
                pr = subprocess.run(purl_cmd, capture_output=True, text=True, timeout=20)
                headers = pr.stdout.lower()
                if "content-type: application/pdf" in headers or ".pdf" in pr.stdout:
                    # Follow redirect to get final URL
                    loc_m = re.search(r"location:\s*(https?://[^\r\n]+)", pr.stdout, re.IGNORECASE)
                    if loc_m:
                        pdf_url = loc_m.group(1).strip()
                    elif "200" in pr.stdout[:50]:
                        pdf_url = purl
            except Exception:
                pass

            return {
                "authors": authors,
                "keywords": keywords,
                "doi": doi,
                "description": description,
                "pub_date": pub_date,
                "pdf_url": pdf_url,
                "report_number": data.get("report_number") or "",
                "product_type": data.get("product_type") or "",
                "country": data.get("country_publication") or "",
            }
        except Exception as exc:
            print(f"[{self.site_id}] OSTI parse error for {osti_id}: {exc}")
            return {}

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Fetch all records from the inline HTML table; enrich via OSTI API.

        Parameters
        ----------
        limit : int | None
            Maximum number of papers to save. None = unlimited.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"

        # ── Step 1: fetch the single list page ──────────────────────────
        print(f"[{self.site_id}] Fetching list page: {_LIST_URL}")
        raw = self._curl_get(_LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch list page. Aborting.")
            return 0

        # ── Step 2: parse all rows ────────────────────────────────────
        all_rows = self._parse_table_rows(raw)
        print(f"[{self.site_id}] Parsed {len(all_rows)} rows from table")
        if not all_rows:
            print(f"[{self.site_id}] No rows found. Aborting.")
            return 0

        # ── Step 3: process rows up to limit ─────────────────────────
        for idx, row in enumerate(all_rows):
            if limit is not None and saved >= limit:
                break

            # Time budget guard
            elapsed = time.time() - start_time
            if elapsed > _TIME_BUDGET:
                print(f"[{self.site_id}] 25-minute time budget reached. Stopping.")
                break

            osti_url = row.get("osti_url") or ""
            title = row.get("title") or ""

            # URL-dedup
            dedup_key = osti_url or row.get("award_no") or title
            if dedup_key and dedup_key in seen_urls:
                continue
            if dedup_key:
                seen_urls.add(dedup_key)

            try:
                inline_abstract = row.get("abstract") or ""
                osti_id = row.get("osti_id") or ""

                # Progress log every 10 items
                if idx > 0 and idx % 10 == 0:
                    print(f"[{self.site_id}] page 1: saved {saved}/{limit_label} (row {idx}/{len(all_rows)})")

                # OSTI enrichment (best-effort, ~1s per item)
                osti_data: dict = {}
                if osti_id:
                    time.sleep(self._delay)
                    osti_data = self._fetch_osti(osti_id)

                # Abstract: prefer OSTI description (richer), fall back to inline
                abstract = osti_data.get("description") or inline_abstract
                abstract = abstract.strip()

                if len(abstract) < 50:
                    print(f"[{self.site_id}] Abstract <50 chars, skipping: {title[:60]}")
                    continue

                # Dates
                date_raw = row.get("date_raw") or ""
                completion_date = self._parse_date(date_raw) if date_raw else ""
                pub_date = osti_data.get("pub_date") or completion_date

                # Authors
                authors_list = osti_data.get("authors") or []
                authors_str = "; ".join(authors_list) if authors_list else ""

                # Keywords
                kw_list = osti_data.get("keywords") or []
                keywords_str = ", ".join(kw_list) if kw_list else ""

                # PDF
                pdf_url = osti_data.get("pdf_url") or None

                # original_filename from pdf_url
                original_filename = None
                if pdf_url:
                    fname_m = re.search(r"/([^/?#]+\.pdf)", pdf_url, re.IGNORECASE)
                    if fname_m:
                        original_filename = fname_m.group(1)

                award_no = row.get("award_no") or ""
                performer = row.get("performer") or ""
                state = row.get("state") or ""

                paper = {
                    "site_id": self.site_id,
                    "external_id": award_no or osti_id or None,
                    "post_number": award_no or osti_id or None,
                    "title": title,
                    "abstract": abstract,
                    "published_date": pub_date,
                    "listed_date": completion_date,
                    "authors": authors_str or None,
                    "publisher": performer or None,
                    "department": state or None,
                    "journal": None,
                    "url": osti_url or None,
                    "pdf_url": pdf_url,
                    "keywords": keywords_str or None,
                    "category": row.get("category") or None,
                    "doi": osti_data.get("doi") or None,
                    "original_filename": original_filename,
                    "metadata": json.dumps({
                        "award_number": award_no,
                        "performer_state": state,
                        "osti_id": osti_id,
                        "report_number": osti_data.get("report_number") or "",
                        "product_type": osti_data.get("product_type") or "",
                        "country_publication": osti_data.get("country") or "",
                        "completion_date": date_raw,
                        "posted_date": date_raw,
                        "originalFilename": original_filename or "",
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_label}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
