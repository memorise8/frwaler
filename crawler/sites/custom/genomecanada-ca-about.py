# -*- coding: utf-8 -*-
"""Genome Canada Publications crawler.

Target: https://genomecanada.ca/about/publications/
Site structure: Static WordPress/Elementor page listing ~50 PDF publications.

Sections:
  - Annual Reports   (Elementor icon-list archive format, 2008-2025)
  - Corporate Plans  (Elementor icon-list archive format, 2009-2026)
  - Other Reports, Policy Briefs, Pre-budget, GE3LS (individual card format)

No API / no pagination — single-page scrape of the publications index.
Abstract: pdftotext on each downloaded PDF (first 5 pages).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "genomecanada-ca-about"
_BASE_URL = "https://genomecanada.ca"
_INDEX_URL = "https://genomecanada.ca/about/publications/"
_ABSTRACT_MIN_CHARS = 50  # skip and log if shorter; pdftotext gives >>100 for real PDFs
_SAFETY_CAP_PAGES = 200   # not used (single page), kept for interface compliance
_BS4_PARSERS = ["html5lib", "lxml", "html.parser"]


# ---------------------------------------------------------------------------
# Helpers (module-level so they are importable without instantiation)
# ---------------------------------------------------------------------------

def _make_soup(html: str) -> BeautifulSoup:
    for parser in _BS4_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _fn_to_title(filename: str) -> str:
    name = re.sub(r"\.pdf$", "", filename, flags=re.I)
    return _clean(name.replace("_", " ").replace("-", " "))


def _upload_date(url: str) -> str:
    """YYYY-MM-01 from WP uploads URL, e.g. /uploads/2024/07/ → 2024-07-01."""
    m = re.search(r"/uploads/(\d{4})/(\d{2})/", url)
    return f"{m.group(1)}-{m.group(2)}-01" if m else ""


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class GenomeCanadaAboutCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: genomecanada-ca-about"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url: str, binary: bool = False):
        """Fetch URL via curl with 3-attempt exponential backoff.

        Returns bytes if binary=True, str otherwise, or None on failure.
        """
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*;q=0.8",
            url,
        ]
        delays = [1, 3, 9]
        for attempt, wait in enumerate(delays, 1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=65, check=False)
                if result.stdout:
                    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error ({url[:60]}): {exc}")
            if attempt < len(delays):
                print(f"[{_SITE_ID}] Retry {attempt}/{len(delays)-1} in {wait}s for {url[:60]}")
                time.sleep(wait)
        print(f"[{_SITE_ID}] Failed after 3 attempts: {url[:60]}")
        return None

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _pdf_to_text(self, pdf_bytes: bytes, pages: int = 5) -> str:
        """Write bytes to a temp file, run pdftotext, return clean text."""
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(pdf_bytes)
                tmp = f.name
            result = subprocess.run(
                ["pdftotext", "-l", str(pages), tmp, "-"],
                capture_output=True, timeout=30, check=False,
            )
            return _clean(result.stdout.decode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"[{_SITE_ID}] pdftotext error: {exc}")
            return ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_page(self, html: str) -> list[dict]:
        """Parse the publications index and return list of publication dicts.

        Returns:
            List of dicts with keys: pdf_url, title, category, upload_date.
        """
        soup = _make_soup(html)
        results: list[dict] = []
        seen: set[str] = set()

        # ------------------------------------------------------------------
        # Pass 1: Elementor icon-list items (archive Annual Reports + Plans)
        #
        # Structure per item:
        #   <li class="elementor-icon-list-item">
        #     <a href="PDF_URL" target="_blank">
        #       <span class="elementor-icon-list-icon">…SVG…</span>
        #       <span class="elementor-icon-list-text">2023–2024</span>
        #     </a>
        #   </li>
        # ------------------------------------------------------------------
        for ul in soup.find_all("ul", class_="elementor-icon-list-items"):
            for li in ul.find_all("li", class_="elementor-icon-list-item"):
                a_tag = li.find("a", href=re.compile(r"\.pdf$", re.I))
                if not a_tag:
                    continue
                pdf_url = a_tag.get("href", "")
                if not pdf_url or pdf_url in seen:
                    continue
                seen.add(pdf_url)

                filename = pdf_url.split("/")[-1]
                label_span = a_tag.find("span", class_="elementor-icon-list-text")
                label = _clean(label_span.get_text()) if label_span else ""

                fn_lower = filename.lower()
                if "annual" in fn_lower:
                    category = "Annual Report"
                    title = (f"Genome Canada Annual Report {label}"
                             if label else _fn_to_title(filename))
                elif "corporate" in fn_lower or "addendum" in fn_lower:
                    category = "Corporate Plan"
                    title = (f"Genome Canada Corporate Plan {label}"
                             if label else _fn_to_title(filename))
                else:
                    category = "Publication"
                    title = label or _fn_to_title(filename)

                results.append({
                    "pdf_url": pdf_url,
                    "title": title,
                    "category": category,
                    "upload_date": _upload_date(pdf_url),
                })

        # ------------------------------------------------------------------
        # Pass 2: remaining PDF anchors (featured latest cards + other reports)
        #
        # For these, the title text appears in the same Elementor column as
        # the download link.  Walk up the DOM to find the column container,
        # then extract and clean its text.
        # ------------------------------------------------------------------
        _BOILERPLATE = [
            "Download Report", "Download Brief", "Download",
            "Archived annual reports from earlier years are available upon request.",
            "Archived corporate plans from earlier years are available upon request.",
        ]

        for a_tag in soup.find_all("a", href=re.compile(r"\.pdf$", re.I)):
            pdf_url = a_tag.get("href", "")
            if not pdf_url or pdf_url in seen:
                continue
            seen.add(pdf_url)

            filename = pdf_url.split("/")[-1]
            title = ""

            # Walk up ancestors to find the Elementor column/section boundary
            node = a_tag.parent
            for _ in range(20):
                if node is None:
                    break
                classes = " ".join(node.get("class", []))
                if "elementor-column" in classes or "elementor-section" in classes:
                    heading = node.find(re.compile(r"^h[1-5]$"))
                    if heading:
                        title = _clean(heading.get_text())
                    else:
                        raw = _clean(node.get_text(separator=" "))
                        for noise in _BOILERPLATE:
                            raw = raw.replace(noise, " ")
                        raw = _clean(raw)
                        title = raw[:200].strip()
                    break
                node = node.parent

            if not title:
                title = _fn_to_title(filename)

            fn_lower = filename.lower()
            title_lower = title.lower()
            if "annual" in fn_lower:
                category = "Annual Report"
            elif "corporate" in fn_lower:
                category = "Corporate Plan"
            elif any(w in title_lower for w in ["policy", "submission", "brief"]):
                category = "Policy Brief"
            elif any(w in fn_lower for w in ["budget", "fina", "prebudget", "pre-budget"]):
                category = "Pre-budget Submission"
            elif "playbook" in fn_lower:
                category = "Internal Report"
            else:
                category = "Report"

            results.append({
                "pdf_url": pdf_url,
                "title": title,
                "category": category,
                "upload_date": _upload_date(pdf_url),
            })

        return results

    # ------------------------------------------------------------------
    # Main crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Genome Canada publications page and save records to DB.

        Parameters
        ----------
        limit : int or None
            Maximum number of publications to save.  None = unlimited.

        Returns
        -------
        int
            Number of publications saved.
        """
        start_time = time.time()
        max_secs = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        # ---- Fetch index page ----
        print(f"[{_SITE_ID}] Fetching index: {_INDEX_URL}")
        html = self._curl(_INDEX_URL)
        if not html:
            print(f"[{_SITE_ID}] Failed to fetch index. Aborting.")
            return 0

        publications = self._parse_page(html)
        total = len(publications)
        print(f"[{_SITE_ID}] Parsed {total} unique publications.")

        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = str(limit) if limit is not None else "∞"
        p = 1  # single logical "page" (no pagination on this site)

        for i, pub in enumerate(publications, 1):
            # Respect limit
            if limit is not None and saved >= limit:
                break

            # Wall-clock safety
            if time.time() - start_time > max_secs:
                print(f"[{_SITE_ID}] 25-minute budget reached at item {i}. Stopping.")
                break

            pdf_url = pub["pdf_url"]
            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)

            try:
                title = pub["title"]
                category = pub["category"]
                upload_date = pub["upload_date"]
                filename = pdf_url.split("/")[-1]

                print(f"[{_SITE_ID}] [{i}/{total}] Downloading: {filename[:55]}")

                pdf_bytes = self._curl(pdf_url, binary=True)
                if not pdf_bytes:
                    print(f"[{_SITE_ID}] Download failed, skipping: {filename}")
                    continue

                abstract = self._pdf_to_text(pdf_bytes)

                if len(abstract) < _ABSTRACT_MIN_CHARS:
                    print(
                        f"[{_SITE_ID}] Abstract too short "
                        f"({len(abstract)} chars), skipping: {filename}"
                    )
                    continue

                # Date: upload URL gives YYYY-MM; fallback to year from filename
                published_date = upload_date
                if not published_date:
                    yr = re.search(r"(20\d{2})", filename)
                    published_date = f"{yr.group(1)}-01-01" if yr else ""

                # post_number: YYYYMM for incremental-collection comparisons
                pm = re.search(r"/uploads/(\d{4})/(\d{2})/", pdf_url)
                post_number = f"{pm.group(1)}{pm.group(2)}" if pm else None

                external_id = re.sub(r"\.pdf$", "", filename, flags=re.I)

                paper = {
                    "id": None,
                    "site_id": _SITE_ID,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract[:8000],
                    "category": category,
                    "published_date": published_date,
                    "listed_date": upload_date,
                    "url": _INDEX_URL,
                    "pdf_url": pdf_url,
                    "original_filename": filename,
                    "authors": "",
                    "publisher": "Genome Canada",
                    "journal": "",
                    "keywords": "",
                    "doi": "",
                    "department": "",
                    "metadata": json.dumps({
                        "posted_date": upload_date,
                        "originalFilename": filename,
                        "source_page": _INDEX_URL,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] Item {i} failed: {exc}")
                continue

            if i % 10 == 0:
                print(f"[{_SITE_ID}] page {p}: saved {saved}/{limit_or_inf}")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
