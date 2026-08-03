# -*- coding: utf-8 -*-
"""US Department of State — Plans, Performance & Budget crawler.

Source: https://www.state.gov/plans-performance-budget/
Strategy:
  1. Scrape section listing pages to collect individual document URLs.
  2. Probe historical annual-report slug patterns (newest first).
  3. For each document URL: fetch HTML to find WP page-ID, then call
     the WordPress REST API to get structured content + excerpt.
  4. Build abstract from excerpt + cleaned body text (≥100 chars).
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler


# ── helpers ──────────────────────────────────────────────────────────────────

def _try_bs4(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _strip_html(raw: str) -> str:
    """Remove script/style blocks, strip tags, unescape entities, normalise ws."""
    s = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL)
    s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.DOTALL)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ── crawler ───────────────────────────────────────────────────────────────────

class StateGovPlansBudgetCrawler(BaseCrawler):
    site_id   = "state-gov-plans-performance-bu"
    site_name = "Custom: state-gov-plans-performance-bu"
    base_url  = "https://www.state.gov"

    # Hub and section listing pages (used for initial link discovery)
    _SECTIONS = [
        "https://www.state.gov/plans-performance-budget/",
        "https://www.state.gov/plans-performance-budget/international-affairs-budgets/",
        "https://www.state.gov/plans-performance-budget/agency-financial-reports/",
        "https://www.state.gov/evidence-evaluation-and-learning/",
        "https://www.state.gov/performance-plans-and-reports/",
    ]

    # Standalone pages not reachable from the sections above
    _KNOWN_PAGES = [
        "https://www.state.gov/agency-strategic-plan",
    ]

    # Year-based slug patterns — probed newest-first for historical documents
    _YEAR_SLUG_PATTERNS = [
        "fy-{y}-international-affairs-budget",
        "fy-{y}-agency-financial-report",
        "fy-{y}-performance-plans-and-reports",
        "fy-{y}-annual-performance-report",
        "fy-{y}-congressional-budget-justification",
    ]
    _PROBE_YEAR_START = 2010
    _PROBE_YEAR_END   = 2027

    # URL substrings that indicate navigation / non-document pages
    _NAV_FRAGS = frozenset([
        "/wp-content/", "/wp-json/", "/translations/", "/bureaus-offices/",
        "/newsroom/", "/biographies-list/", "/privacy-policy", "/copyright",
        "/section-508", "/eeo-", "/digital-government", "/plain-writing",
        "/telephone-directory", "/department-email", "/feed/", "/visas/",
        "/travelers/", "/students/", "/employees/", "/business/",
        "/freedom-250", "/data/", "/open-government/", "/domestic-impact",
        "/information-quality", "/u-s-department-of-state-archive",
        "/department-reports/", "/department-of-state-org", "/xmlrpc",
        "/wp-login", "/sitemap", "/about/", "/secretary/",
        "/deputy-secretary", "/policy-issues/",
        # Sub-listing pages (2-segment paths handled by slash check)
    ])

    # ── URL filtering ──────────────────────────────────────────────────────

    def _is_doc_url(self, url: str) -> bool:
        """Return True only for top-level state.gov document-slug pages."""
        if not url.startswith("https://www.state.gov/"):
            return False
        if any(f in url for f in self._NAV_FRAGS):
            return False
        path = url.replace("https://www.state.gov/", "").strip("/")
        # Only top-level slugs (no internal '/')
        if not path or "/" in path:
            return False
        return True

    def _gather_doc_links(self, html_text: str) -> list[str]:
        """Extract candidate document URLs from a page's HTML."""
        seen: set[str] = set()
        result = []
        for m in re.finditer(r'href=["\']([^"\'?#\s]+)["\']', html_text):
            url = m.group(1).rstrip("/")
            if url not in seen and self._is_doc_url(url):
                seen.add(url)
                result.append(url)
        return result

    # ── candidate discovery ────────────────────────────────────────────────

    def _build_candidates(self) -> list[str]:
        """Return an ordered, deduplicated list of candidate document URLs."""
        ordered: list[str] = []
        seen: set[str] = set()

        def add(url: str) -> None:
            u = url.rstrip("/")
            if u not in seen:
                seen.add(u)
                ordered.append(u)

        # 1. Section listing pages
        for sec in self._SECTIONS:
            resp = self._request(sec)
            if not resp:
                continue
            if "Technical Difficulties" in resp.text:
                continue
            for link in self._gather_doc_links(resp.text):
                add(link)

        # 2. Known standalone pages
        for u in self._KNOWN_PAGES:
            add(u)

        # 3. Historical slug probes — newest first
        for year in range(self._PROBE_YEAR_END, self._PROBE_YEAR_START - 1, -1):
            for pat in self._YEAR_SLUG_PATTERNS:
                add(f"https://www.state.gov/{pat.format(y=year)}")

        return ordered

    # ── per-document fetch ────────────────────────────────────────────────

    def _fetch_wp_page(self, url: str) -> dict | None:
        """
        Fetch one document page and return a data dict, or None to skip.

        Steps:
          a. Fetch HTML → find embedded WP REST page-ID.
          b. Call /wp-json/wp/v2/pages/{id} for structured content.
          c. Build abstract from excerpt + cleaned content body.
        """
        page_url = url if url.endswith("/") else url + "/"

        # --- fetch HTML ---
        resp = self._request(page_url)
        if not resp:
            return None
        html_text = resp.text
        if not html_text or "Technical Difficulties" in html_text or len(html_text) < 500:
            return None

        # extract WP page-ID
        pid_m = re.search(r'wp(?:-json)?/wp/v2/pages/(\d+)', html_text)
        if not pid_m:
            return None
        page_id = pid_m.group(1)

        # --- fetch WP REST API ---
        api_url = (
            f"https://www.state.gov/wp-json/wp/v2/pages/{page_id}"
            "?_fields=id,title,date,modified,link,content,excerpt"
        )
        api_resp = self._request(api_url)
        if not api_resp:
            return None

        try:
            data = json.loads(api_resp.text)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(data, dict):
            return None

        title_raw = (data.get("title") or {}).get("rendered") or ""
        title = _html.unescape(title_raw).strip()
        if not title or "Technical Difficulties" in title:
            return None

        date_str     = (data.get("date")     or "")[:10]
        modified_str = (data.get("modified") or "")[:10]
        link         = data.get("link") or page_url

        # --- abstract ---
        excerpt_raw  = ((data.get("excerpt") or {}).get("rendered") or "")
        excerpt_text = _strip_html(excerpt_raw)

        content_raw  = ((data.get("content") or {}).get("rendered") or "")
        content_text = _strip_html(content_raw)

        # Remove breadcrumb preamble: text up to and including the title
        t_idx = content_text.find(title)
        body  = content_text[t_idx + len(title):].strip() if t_idx >= 0 else content_text

        # Trim trailing tags / categories block
        m_tags = re.search(r'\bTags\b', body)
        if m_tags:
            body = body[:m_tags.start()].strip()

        abstract_parts: list[str] = []
        if excerpt_text:
            abstract_parts.append(excerpt_text)
        if body and body not in excerpt_text and len(body) > 20:
            abstract_parts.append(body)
        abstract = re.sub(r"\s+", " ", " ".join(abstract_parts)).strip()

        # --- PDFs ---
        pdfs      = re.findall(r'href=["\']([^"\']+\.pdf)["\']', content_raw, re.I)
        pdf_url   = pdfs[0] if pdfs else None
        orig_name = pdf_url.rsplit("/", 1)[-1] if pdf_url else None

        # --- keywords from Tags block ---
        m_kw = re.search(r'\bTags\b(.*?)(?:\bCategories\b|\Z)', content_text, re.I | re.DOTALL)
        keywords = re.sub(r"\s+", " ", m_kw.group(1)).strip()[:400] if m_kw else ""

        return {
            "page_id":        page_id,
            "title":          title,
            "published_date": date_str,
            "modified_date":  modified_str,
            "url":            link,
            "abstract":       abstract,
            "pdf_url":        pdf_url,
            "original_filename": orig_name,
            "keywords":       keywords,
            "pdfs":           pdfs,
        }

    # ── main crawl ────────────────────────────────────────────────────────

    def crawl(self, limit=None):
        start_time = time.time()
        saved      = 0
        seen_urls: set[str] = set()
        limit_str  = str(limit) if limit is not None else "∞"
        page_ctr   = 0

        # Discovery (fetches section listing pages)
        print(f"[{self.site_id}] Discovering candidate URLs...")
        candidates = self._build_candidates()
        print(f"[{self.site_id}] {len(candidates)} candidates found")

        for idx, url in enumerate(candidates):
            # Honour limit
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget: 25 minutes
            elapsed = time.time() - start_time
            if elapsed > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] Wall-clock budget (25 min) reached after {elapsed:.0f}s. Stopping.")
                break

            # Safety cap: 200 virtual pages
            if page_ctr >= 200:
                print(f"[{self.site_id}] Safety cap of 200 pages reached. Stopping.")
                break

            # Deduplication
            norm = url.rstrip("/")
            if norm in seen_urls:
                continue
            seen_urls.add(norm)

            # Progress log every 10 candidates
            if idx > 0 and idx % 10 == 0:
                page_ctr += 1
                print(f"[{self.site_id}] page {page_ctr}: saved {saved}/{limit_str}")

            try:
                doc = self._fetch_wp_page(url)
                if not doc:
                    continue

                abstract = doc["abstract"]

                # Per task spec: skip if abstract < 50 chars
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] {url}: abstract too short "
                        f"({len(abstract)} chars), skipping"
                    )
                    continue

                self._save_paper({
                    "site_id":           self.site_id,
                    "external_id":       doc["page_id"],
                    "post_number":       doc["page_id"],
                    "title":             doc["title"],
                    "abstract":          abstract,
                    "url":               doc["url"],
                    "pdf_url":           doc.get("pdf_url"),
                    "published_date":    doc["published_date"],
                    "listed_date":       doc["modified_date"],
                    "posted_date":       doc["modified_date"],
                    "publisher":         "U.S. Department of State",
                    "authors":           "",
                    "department":        "Bureau of Budget and Planning",
                    "category":          "Plans, Performance, Budget",
                    "keywords":          doc.get("keywords", ""),
                    "original_filename": doc.get("original_filename"),
                    "doi":               None,
                    "metadata":          json.dumps({
                        "wp_page_id":       doc["page_id"],
                        "pdfs":             doc.get("pdfs", []),
                        "posted_date":      doc["modified_date"],
                        "originalFilename": doc.get("original_filename"),
                    }, ensure_ascii=False),
                })
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {doc['title'][:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {url} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
