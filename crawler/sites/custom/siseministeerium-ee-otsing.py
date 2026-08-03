# -*- coding: utf-8 -*-
"""Crawler for Siseministeerium (Estonia Interior Ministry) news/articles.

Endpoints:
  list  GET https://search.service.eu-live.vportal.ee/v1/search/siseministeerium
             ?sort_by=created&type=Artikkel&page={p}
        Requires: Referer + Origin headers from https://www.siseministeerium.ee
        Returns Solr JSON: response.numFound, response.docs[].
        Default page size = 10, page is 0-indexed.

  detail  GET https://www.siseministeerium.ee{doc.uri}
        HTML page; lead text in .field--name-field-lead-text;
        body in .node__content; PDF links via a[href$=.pdf].
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "siseministeerium-ee-otsing"


class SiseministeeriumEeOtsingCrawler(BaseCrawler):
    site_id = "siseministeerium-ee-otsing"
    site_name = "Custom: siseministeerium-ee-otsing"
    base_url = "https://www.siseministeerium.ee"

    _API_URL = "https://search.service.eu-live.vportal.ee/v1/search/siseministeerium"
    _LIST_TYPE = "Artikkel"
    _SORT_BY = "created"
    _PAGE_SIZE = 10        # API default; per_page param is ignored by the server
    _MIN_ABSTRACT = 50     # chars: skip item if below this
    _CURL_TIMEOUT = 45
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))       # safety cap

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=None, extra_headers=None):
        """Fetch *url* with curl, retry 3× with exponential backoff.

        Returns decoded text on success, None on failure.
        Always sends Referer + Origin so the search service responds.
        """
        timeout = timeout or self._CURL_TIMEOUT
        cmd = [
            "curl", "-g", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json, text/html, */*;q=0.8",
            "-H", "Accept-Language: et,en;q=0.9",
            "-H", "Referer: https://www.siseministeerium.ee/",
            "-H", "Origin: https://www.siseministeerium.ee",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                cmd.extend(["-H", f"{k}: {v}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr[:80]}"
            if attempt < 2:
                print(
                    f"[{_SITE_ID}] curl attempt {attempt + 1}/3 failed for {url}: "
                    f"{last_error}; retrying in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])
        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", str(text)).strip()

    @staticmethod
    def _node_id_from_ss_id(ss_search_api_id):
        """Extract numeric node ID from 'entity:node/4332:et' → '4332'."""
        if not ss_search_api_id:
            return None
        m = re.search(r"/(\d+):", str(ss_search_api_id))
        return m.group(1) if m else None

    def _build_abstract_from_api(self, doc):
        """Build a rich abstract from the search API doc (no detail fetch needed)."""
        lead = self._clean(doc.get("lead_text") or "")
        title = self._clean(doc.get("title") or "")
        content_parts = doc.get("content") or []

        body_parts = []
        for part in content_parts:
            cleaned = self._clean(part)
            if cleaned and cleaned != title and len(cleaned) > 30:
                body_parts.append(cleaned)

        if lead and body_parts:
            if body_parts[0].startswith(lead[:50]):
                combined = " ".join(body_parts)
            else:
                combined = lead + " " + " ".join(body_parts)
        elif lead:
            combined = lead
        else:
            combined = " ".join(body_parts)

        return re.sub(r"\s+", " ", combined).strip()[:4000]

    def _parse_detail(self, html_raw, detail_url):
        """Parse detail page HTML. Returns (abstract, pdf_url, original_filename)."""
        soup = self._parse_html(html_raw)
        if soup is None:
            return None, None, None

        abstract = None
        for sel in (
            ".field--name-field-lead-text",
            ".node__content",
            ".field--name-body",
            ".field--body",
            "article .field",
            "main article",
        ):
            el = soup.select_one(sel)
            if el:
                text = self._clean(el.get_text(" ", strip=True))
                if len(text) >= self._MIN_ABSTRACT:
                    abstract = text[:4000]
                    break

        pdf_url = None
        original_filename = None
        for a_tag in soup.select("a[href]"):
            href = a_tag.get("href") or ""
            href_lower = href.lower()
            if ".pdf" in href_lower or (
                "/files/" in href_lower and any(
                    href_lower.endswith(ext) for ext in (".pdf", ".PDF")
                )
            ):
                abs_href = urljoin(self.base_url, href)
                if ".pdf" in abs_href.lower():
                    pdf_url = abs_href
                    fname = abs_href.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                    original_filename = fname if "." in fname else None
                    break

        return abstract, pdf_url, original_filename

    # ------------------------------------------------------------------
    # List API
    # ------------------------------------------------------------------

    def _fetch_page(self, page_num):
        """Fetch one page from the search API. Returns (docs, total_found)."""
        url = (
            f"{self._API_URL}"
            f"?sort_by={self._SORT_BY}&type={self._LIST_TYPE}&page={page_num}"
        )
        raw = self._curl(url)
        if not raw:
            return [], 0
        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] JSON parse error on page {page_num}: {exc}")
            return [], 0
        resp = data.get("response", {})
        total = resp.get("numFound", 0) or 0
        docs = resp.get("docs") or []
        return docs, total

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        print(f"[{_SITE_ID}] starting crawl (limit={limit_str})")
        print(
            f"[{_SITE_ID}] list API: {self._API_URL}"
            f"?sort_by={self._SORT_BY}&type={self._LIST_TYPE}&page={{p}}"
        )

        saved = 0
        seen_urls = set()
        start_time = time.time()

        for page in range(self._MAX_PAGES):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page}; stopping")
                break

            if page == self._MAX_PAGES - 1:
                print(f"[{_SITE_ID}] safety cap of {self._MAX_PAGES} pages reached; stopping")

            docs, total = self._fetch_page(page)

            if page == 0:
                print(f"[{_SITE_ID}] total available: {total}")

            if not docs:
                print(f"[{_SITE_ID}] page {page}: empty response; end of results")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            page_had_new = False
            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                uri = doc.get("uri") or ""
                detail_url = urljoin(self.base_url, uri) if uri else ""
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                page_had_new = True

                try:
                    title = self._clean(doc.get("title") or "")
                    if not title:
                        continue

                    ss_id = doc.get("ss_search_api_id") or doc.get("id") or ""
                    node_id = self._node_id_from_ss_id(ss_id)
                    external_id = node_id or ss_id

                    created_raw = doc.get("created") or ""
                    published_date = created_raw[:10] if len(created_raw) >= 10 else None

                    category = doc.get("ss_entity_bundle") or doc.get("content_type") or ""

                    # Fetch detail page for richer abstract + PDF links
                    time.sleep(self._delay)
                    det_raw = self._curl(detail_url)
                    det_abstract, pdf_url, original_filename = (None, None, None)
                    if det_raw:
                        det_abstract, pdf_url, original_filename = self._parse_detail(
                            det_raw, detail_url
                        )

                    # Prefer detail page body; fall back to API content array
                    api_abstract = self._build_abstract_from_api(doc)
                    if det_abstract and len(det_abstract) >= len(api_abstract or ""):
                        abstract = det_abstract
                    else:
                        abstract = api_abstract or det_abstract or ""

                    if not abstract or len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{_SITE_ID}] skip '{title[:50]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    metadata = {
                        "posted_date": created_raw,
                        "node_id": node_id,
                        "ss_search_api_id": ss_id,
                        "content_type": doc.get("content_type"),
                        "langcode": doc.get("langcode"),
                        "image_uri": doc.get("image_uri"),
                    }
                    metadata = {k: v for k, v in metadata.items() if v}

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": node_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "category": category,
                        "publisher": "Siseministeerium",
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    label = doc.get("title") or detail_url or "unknown"
                    print(f"[{_SITE_ID}] item '{label[:50]}' failed: {exc}")
                    continue

            if not page_had_new:
                print(f"[{_SITE_ID}] page {page}: all URLs already seen; stopping")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
