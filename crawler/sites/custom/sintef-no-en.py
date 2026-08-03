# -*- coding: utf-8 -*-
"""SINTEF (sintef.no) research reports crawler.

Starting URL: https://www.sintef.no/en/search/?querytext=*&flow=reports

The search-result list is server-rendered HTML on sintef.no, but every
hit links out to an external record on the Norwegian national research
archive (nva.sikt.no / api.nva.unit.no). Full metadata (abstract,
authors, dates, PDF file) is fetched from the public NVA REST API:

  - list:      https://www.sintef.no/en/search/?querytext=*&flow=reports&pagenumber=N
  - detail:    https://api.nva.unit.no/publication/{registrationId}
  - PDF link:  https://api.nva.unit.no/publication/{registrationId}/filelink/{fileId}
               -> returns a short-lived presigned S3 URL for the PDF.
  - publisher: https://api.nva.unit.no/publication-channels-v2/publisher/{id}/{year}
  - org/dept:  https://api.nva.unit.no/cristin/organization/{id}

The NVA API requires ``Accept: application/json`` or it 303-redirects to
the HTML SPA page instead of returning JSON.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


class SintefNoEnCrawler(BaseCrawler):
    """Crawler for SINTEF research reports (sintef.no -> NVA archive)."""

    site_id = "sintef-no-en"
    site_name = "Custom: sintef-no-en"
    base_url = "https://www.sintef.no"

    _LIST_URL = "https://www.sintef.no/en/search/?querytext=*&flow=reports&pagenumber={page}"
    _NVA_API = "https://api.nva.unit.no/publication/{reg_id}"
    _NVA_FILELINK_API = "https://api.nva.unit.no/publication/{reg_id}/filelink/{file_id}"
    _NVA_PUBLISHER_API = "https://api.nva.unit.no/publication-channels-v2/publisher/{channel_id}/{year}"
    _NVA_ORG_API = "https://api.nva.unit.no/cristin/organization/{org_id}"

    _MIN_ABSTRACT = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
    _RETRY_WAITS = (1, 3, 9)

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, as_json=False):
        """GET via curl with retries. Returns decoded text, or None on failure."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
        ]
        if as_json:
            cmd += ["-H", "Accept: application/json"]
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error ({exc}) for {url}")
            if attempt < 2:
                wait = self._RETRY_WAITS[attempt]
                print(f"[{self.site_id}] retrying in {wait}s: {url}")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    def _fetch_json(self, url):
        raw = self._curl_get(url, as_json=True)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw_html):
        """Return a list of {reg_id, title, url} dicts for one search-result page."""
        entries = []
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
            return entries

        result = soup.find("div", id="result")
        container = result if result else soup

        for article in container.find_all("article", class_=re.compile(r"\bsearch-hit\b")):
            try:
                link = article.find("a", class_="search-hit__title-link")
                if not link or not link.get("href"):
                    continue
                url = link["href"].strip()

                title_span = link.find("span", class_="search-hit__title-text")
                title = title_span.get_text(strip=True) if title_span else link.get_text(strip=True)
                if not title:
                    continue

                content_div = article.find("div", class_="search-hit__content")
                reg_id = (content_div.get("data-publicationid") if content_div else None)
                if not reg_id:
                    tail = url.rstrip("/").split("/")[-1]
                    reg_id = tail if tail else None

                entries.append({"reg_id": reg_id, "title": title, "url": url})
            except Exception as exc:
                print(f"[{self.site_id}] list item parse error: {exc}")
                continue

        return entries

    # ------------------------------------------------------------------
    # NVA detail enrichment
    # ------------------------------------------------------------------

    @staticmethod
    def _iso_from_date_parts(date_parts):
        if not date_parts:
            return None
        year = date_parts.get("year")
        if not year:
            return None
        month = date_parts.get("month") or "1"
        day = date_parts.get("day") or "1"
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except (TypeError, ValueError):
            return None

    def _resolve_publisher_name(self, publication_context):
        publisher = (publication_context or {}).get("publisher") or {}
        channel_url = publisher.get("id")
        if not channel_url:
            return ""
        m = re.search(r"/publisher/([^/]+)/(\d{4})", channel_url)
        if not m:
            return ""
        api_url = self._NVA_PUBLISHER_API.format(channel_id=m.group(1), year=m.group(2))
        data = self._fetch_json(api_url)
        if not data:
            return ""
        return data.get("name") or ""

    def _resolve_department(self, contributors):
        for contributor in contributors or []:
            affiliations = contributor.get("affiliations") or []
            for aff in affiliations:
                org_url = aff.get("id")
                if not org_url:
                    continue
                m = re.search(r"/organization/([^/?#]+)", org_url)
                if not m:
                    continue
                api_url = self._NVA_ORG_API.format(org_id=m.group(1))
                data = self._fetch_json(api_url)
                if not data:
                    return ""
                labels = data.get("labels") or {}
                return labels.get("en") or labels.get("nb") or ""
        return ""

    def _resolve_pdf(self, reg_id, associated_artifacts):
        for artifact in associated_artifacts or []:
            if artifact.get("type") != "OpenFile":
                continue
            if artifact.get("mimeType") != "application/pdf":
                continue
            file_id = artifact.get("identifier")
            if not file_id:
                continue
            api_url = self._NVA_FILELINK_API.format(reg_id=reg_id, file_id=file_id)
            data = self._fetch_json(api_url)
            pdf_url = (data or {}).get("id")
            if pdf_url:
                return pdf_url, artifact.get("name") or "", api_url
        return None, "", None

    def _build_paper(self, entry):
        reg_id = entry["reg_id"]
        detail = self._fetch_json(self._NVA_API.format(reg_id=reg_id))
        if not detail:
            return None

        ed = detail.get("entityDescription") or {}
        abstract = (ed.get("abstract") or "").strip()
        if len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] short abstract ({len(abstract)}) for '{entry['title'][:50]}', skipping")
            return None

        title = ed.get("mainTitle") or entry["title"]

        published_date = self._iso_from_date_parts(ed.get("publicationDate"))
        listed_date_raw = detail.get("publishedDate")
        listed_date = (listed_date_raw or "")[:10] or None

        contributors = ed.get("contributors") or []
        author_names = []
        for c in contributors:
            if (c.get("role") or {}).get("type") != "Creator":
                continue
            name = (c.get("identity") or {}).get("name")
            if name and name not in author_names:
                author_names.append(name)
        authors = "; ".join(author_names)

        reference = ed.get("reference") or {}
        publication_context = reference.get("publicationContext") or {}
        publication_instance = reference.get("publicationInstance") or {}

        publisher = self._resolve_publisher_name(publication_context)
        department = self._resolve_department(contributors)

        journal = ""
        context_type = publication_context.get("type") or ""
        if "Journal" in context_type:
            journal = publication_context.get("name") or ""

        keywords = ", ".join(ed.get("tags") or [])
        category = publication_instance.get("type") or context_type
        doi = detail.get("doi")
        isbn_list = publication_context.get("isbnList") or []

        pdf_url, original_filename, filelink_api_url = self._resolve_pdf(
            reg_id, detail.get("associatedArtifacts")
        )

        meta = {
            "posted_date": listed_date_raw,
            "originalFilename": original_filename or None,
            "journal_raw": journal or None,
            "series": (publication_context.get("series") or {}).get("id"),
            "volume": None,
            "issue": None,
            "publicationIdentifier": reg_id,
            "isbn": "; ".join(isbn_list) if isbn_list else None,
            "language": ed.get("language"),
            "contextType": context_type,
            "instanceType": publication_instance.get("type"),
            "fileApiUrl": filelink_api_url,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": reg_id,
            "post_number": reg_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": journal,
            "url": entry["url"],
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename or None,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 0

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page += 1
                list_url = self._LIST_URL.format(page=page)
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                    break

                entries = self._parse_list_page(raw)
                if not entries:
                    print(f"[{self.site_id}] page {page}: no entries found. End of results.")
                    break

                new_on_page = 0
                for entry in entries:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - start_time > self._MAX_WALL:
                        print(f"[{self.site_id}] Wall-clock budget exceeded mid-page. Stopping cleanly.")
                        break

                    url = entry["url"]
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    try:
                        paper = self._build_paper(entry)
                        if not paper:
                            continue
                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {paper['title'][:60]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item failed ({entry.get('reg_id', '?')}): {exc}; continuing.")
                        continue
                    finally:
                        time.sleep(self._delay)

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page}: all items already seen. Stopping to avoid loop.")
                    break

                if page % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
