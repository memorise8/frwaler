# -*- coding: utf-8 -*-
"""Crawler for National Academies CSTB publications (nationalacademies.org)."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler


class NationalAcademiesPublicationsCrawler(BaseCrawler):
    site_id = "nationalacademies-org-publications"
    site_name = "Custom: nationalacademies-org-publications"
    base_url = "https://www.nationalacademies.org"

    _LIST_URL = "https://www.nationalacademies.org/publications/all"
    _MAX_PAGES = 200
    _BUDGET_SECS = 25 * 60  # 25 minutes

    # ------------------------------------------------------------------ #
    # Network helpers                                                      #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url, retries=3):
        """Fetch URL via curl; returns decoded text or None on failure."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,*/*;q=0.8",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = (attempt + 1) ** 2
                if attempt < retries - 1:
                    print(f"[{self.site_id}] Empty response (attempt {attempt+1}), retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                wait = (attempt + 1) ** 2
                if attempt < retries - 1:
                    print(f"[{self.site_id}] curl error (attempt {attempt+1}): {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # Parsing helpers                                                      #
    # ------------------------------------------------------------------ #

    def _parse_list_page(self, html):
        """Return list of unique (url, pub_id) from a publications listing page."""
        seen_urls = set()
        seen_ids = set()
        result = []
        # Match /units/.../publication/ID, /projects/.../publication/ID, and /publications/ID
        patterns = [
            r'href="(https://www\.nationalacademies\.org/(?:units|projects)/[^"]+/publication/(\d+))"',
            r'href="(https://www\.nationalacademies\.org/publications/(\d+))"',
        ]
        for pat in patterns:
            for m in re.finditer(pat, html):
                url, pub_id = m.group(1), m.group(2)
                if url not in seen_urls and pub_id not in seen_ids:
                    seen_urls.add(url)
                    seen_ids.add(pub_id)
                    result.append((url, pub_id))
        return result

    def _extract_datalayer(self, html, pub_id):
        """Extract publicationsDataLayer[pub_id] JSON from page HTML."""
        # JSON structure has no nested objects, so [^{}]* is safe
        m = re.search(
            r'window\.publicationsDataLayer\[' + re.escape(pub_id) + r'\]\s*=\s*(\{[^{}]*\})',
            html,
        )
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return {}

    def _meta(self, html, name):
        """Extract <meta name|property=name content=...> value."""
        for pat in [
            r'<meta[^>]+(?:property|name)="' + re.escape(name) + r'"[^>]+content="([^"]*)"',
            r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="' + re.escape(name) + r'"',
        ]:
            m = re.search(pat, html, re.DOTALL)
            if m:
                return m.group(1).strip()
        return ""

    def _parse_detail(self, html, pub_id, url):
        """Extract all publication fields from a detail page."""
        data_layer = self._extract_datalayer(html, pub_id)

        # Abstract: og:description is the publication's summary
        abstract = (
            self._meta(html, "og:description")
            or self._meta(html, "description")
        )

        # Title: og:title or <title>
        title = self._meta(html, "og:title")
        if not title:
            m = re.search(r"<title>([^<]+)</title>", html)
            if m:
                title = m.group(1)
        if title:
            title = re.sub(r"\s*[|\-]\s*National Academies.*$", "", title, flags=re.IGNORECASE).strip()

        # DOI
        doi = ""
        m = re.search(r'href="https://doi\.org/(10\.[^"]+)"', html)
        if m:
            doi = m.group(1).strip()

        # PDF URL (NAP resource links)
        pdf_url = None
        m = re.search(r'href="(https://nap\.nationalacademies\.org/[^"]+\.pdf)"', html)
        if m:
            pdf_url = m.group(1)

        # Published date from dataLayer created_at (format: "2025-12-10 18:16:25")
        published_date = None
        created_at = data_layer.get("created_at") or ""
        m2 = re.match(r"(\d{4}-\d{2}-\d{2})", created_at)
        if m2:
            published_date = m2.group(1)

        # Authors — semicolon-separated in dataLayer
        authors = data_layer.get("authors") or ""

        # Topics → keywords (comma-separated)
        topics = data_layer.get("topics") or []
        keywords = ",".join(topics) if topics else None

        # Category from sub_type
        category = data_layer.get("sub_type") or ""

        # Original filename from pdf_url
        original_filename = None
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1]

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "authors": authors,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "data_layer": data_layer,
        }

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "∞"

        for p in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget
            if time.time() - start_time > self._BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached at page {p}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if p == self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")

            list_url = f"{self._LIST_URL}?unit[]=CSTB&page={p}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {p}. Stopping.")
                break

            items = self._parse_list_page(raw)
            new_items = [(url, pid) for url, pid in items if url not in seen_urls]

            if not new_items:
                print(f"[{self.site_id}] No new items at page {p}. Done.")
                break

            if p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

            for url, pub_id in new_items:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(url)
                    if not detail_html:
                        print(f"[{self.site_id}] item {pub_id} failed: empty response")
                        continue

                    fields = self._parse_detail(detail_html, pub_id, url)

                    if not fields["title"]:
                        print(f"[{self.site_id}] item {pub_id}: no title, skipping")
                        continue

                    abstract = fields["abstract"]
                    if not abstract or len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {pub_id}: abstract too short "
                            f"({len(abstract or '')} chars), skipping"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": pub_id,
                        "post_number": pub_id,
                        "title": fields["title"],
                        "abstract": abstract,
                        "published_date": fields["published_date"],
                        "listed_date": fields["published_date"],
                        "authors": fields["authors"],
                        "publisher": "National Academies of Sciences, Engineering, and Medicine",
                        "keywords": fields["keywords"],
                        "category": fields["category"],
                        "doi": fields["doi"],
                        "url": url,
                        "pdf_url": fields["pdf_url"],
                        "original_filename": fields["original_filename"],
                        "metadata": json.dumps(
                            {
                                "posted_date": fields["data_layer"].get("created_at"),
                                "originalFilename": fields["original_filename"],
                                "topics": fields["data_layer"].get("topics", []),
                                "sub_type": fields["data_layer"].get("sub_type"),
                                "nap_id": pub_id,
                                "type": fields["data_layer"].get("type"),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {fields['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pub_id} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
