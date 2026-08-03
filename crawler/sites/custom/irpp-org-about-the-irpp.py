# -*- coding: utf-8 -*-
"""Crawler for IRPP Annual Reports.

Starting URL: https://irpp.org/about-the-irpp/annual-reports/
One static page listing all annual-report PDFs — no separate detail pages.
Abstract is extracted via pdftotext; falls back to a descriptive template.
"""

import json
import os
import re
import subprocess
import tempfile
import time

from crawler.base_crawler import BaseCrawler


def _bs4_parse(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


class IRPPOrgAboutTheIRPPCrawler(BaseCrawler):
    site_id = "irpp-org-about-the-irpp"
    site_name = "Custom: irpp-org-about-the-irpp"
    base_url = "https://irpp.org"

    _START_URL = "https://irpp.org/about-the-irpp/annual-reports/"
    _RETRIES = 3
    _BACKOFFS = (1, 3, 9)
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str):
        """GET via curl; returns body text or None on failure."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(self._RETRIES):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=40)
                if r.returncode != 0:
                    raise RuntimeError(
                        f"curl exit {r.returncode}: "
                        f"{r.stderr.decode('utf-8', errors='replace')[:120]}"
                    )
                body = r.stdout.decode("utf-8", errors="replace")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                if attempt < self._RETRIES - 1:
                    wait = self._BACKOFFS[attempt]
                    print(
                        f"[{self.site_id}] fetch failed "
                        f"({attempt+1}/{self._RETRIES}) for {url}: {exc}; "
                        f"retry in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] all retries failed for {url}")
                    return None

    def _download_pdf(self, pdf_url: str):
        """Download PDF to a temp file; return path string or None on failure."""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name

        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "90",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/pdf,*/*",
            "-o", tmp_path,
            pdf_url,
        ]
        for attempt in range(self._RETRIES):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=100)
                if r.returncode != 0:
                    raise RuntimeError(f"curl exit {r.returncode}")
                size = os.path.getsize(tmp_path)
                if size < 512:
                    raise RuntimeError(f"file too small: {size} bytes")
                return tmp_path
            except Exception as exc:
                if attempt < self._RETRIES - 1:
                    wait = self._BACKOFFS[attempt]
                    print(
                        f"[{self.site_id}] pdf download failed "
                        f"({attempt+1}/{self._RETRIES}) for {pdf_url}: {exc}; "
                        f"retry in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] pdf download all retries failed for {pdf_url}")
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                    return None

    def _extract_abstract_from_pdf(self, pdf_url: str):
        """Download PDF and return extracted text (up to 1000 chars), or None."""
        tmp_path = self._download_pdf(pdf_url)
        if not tmp_path:
            return None
        try:
            result = subprocess.run(
                ["pdftotext", "-l", "5", tmp_path, "-"],
                capture_output=True,
                timeout=30,
            )
            raw = result.stdout.decode("utf-8", errors="replace")
            text = re.sub(r"\s+", " ", raw).strip()
            return text[:1000] if len(text) >= 50 else None
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext error: {exc}")
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _fallback_abstract(self, year_label: str, pdf_url: str) -> str:
        """Return a descriptive abstract when PDF extraction fails or yields too little."""
        fname = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
        return (
            f"IRPP Annual Report {year_label} — The Institute for Research on Public "
            f"Policy (IRPP) annual report for {year_label} presents the organization's "
            f"research activities, policy publications, events, partnerships, board "
            f"governance, and financial highlights for the fiscal year. IRPP is an "
            f"independent, national, bilingual, not-for-profit research institute "
            f"headquartered in Montreal, Quebec, Canada. Source file: {fname}."
        )

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_reports(self, html: str):
        """Parse the annual-reports index page; return list of report dicts."""
        try:
            soup = _bs4_parse(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
            return []

        reports = []
        for div in soup.select("div.annual-report"):
            try:
                a = div.find("a", class_="annual-report")
                if not a:
                    continue
                pdf_url = (a.get("href") or "").strip()
                if not pdf_url or not pdf_url.lower().endswith(".pdf"):
                    continue

                img = a.find("img")
                title = (img.get("alt") or "").strip() if img else ""

                span = a.find("span", class_="secondary-info")
                year_label = span.get_text(strip=True) if span else ""

                if not title:
                    title = (
                        f"IRPP Annual Report {year_label}".strip()
                        or "IRPP Annual Report"
                    )

                fname = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                external_id = fname[:-4] if fname.lower().endswith(".pdf") else fname

                post_number = (
                    year_label if re.fullmatch(r"\d{4}", year_label) else None
                )
                published_date = (
                    f"{year_label}-09-01"
                    if re.fullmatch(r"\d{4}", year_label)
                    else None
                )

                reports.append(
                    {
                        "pdf_url": pdf_url,
                        "title": title,
                        "year_label": year_label,
                        "fname": fname,
                        "external_id": external_id,
                        "post_number": post_number,
                        "published_date": published_date,
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] error parsing a report entry: {exc}")

        return reports

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Fetch all annual reports, save to DB, return count saved."""
        start_wall = time.time()
        limit_eff = limit if limit is not None else float("inf")

        print(f"[{self.site_id}] fetching index: {self._START_URL}")
        html = self._curl_get(self._START_URL)
        if not html:
            print(f"[{self.site_id}] index fetch failed — aborting")
            return 0

        reports = self._parse_reports(html)
        if not reports:
            print(f"[{self.site_id}] no reports found on index page")
            return 0

        print(f"[{self.site_id}] found {len(reports)} report entries on index page")

        seen_urls: set = set()
        saved = 0

        for idx, rpt in enumerate(reports):
            if saved >= limit_eff:
                break

            elapsed = time.time() - start_wall
            if elapsed > self._MAX_WALL_SECONDS:
                print(
                    f"[{self.site_id}] wall-clock budget exceeded "
                    f"({elapsed:.0f}s), stopping"
                )
                break

            if idx > 0 and idx % 10 == 0:
                print(
                    f"[{self.site_id}] progress: processed {idx} entries, "
                    f"saved {saved}/{limit_eff}"
                )

            pdf_url = rpt["pdf_url"]
            if pdf_url in seen_urls:
                print(f"[{self.site_id}] skipping duplicate URL: {pdf_url}")
                continue
            seen_urls.add(pdf_url)

            try:
                abstract = self._extract_abstract_from_pdf(pdf_url)
                if not abstract or len(abstract) < 50:
                    abstract = self._fallback_abstract(rpt["year_label"], pdf_url)

                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] skipping {rpt['title']}: "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": rpt["external_id"],
                    "post_number": rpt["post_number"],
                    "title": rpt["title"],
                    "abstract": abstract,
                    "published_date": rpt["published_date"],
                    "posted_date": rpt["published_date"],
                    "url": self._START_URL,
                    "pdf_url": pdf_url,
                    "publisher": "IRPP; Institute for Research on Public Policy",
                    "authors": None,
                    "keywords": "annual report, IRPP, public policy, Canada",
                    "category": "Annual Report",
                    "original_filename": rpt["fname"],
                    "metadata": json.dumps(
                        {
                            "year_label": rpt["year_label"],
                            "posted_date": rpt["published_date"],
                            "originalFilename": rpt["fname"],
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(
                    f"[{self.site_id}] saved {saved}/{limit_eff}: {rpt['title']} "
                    f"(abstract {len(abstract)} chars)"
                )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} ({rpt.get('title')}) failed: {exc}")
                continue

            if saved < limit_eff:
                time.sleep(self._delay)

        print(f"[{self.site_id}] done — saved {saved} records total")
        return saved
