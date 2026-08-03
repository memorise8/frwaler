# -*- coding: utf-8 -*-
"""MET Norway (Meteorologisk institutt) MET Report publications crawler.

Source : https://www.met.no/publikasjoner/met-report
Layout : Year-based HTML pages list PDF reports as plain paragraphs:
         "<NN-YYYY>: <Authors>\n<a href='…pdf'>Title</a>"
Abstract: Extracted from PDF pages 1-6 via pdfplumber (Abstract section).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
from pathlib import Path

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class MetNoPublikasjoner(BaseCrawler):
    site_id = "met-no-publikasjoner"
    site_name = "Custom: met-no-publikasjoner"
    base_url = "https://www.met.no"

    _LIST_URL = "https://www.met.no/publikasjoner/met-report"
    _PUBLISHER = "Meteorologisk institutt"
    _MAX_PDF_BYTES = 15 * 1024 * 1024  # skip abstract extraction if PDF > 15 MB

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, binary: bool = False, retries: int = 3):
        """GET via curl; returns str (or bytes if binary=True), None on failure."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
            "-A", self.USER_AGENT, url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=70)
                if result.stdout:
                    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] Empty response (attempt {attempt+1}), retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] curl error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    def _pdf_size(self, url: str) -> int | None:
        """HEAD request → content-length in bytes, or None."""
        try:
            result = subprocess.run(
                ["curl", "-skI", "--tls-max", "1.3", "--max-time", "15",
                 "-A", self.USER_AGENT, url],
                capture_output=True, text=True, timeout=20,
            )
            for line in result.stdout.splitlines():
                if line.lower().startswith("content-length:"):
                    return int(line.split(":", 1)[1].strip())
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _parse_year_page(self, html: str, year: int, page_url: str) -> list[dict]:
        """Return a list of report dicts parsed from a year HTML page."""
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error ({page_url}): {exc}")
            return []
        if not soup:
            return []

        reports = []
        for p in soup.find_all("p"):
            # Find PDF attachment links
            a_tags = [
                a for a in p.find_all("a")
                if "_/attachment" in (a.get("href") or "")
                and ".pdf" in (a.get("href") or "").lower()
            ]
            if not a_tags:
                continue

            a = a_tags[0]
            href = a.get("href", "")
            if not href:
                continue
            pdf_url = href if href.startswith("http") else f"https://www.met.no{href}"
            title = a.get_text(separator=" ", strip=True)
            if not title:
                continue

            original_filename = pdf_url.rstrip("/").split("/")[-1]

            # Extract report number + authors from paragraph text
            # Typical format: "NN-YYYY:&nbsp;Author Names\n<a>Title</a>"
            p_text = p.get_text(separator="\n")
            lines = [ln.strip() for ln in p_text.splitlines() if ln.strip()]

            report_num = ""
            authors = ""
            if lines:
                first = lines[0]
                # "01-2025: Author A, Author B"
                m = re.match(r"^(\d{1,3})[/-](\d{4})[:\s]+(.+)", first)
                if m:
                    report_num = f"{int(m.group(1)):02d}-{m.group(2)}"
                    authors = m.group(3).strip().strip(":")
                else:
                    # "01: Author A" (year implicit)
                    m2 = re.match(r"^(\d{1,3})[:\s]+(.+)", first)
                    if m2:
                        report_num = f"{int(m2.group(1)):02d}-{year}"
                        authors = m2.group(2).strip().strip(":")

            if not report_num:
                # Fallback: derive from filename (e.g. MET-report-01-2025.pdf)
                fm = re.search(r"(\d{1,3})[-_](\d{4})", original_filename)
                if fm:
                    report_num = f"{int(fm.group(1)):02d}-{fm.group(2)}"
                else:
                    report_num = f"??-{year}"

            reports.append({
                "report_num": report_num,
                "authors": authors,
                "title": title,
                "pdf_url": pdf_url,
                "external_id": f"met-report-{report_num}",
                "original_filename": original_filename,
                "url": page_url,
                "year": year,
            })

        return reports

    # ------------------------------------------------------------------
    # PDF abstract extraction
    # ------------------------------------------------------------------

    def _parse_abstract(self, text: str) -> str:
        """Extract the Abstract section from multi-page PDF text."""
        if not text:
            return ""

        # Search for "Abstract" / "ABSTRACT" / Norwegian "Sammendrag"
        for pat in (r"\bAbstract\b", r"\bABSTRACT\b", r"\bSammendrag\b"):
            mo = re.search(pat, text)
            if not mo:
                continue
            after = text[mo.end():].lstrip()
            # Cut off at the next major section heading
            end = re.search(
                r"\n\s*(?:"
                r"\d+[\.\s]+[A-Z]"       # numbered section "1. Introduction"
                r"|Contents\s*\n"
                r"|Keywords?\s*\n"
                r"|Introduction\s*\n"
                r"|Acknowledgem"
                r"|Table\s+of\s+[Cc]ontents"
                r"|References\s*\n"
                r")",
                after,
            )
            chunk = after[: end.start()] if end else after[:3000]
            cleaned = re.sub(r"\s+", " ", chunk).strip()
            if len(cleaned) >= 50:
                return cleaned

        return ""

    def _extract_abstract(self, pdf_url: str) -> str:
        """Download PDF and return abstract text, or '' if unavailable."""
        size = self._pdf_size(pdf_url)
        if size is not None and size > self._MAX_PDF_BYTES:
            print(
                f"[{self.site_id}] PDF too large "
                f"({size // 1024 // 1024} MB), skipping abstract: "
                f"{pdf_url.split('/')[-1]}"
            )
            return ""

        pdf_bytes = self._curl(pdf_url, binary=True)
        if not pdf_bytes:
            return ""

        # Try pdfplumber (preferred)
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                all_text = "\n".join(
                    (page.extract_text() or "") for page in pdf.pages[:6]
                )
            abstract = self._parse_abstract(all_text)
            if abstract:
                return abstract
        except Exception as exc:
            print(f"[{self.site_id}] pdfplumber error: {exc}")

        # Fallback: pypdf
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes), strict=False)
            all_text = "\n".join(
                (page.extract_text() or "") for page in list(reader.pages)[:6]
            )
            return self._parse_abstract(all_text)
        except Exception as exc:
            print(f"[{self.site_id}] pypdf error: {exc}")

        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        import datetime

        start_ts = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))    # 25 min wall-clock budget
        MAX_PAGES = 200        # safety cap (one per year page)

        saved = 0
        seen_pdf_urls: set[str] = set()
        page_count = 0

        # ── Step 1: fetch main page → current-year reports + year links ──
        main_html = self._curl(self._LIST_URL)
        if not main_html:
            print(f"[{self.site_id}] Failed to fetch main listing page")
            return 0

        current_year = datetime.datetime.now().year
        soup = self._make_soup(main_html)
        if not soup:
            print(f"[{self.site_id}] Failed to parse main page HTML")
            return 0

        # Build ordered year-page queue: (year, url, pre-fetched-html | None)
        year_queue: list[tuple[int, str, str | None]] = [
            (current_year, self._LIST_URL, main_html)
        ]
        seen_page_urls: set[str] = {self._LIST_URL}

        for a in soup.find_all(
            "a",
            href=re.compile(r"/publikasjoner/met-report/met-report-\d{4}$"),
        ):
            href = a.get("href", "")
            m = re.search(r"met-report-(\d{4})$", href)
            if not m:
                continue
            yr = int(m.group(1))
            url = f"https://www.met.no{href}"
            if url not in seen_page_urls:
                seen_page_urls.add(url)
                year_queue.append((yr, url, None))

        # Newest year first
        year_queue.sort(key=lambda x: x[0], reverse=True)
        print(f"[{self.site_id}] {len(year_queue)} year pages discovered")

        # ── Step 2: iterate year pages ──
        for year, page_url, page_html in year_queue:
            if limit is not None and saved >= limit:
                break
            if page_count >= MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} year-pages reached")
                break
            if time.time() - start_ts > MAX_WALL:
                print(f"[{self.site_id}] Wall-clock budget reached, exiting cleanly")
                break

            page_count += 1
            if page_count % 10 == 0:
                lbl = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page_count}: saved {saved}/{lbl}")

            if page_html is None:
                time.sleep(self._delay)
                page_html = self._curl(page_url)
                if not page_html:
                    print(f"[{self.site_id}] Failed to fetch: {page_url}")
                    continue

            reports = self._parse_year_page(page_html, year, page_url)
            if not reports:
                print(f"[{self.site_id}] Year {year}: no reports found")
                continue

            print(f"[{self.site_id}] Year {year}: {len(reports)} reports")

            for report in reports:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_ts > MAX_WALL:
                    break

                try:
                    pdf_url = report["pdf_url"]
                    if pdf_url in seen_pdf_urls:
                        continue
                    seen_pdf_urls.add(pdf_url)

                    time.sleep(self._delay)
                    abstract = self._extract_abstract(pdf_url)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Abstract too short "
                            f"({len(abstract)}c), skipping: "
                            f"{report['title'][:50]}"
                        )
                        continue

                    published_date = f"{report['year']}-01-01"

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": report["external_id"],
                        "post_number": report["report_num"],
                        "title": report["title"],
                        "abstract": abstract,
                        "authors": report["authors"],
                        "publisher": self._PUBLISHER,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": report["url"],
                        "pdf_url": pdf_url,
                        "original_filename": report["original_filename"],
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "MET report",
                        "metadata": json.dumps(
                            {
                                "posted_date": published_date,
                                "originalFilename": report["original_filename"],
                                "report_number": report["report_num"],
                                "year": report["year"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lbl = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{lbl}: {report['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item {report.get('external_id', '?')} "
                        f"failed: {exc}"
                    )
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
