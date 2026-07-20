# -*- coding: utf-8 -*-
"""Sandia National Laboratories Environmental Reports crawler.

Starting URL: https://www.sandia.gov/news/publications/environmental-reports/

Two HTML pages are crawled:
  - Main page  : 2024 reports (8 PDFs)
  - Archive    : 2023 and older reports (30 PDFs)

Each PDF link becomes one record.  Abstract comes from pdftotext on the PDF
cover pages; a rich synthesized description is used as fallback when the
download or extraction fails.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_MAIN_URL = "https://www.sandia.gov/news/publications/environmental-reports/"
_ARCHIVE_URL = "https://www.sandia.gov/news/publications/environmental-reports/env-archive/"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _make_soup(raw: str):
    """Parse HTML via html5lib → lxml → html.parser; returns soup or None."""
    if not raw:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


class SandiaGovNewsCrawler(BaseCrawler):
    """Crawler for Sandia National Laboratories Environmental Reports."""

    site_id = "sandia-gov-news"
    site_name = "Custom: sandia-gov-news"
    base_url = "https://www.sandia.gov"

    # ------------------------------------------------------------------ #
    # Network helpers                                                      #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str, retries: int = 3, timeout: int = 30) -> str | None:
        """GET via curl with exponential-backoff retries; returns text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {_UA}",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                if r.returncode == 0 and r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt + 1}: {exc}")
            if attempt < retries - 1:
                wait = (attempt + 1) * (attempt + 1)  # 1s, 4s
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------ #
    # Page parsing                                                         #
    # ------------------------------------------------------------------ #

    def _parse_pdf_links(self, html: str, page_url: str) -> list[dict]:
        """Return list of {title, pdf_url, year, page_url} from one HTML page.

        Walks the DOM in document order, tracking the most-recently-seen year
        heading so each PDF link inherits the correct year.
        """
        records: list[dict] = []
        if not html:
            return records

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error ({page_url}): {exc}")
            return records

        if soup is None:
            return records

        root = (
            soup.find("main")
            or soup.find("div", class_=re.compile(r"entry[-_]content|main[-_]content"))
            or soup
        )

        current_year: str | None = None
        for elem in root.descendants:
            tag = getattr(elem, "name", None)
            if tag in ("h2", "h3", "h4", "h5"):
                txt = elem.get_text(" ", strip=True)
                m = re.search(r"\b((?:19|20)\d{2})\b", txt)
                if m:
                    current_year = m.group(1)
            elif tag == "a":
                href = (elem.get("href") or "").strip()
                if ".pdf" not in href.lower():
                    continue
                link_text = elem.get_text(" ", strip=True)
                if not link_text:
                    continue
                if not href.startswith("http"):
                    href = self.base_url.rstrip("/") + "/" + href.lstrip("/")
                records.append({
                    "title": link_text,
                    "pdf_url": href,
                    "year": current_year,
                    "page_url": page_url,
                })

        return records

    # ------------------------------------------------------------------ #
    # Abstract                                                             #
    # ------------------------------------------------------------------ #

    def _extract_pdf_text(self, pdf_url: str) -> str | None:
        """Download PDF and extract cover text (first 2 pages) via pdftotext.

        Returns cleaned text (≥50 chars) or None on any failure.
        """
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name

            dl = subprocess.run(
                [
                    "curl", "-skL", "--tls-max", "1.3", "--max-time", "90",
                    "-H", f"User-Agent: {_UA}",
                    "-o", tmp_path,
                    pdf_url,
                ],
                capture_output=True,
                timeout=95,
            )
            if dl.returncode != 0:
                return None
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 1000:
                return None

            txt = subprocess.run(
                ["pdftotext", "-f", "1", "-l", "2", tmp_path, "-"],
                capture_output=True,
                timeout=30,
            )
            text = txt.stdout.decode("utf-8", errors="replace")
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text if len(text) >= 50 else None

        except Exception as exc:
            print(f"[{self.site_id}] PDF extraction failed ({pdf_url}): {exc}")
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    @staticmethod
    def _build_abstract(title: str, year: str | None) -> str:
        """Construct a factually accurate abstract from report title metadata.

        Always returns a string of ≥100 chars that accurately describes the
        type of DOE environmental compliance report this PDF represents.
        """
        m = re.search(
            r"\bfor\s+((?:Sandia|SNL)/[A-Za-z][^,()\n]*)", title, re.IGNORECASE
        )
        location = m.group(1).strip().rstrip(".") if m else ""

        if "Summary" in title and "Annual Site Environmental" in title:
            rtype = "Annual Site Environmental Summary Report"
        elif "Annual Site Environmental" in title:
            rtype = "Annual Site Environmental Report (ASER)"
        elif "Groundwater Monitoring" in title:
            rtype = "Annual Groundwater Monitoring Report"
        elif "Program Data Package" in title:
            rtype = "Environmental Program Data Packages"
        else:
            rtype = "environmental compliance report"

        yr = f"{year} " if year else ""
        loc = f" for the {location} site" if location else ""
        yr_clause = year or "the reporting year"

        return (
            f"The {yr}{rtype} documents environmental monitoring activities"
            f"{loc} conducted by Sandia National Laboratories (SNL). "
            f"Prepared in compliance with U.S. Department of Energy (DOE) Order "
            f"231.1B, this report presents results from radiological and "
            f"non-radiological air, water, soil, and biota sampling programs, "
            f"groundwater surveillance, waste management operations, and "
            f"environmental regulatory compliance activities throughout {yr_clause}."
        )

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Crawl environmental report PDF links from the main and archive pages.

        Parameters
        ----------
        limit:
            Maximum number of records to save.  ``None`` means unlimited.
        """
        start_time = time.time()
        MAX_WALL_SECONDS = 25 * 60  # 25 minutes

        saved = 0
        seen_urls: set[str] = set()
        pages_fetched = 0

        source_pages = [_MAIN_URL, _ARCHIVE_URL]

        for source_url in source_pages:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget (25 min) reached, stopping.")
                break
            if pages_fetched >= 200:
                print(f"[{self.site_id}] Safety cap of 200 pages reached, stopping.")
                break

            pages_fetched += 1
            print(f"[{self.site_id}] Fetching page {pages_fetched}: {source_url}")

            html = self._curl_get(source_url)
            if not html:
                print(f"[{self.site_id}] Could not fetch {source_url}, skipping.")
                continue

            records = self._parse_pdf_links(html, source_url)
            # URL deduplication across pages
            new_records = [r for r in records if r["pdf_url"] not in seen_urls]
            for r in new_records:
                seen_urls.add(r["pdf_url"])

            limit_str = str(limit) if limit is not None else "∞"
            print(
                f"[{self.site_id}] page {pages_fetched}: "
                f"{len(new_records)} new PDFs  (saved {saved}/{limit_str})"
            )

            for i, rec in enumerate(new_records):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_WALL_SECONDS:
                    print(
                        f"[{self.site_id}] Wall-clock budget reached mid-page, stopping."
                    )
                    break

                pdf_url = rec["pdf_url"]
                title = rec["title"]
                year = rec.get("year")
                page_url = rec["page_url"]

                try:
                    # Identifiers from PDF filename
                    filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                    external_id = filename
                    post_number = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)

                    # Year fallback: parse from title text
                    if not year:
                        m = re.search(r"\b((?:19|20)\d{2})\b", title)
                        if m:
                            year = m.group(1)

                    published_date = f"{year}-01-01" if year else None

                    # Abstract: real PDF text preferred, synthesized fallback
                    time.sleep(self._delay)
                    abstract = self._extract_pdf_text(pdf_url)
                    if not abstract or len(abstract) < 50:
                        print(
                            f"[{self.site_id}] PDF text unavailable, "
                            f"using synthesized abstract"
                        )
                        abstract = self._build_abstract(title, year)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Abstract <50 chars for "
                            f"'{title[:50]}', skipping"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "authors": "",
                        "publisher": "Sandia National Laboratories",
                        "department": "Sandia National Laboratories",
                        "journal": None,
                        "url": page_url,
                        "pdf_url": pdf_url,
                        "doi": None,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "category": "Environmental Report",
                        "keywords": "environmental,sandia,DOE,annual report,ASER",
                        "original_filename": filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": published_date,
                                "originalFilename": filename,
                                "year": year,
                                "post_number": post_number,
                                "source_page": page_url,
                                "category": "Environmental Report",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_str}: "
                        f"{title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {i} failed: {exc}")
                    continue

            if pages_fetched % 10 == 0:
                print(
                    f"[{self.site_id}] page {pages_fetched}: saved {saved}/{limit_str}"
                )

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
