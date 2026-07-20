# -*- coding: utf-8 -*-
"""U.S. Energy Information Administration (EIA) reports crawler.

Starting URL: https://www.eia.gov/reports/

The "All Reports & Publications" page renders its list client-side via
an AJAX call to /global/includes/bookshelf/index.php?tags=<id>. There is
no classic incrementing page number, so each "page" here is one topic
tag walked in descending order of its declared report count; results are
deduplicated across tags via seen ids/urls.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


class EiaGovReportsCrawler(BaseCrawler):
    """Crawler for EIA "All Reports & Publications" bookshelf."""

    site_id = "eia-gov-reports"
    site_name = "Custom: eia-gov-reports"
    base_url = "https://www.eia.gov"

    _BOOKSHELF_URL = "https://www.eia.gov/global/includes/bookshelf/index.php"
    _PUBLISHER = "U.S. Energy Information Administration (EIA)"
    _MIN_ABSTRACT = 100
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: dict | None = None) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        full_url = url
        if params:
            from urllib.parse import urlencode
            full_url = f"{url}?{urlencode(params)}"
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            full_url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {full_url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {full_url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _curl_get_json(self, url: str, params: dict | None = None):
        raw = self._curl_get(url, params)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] JSON parse error for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _get_tags(self) -> list:
        """Return topic tags sorted by declared report count, descending."""
        data = self._curl_get_json(self._BOOKSHELF_URL)
        if not data:
            return []
        tags = data.get("tags") or []
        tags = [t for t in tags if str(t.get("identifier", "")).startswith("T")]
        tags.sort(key=lambda t: t.get("numreports", 0), reverse=True)
        return tags

    def _get_reports_for_tag(self, tag_numeric_id: str) -> list:
        data = self._curl_get_json(self._BOOKSHELF_URL, {"tags": tag_numeric_id})
        if not data:
            return []
        return data.get("reports") or []

    @staticmethod
    def _parse_release_date(raw: str):
        if not raw:
            return None
        cleaned = re.sub(r"\s+", " ", raw.strip())
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y"):
            try:
                from datetime import datetime
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _extract_abstract(self, raw_html: str) -> str:
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error while extracting abstract: {exc}")
            return ""
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        paragraphs = []
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            if len(text) > 20:
                paragraphs.append(text)
        combined = re.sub(r"\s+", " ", " ".join(paragraphs)).strip()
        return combined[:4000]

    @staticmethod
    def _filename_from_url(url: str):
        if not url:
            return None
        last = url.rstrip("/").rsplit("/", 1)[-1]
        return last or None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl EIA reports across topic tags, enriching thin abstracts from detail pages."""
        saved = 0
        seen_urls: set = set()
        seen_ids: set = set()
        start_time = time.time()
        page_num = 0
        lim_str = str(limit) if limit is not None else "inf"

        tags = self._get_tags()
        if not tags:
            print(f"[{self.site_id}] No tags found. Aborting.")
            return 0

        label_by_id = {}
        for t in tags:
            ident = str(t.get("identifier", ""))
            if ident.startswith("T"):
                label_by_id[ident[1:]] = t.get("label", "")

        print(f"[{self.site_id}] Found {len(tags)} topic tags to walk.")

        try:
            for tag in tags:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page_num += 1
                tag_id = str(tag.get("identifier", ""))[1:]
                tag_label = tag.get("label", "")
                reports = self._get_reports_for_tag(tag_id)
                new_count = 0

                for report in reports:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - start_time > self._MAX_WALL:
                        print(f"[{self.site_id}] Wall-clock budget exceeded mid-tag. Stopping cleanly.")
                        break

                    rid = report.get("id")
                    link_html = report.get("link_html") or ""
                    if not link_html:
                        continue
                    url = urljoin(self.base_url, link_html)
                    if url in seen_urls or rid in seen_ids:
                        continue
                    seen_urls.add(url)
                    if rid is not None:
                        seen_ids.add(rid)

                    try:
                        title = (report.get("title") or "").strip()
                        if not title:
                            print(f"[{self.site_id}] item {rid} has no title, skipping.")
                            continue

                        abstract = re.sub(r"\s+", " ", (report.get("summary_descript") or "")).strip()
                        if len(abstract) < self._MIN_ABSTRACT:
                            time.sleep(self._delay)
                            detail_html = self._curl_get(url)
                            if detail_html:
                                extracted = self._extract_abstract(detail_html)
                                if len(extracted) >= self._MIN_ABSTRACT:
                                    abstract = extracted
                                elif abstract and extracted:
                                    combined = re.sub(r"\s+", " ", f"{abstract} {extracted}").strip()
                                    if len(combined) >= self._MIN_ABSTRACT:
                                        abstract = combined
                        if len(abstract) < 50:
                            print(f"[{self.site_id}] item {rid} abstract too short ({len(abstract)} chars), skipping.")
                            continue
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] item {rid} abstract below quality threshold ({len(abstract)} chars), skipping.")
                            continue

                        published_date = self._parse_release_date(report.get("release_date"))

                        link_pdf = report.get("link_pdf") or None
                        if link_pdf and "." not in link_pdf.rsplit("/", 1)[-1]:
                            # EIA sometimes returns a bogus "/" placeholder instead of
                            # omitting link_pdf; urljoin would turn that into the site
                            # homepage, so treat any extension-less value as "no PDF".
                            link_pdf = None
                        pdf_url = urljoin(self.base_url, link_pdf) if link_pdf else None
                        report_number = report.get("report_number") or None
                        original_filename = self._filename_from_url(link_pdf) or (
                            report_number if report_number and report_number.lower().endswith(".pdf") else None
                        )

                        alltags = [t.strip() for t in (report.get("alltags") or "").split(",") if t.strip()]
                        keyword_labels = [label_by_id[t] for t in alltags if t in label_by_id and label_by_id[t]]
                        keywords = ", ".join(dict.fromkeys(keyword_labels)) or None

                        post_number = str(rid) if rid is not None else None

                        metadata = {
                            "posted_date": report.get("release_date"),
                            "originalFilename": original_filename,
                            "report_id": rid,
                            "report_number": report_number,
                            "alltags": report.get("alltags"),
                            "has_forecast": report.get("has_forecast"),
                            "has_data": report.get("has_data"),
                            "has_analysis": report.get("has_analysis"),
                            "tag_id": tag_id,
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": post_number,
                            "post_number": post_number,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": published_date,
                            "authors": None,
                            "publisher": self._PUBLISHER,
                            "department": None,
                            "journal": None,
                            "url": url,
                            "pdf_url": pdf_url,
                            "keywords": keywords,
                            "category": tag_label or None,
                            "doi": None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        new_count += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {rid} failed: {exc}")
                        continue

                if new_count == 0:
                    print(f"[{self.site_id}] tag '{tag_label}' (page {page_num}) yielded 0 new records.")

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
