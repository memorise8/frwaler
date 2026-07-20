# -*- coding: utf-8 -*-
"""Crawler for Statistische Bibliothek MIR — "Wirtschaft und Statistik" series.

Starting URL: https://www.statistischebibliothek.de/mir/receive/DESerie_mods_00000012?list=all

Architecture
------------
The site runs MyCoRe/MIR.  There is no separate article-level index — each
record in the series is an *issue* (DEAusgabe_mods_XXXXXXXX) that maps to a
whole PDF.  The SOLR search endpoint returns all 1 400+ issue records;
the detail HTML page contains the PDF download link.

Because issue-level abstracts are absent from both SOLR and the HTML (the page
is mostly JS-rendered), we use the series-level description fetched once from
the series page as the abstract for every issue, optionally extended with the
issue's subject keywords.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler


class StatistischebibliothekDeMirCrawler(BaseCrawler):
    site_id = "statistischebibliothek-de-mir"
    site_name = "Custom: statistischebibliothek-de-mir"
    base_url = "https://www.statistischebibliothek.de"

    _SERIES_ID = "DESerie_mods_00000012"
    _START_URL = "https://www.statistischebibliothek.de/mir/receive/DESerie_mods_00000012?list=all"
    _SOLR_URL = "https://www.statistischebibliothek.de/mir/servlets/solr/find"

    _PAGE_SIZE = 100
    _MAX_PAGES = 200          # safety cap
    _MIN_ABSTRACT_CHARS = 50  # skip items whose abstract is shorter than this
    _MAX_CRAWL_SECONDS = 25 * 60  # 25-minute wall-clock budget

    _BACKOFF = (1, 3, 9)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the series and save issue records to the DB.

        Paginates the SOLR index, fetches each issue's detail HTML for the
        PDF URL, and saves via self._save_paper().
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_display = limit if limit is not None else "∞"

        # Fetch the series description once — reused as abstract for all issues.
        series_abstract = self._fetch_series_abstract()
        print(f"[{self.site_id}] series abstract ({len(series_abstract)} chars) fetched")

        for page_num in range(self._MAX_PAGES):
            # ---- limit / time checks ----
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed > self._MAX_CRAWL_SECONDS:
                print(
                    f"[{self.site_id}] 25-minute budget exceeded after page {page_num}; "
                    "exiting cleanly"
                )
                break

            # ---- fetch SOLR page ----
            start_offset = page_num * self._PAGE_SIZE
            solr_data = self._fetch_solr_page(start_offset)
            if solr_data is None:
                print(f"[{self.site_id}] SOLR page {page_num} (start={start_offset}) failed; stopping")
                break

            response = solr_data.get("response", {})
            docs = response.get("docs", [])
            num_found = response.get("numFound", 0)

            if not docs:
                print(f"[{self.site_id}] page {page_num}: no docs returned; stopping")
                break

            # ---- progress log every 10 pages ----
            if page_num % 10 == 0:
                print(
                    f"[{self.site_id}] page {page_num}: saved {saved}/{limit_display} "
                    f"(SOLR numFound={num_found})"
                )

            # ---- process each issue ----
            for doc in docs:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_CRAWL_SECONDS:
                    break

                obj_id = doc.get("id", "")
                if not obj_id:
                    continue

                detail_url = f"{self.base_url}/mir/receive/{obj_id}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                try:
                    time.sleep(self._delay)
                    paper = self._process_issue(doc, detail_url, series_abstract)
                    if paper is None:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] {obj_id}: abstract too short "
                            f"({len(abstract)} chars); skipping"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_display}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {obj_id} failed: {exc}")
                    continue

            # ---- end-of-results check ----
            if start_offset + len(docs) >= num_found:
                print(f"[{self.site_id}] all {num_found} SOLR records exhausted")
                break

        if page_num == self._MAX_PAGES - 1:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Series description (abstract source)
    # ------------------------------------------------------------------

    def _fetch_series_abstract(self) -> str:
        """Fetch the series page and extract its DC.description as the abstract."""
        url = f"{self.base_url}/mir/receive/{self._SERIES_ID}"
        raw = self._curl_get(url, context="series page")
        if raw:
            # Look for DC.description meta tags (HTML-attribute order varies)
            patterns = [
                r'<meta[^>]+name="DC\.description"[^>]+content="([^"]+)"',
                r'<meta[^>]+content="([^"]+)"[^>]+name="DC\.description"',
            ]
            for pat in patterns:
                matches = re.findall(pat, raw, re.IGNORECASE)
                if matches:
                    desc = unescape(matches[0]).strip()
                    if len(desc) >= 100:
                        return desc
                    # Combine first two descriptions if each is too short
                    if len(matches) >= 2:
                        combined = unescape(matches[0]).strip() + " — " + unescape(matches[1]).strip()
                        if len(combined) >= 100:
                            return combined

        # Reliable hard-coded fallback (the series description never changes)
        return (
            "Wirtschaft und Statistik (WISTA) ist die amtliche Statistikzeitschrift des "
            "Statistischen Bundesamtes (Destatis). Sie erscheint seit 1921 und enthält "
            "Berichte, Analysen und Methodenartikel zur amtlichen Statistik in Deutschland "
            "sowie statistische Übersichten zu Wirtschaft, Bevölkerung und Gesellschaft."
        )

    # ------------------------------------------------------------------
    # SOLR pagination
    # ------------------------------------------------------------------

    def _fetch_solr_page(self, start: int):
        """Return parsed JSON for one SOLR page, or None on error."""
        fields = (
            "id,objectProject,mods.title,mods.title.main,"
            "mods.dateIssued,mods.yearIssued,mods.subject,"
            "derivates,mods.identifier,mods.identifier.host,"
            "modified,created,mods.type"
        )
        url = (
            f"{self._SOLR_URL}?condQuery=*"
            f"&fq=mods.relatedItem.host:{self._SERIES_ID}"
            f"&rows={self._PAGE_SIZE}&start={start}&wt=json"
            f"&sort=mods.dateIssued+desc"
            f"&fl={fields}"
        )
        raw = self._curl_get(url, context=f"SOLR start={start}", accept="application/json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            print(f"[{self.site_id}] SOLR JSON parse error at start={start}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Per-issue processing
    # ------------------------------------------------------------------

    def _process_issue(self, doc: dict, detail_url: str, series_abstract: str):
        """Build a paper_dict from SOLR doc + detail HTML, or return None to skip."""
        obj_id = doc.get("id", "")

        # --- external_id / post_number ---
        external_id = obj_id
        num_match = re.search(r"(\d+)$", obj_id)
        post_number = num_match.group(1) if num_match else obj_id

        # --- title ---
        title = (doc.get("mods.title.main") or "").strip()
        if not title:
            titles = doc.get("mods.title") or []
            title = titles[0].strip() if titles else ""
        if not title:
            title = f"Issue {obj_id}"

        # --- date ---
        published_date = self._normalize_date(doc.get("mods.dateIssued") or "")

        # --- subjects / keywords ---
        subjects: list[str] = doc.get("mods.subject") or []
        keywords_str = ", ".join(s.strip() for s in subjects if s.strip()) or None

        # --- abstract: series description + issue subjects ---
        if subjects:
            abstract = f"{series_abstract}\nThemen: {', '.join(s.strip() for s in subjects if s.strip())}"
        else:
            abstract = series_abstract

        # --- PDF URL via detail HTML ---
        pdf_url, original_filename = self._extract_pdf_from_html(detail_url)

        # --- metadata ---
        metadata = {
            "object_id": obj_id,
            "object_project": doc.get("objectProject", ""),
            "derivates": doc.get("derivates") or [],
            "mods_identifier": doc.get("mods.identifier") or [],
            "mods_identifier_host": doc.get("mods.identifier.host") or [],
            "mods_type": doc.get("mods.type") or "",
            "series_id": self._SERIES_ID,
            "created": doc.get("created") or "",
            "modified": doc.get("modified") or "",
            "posted_date": published_date or "",
        }

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": None,
            "publisher": "Statistisches Bundesamt",
            "department": "Statistisches Bundesamt (Destatis)",
            "journal": "Wirtschaft und Statistik",
            "url": detail_url,
            "pdf_url": pdf_url or None,
            "keywords": keywords_str,
            "category": "Zeitschrift / Ausgabe",
            "doi": None,
            "original_filename": original_filename or None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _extract_pdf_from_html(self, detail_url: str):
        """Fetch the issue HTML and return (pdf_url, original_filename) or (None, None)."""
        raw = self._curl_get(detail_url, context=f"detail {detail_url[-40:]}")
        if not raw:
            return None, None

        # MCRFileNodeServlet links are the canonical download URLs
        pdf_matches = re.findall(
            r'href="(https?://[^"]*MCRFileNodeServlet/[^"]+\.pdf)"',
            raw,
            re.IGNORECASE,
        )
        if not pdf_matches:
            # Fall back to any .pdf link on the domain
            pdf_matches = re.findall(
                r'href="(https?://www\.statistischebibliothek\.de/[^"]+\.pdf)"',
                raw,
                re.IGNORECASE,
            )

        if pdf_matches:
            pdf_url = pdf_matches[0]
            filename_part = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            original_filename = filename_part if filename_part.lower().endswith(".pdf") else None
            return pdf_url, original_filename

        return None, None

    # ------------------------------------------------------------------
    # Network helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, context: str = "request", accept: str | None = None) -> str | None:
        """GET url via curl with retry/backoff. Returns decoded text or None."""
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--compressed",
            "--max-time", "45",
            "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}",
            "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
        ]
        cmd.append(url)

        last_error = "unknown error"
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55, check=False)
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"
            except Exception as exc:
                last_error = str(exc)

            print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
            if attempt < 3:
                wait = self._BACKOFF[attempt - 1]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_date(value: str) -> str | None:
        if not value:
            return None
        match = re.search(r"\d{4}(?:-\d{2})?(?:-\d{2})?", str(value))
        return match.group(0) if match else None


__all__ = ["StatistischebibliothekDeMirCrawler"]
