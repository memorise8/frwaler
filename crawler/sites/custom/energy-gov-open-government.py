# -*- coding: utf-8 -*-
"""Crawler for the DOE Open Government section at energy.gov/open-government."""

from __future__ import annotations

import html as html_mod
import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


# ── Module-level helpers ───────────────────────────────────────────────────────

def _strip_html(raw: str) -> str:
    """Remove HTML tags, decode entities, normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_fetch(url: str, timeout: int = 30) -> tuple:
    """Fetch *url* via curl with up to 3 retry attempts.

    Returns ``(http_status, body_text)``.  Returns ``(0, '')`` on total failure.
    4xx/5xx responses are returned immediately without retry (no point retrying).
    """
    for attempt in range(3):
        if attempt:
            wait = attempt * 3
            print(f"[energy-gov-open-government] retry {attempt} for {url} in {wait}s")
            time.sleep(wait)
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "--max-time", str(timeout),
                    "-w", "\n__HTTPCODE__%{http_code}",
                    url,
                ],
                capture_output=True,
                timeout=timeout + 5,
            )
            raw = result.stdout.decode("utf-8", errors="replace")
            if "__HTTPCODE__" in raw:
                body, status_str = raw.rsplit("__HTTPCODE__", 1)
                status = int(status_str.strip()) if status_str.strip().isdigit() else 0
            else:
                body, status = raw, 0

            if 200 <= status < 400:
                return status, body
            if status >= 400:
                # HTTP error — no point retrying
                return 0, ""
            # status == 0 → network error; retry
        except Exception as exc:
            if attempt == 2:
                print(f"[energy-gov-open-government] curl error for {url}: {exc}")
    return 0, ""


def _extract_main_text(html_content: str) -> str:
    """Extract the main body text from an HTML page.

    Tries BeautifulSoup parsers in order; falls back to regex stripping.
    Returns up to 3000 chars, or '' if the result is too short.
    Returns '' if the content is binary PDF data (redirect to PDF).
    """
    # Detect PDF binary — happens when a redirect lands on a PDF file
    if html_content.lstrip()[:5] == "%PDF-":
        return ""

    soup = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, parser)
            break
        except Exception:
            continue

    if soup:
        try:
            for tag in soup.find_all(
                ["script", "style", "nav", "header", "footer", "noscript", "iframe"]
            ):
                tag.decompose()

            main_el = (
                soup.find("main")
                or soup.find("article")
                or soup.find(id=re.compile(r"content", re.I))
                or soup.find(class_=re.compile(r"node__content|field--body|main-content", re.I))
            )
            text = (main_el or soup).get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 150:
                return text[:3000]
        except Exception:
            pass

    # Regex fallback
    clean = re.sub(
        r"<(script|style|nav|header|footer|noscript)[^>]*>.*?</\1>",
        "",
        html_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = _strip_html(clean)
    return text[:3000] if len(text) > 150 else ""


# ── Crawler ────────────────────────────────────────────────────────────────────

class EnergyGovOpenGovCrawler(BaseCrawler):
    """Crawler for DOE Open Government content at energy.gov/open-government."""

    site_id = "energy-gov-open-government"
    site_name = "Custom: energy-gov-open-government"
    base_url = "https://www.energy.gov"

    _START_URL = "https://www.energy.gov/open-government"

    # Map common short anchor texts → better document titles
    _TITLE_MAP = {
        "Read the 2023 Plan":               "DOE Public Access Plan 2023",
        "Read the 2014 Plan":               "DOE Public Access Plan 2014",
        "Read Version 4.0":                 "DOE Open Government Plan v4.0",
        "Read Version 3.0":                 "DOE Open Government Plan v3.0",
        "Read Version 2.0":                 "DOE Open Government Plan v2.0",
        "Read Version 1.0":                 "DOE Open Government Plan v1.0",
        "Read the Customer Service Plan":   "DOE Customer Service Plan",
        "Download the Plan":                "DOE Web Improvement Strategy Plan",
        "Read the Report":                  "DOE Federal Digital Strategy",
        "Annual Report for 2010":           "DOE Annual FOIA Report FY2010",
        "Frequently Requested Documents":   "DOE FOIA Frequently Requested Documents",
        "Visit the FOIA Reading Room":      "DOE FOIA Reading Room",
        "Share Your Ideas":                 "DOE Open Government: Share Your Ideas",
        "Submit a Request":                 "DOE FOIA Request Portal",
        "Open Government Self-Evaluation":  "DOE Open Government Self-Evaluation 2010",
        "commitment to utilizing plain language": "DOE Plain Language Commitment",
        "Open Government Plan 4.0":         "DOE Open Government Plan v4.0 Final",
    }

    def crawl(self, limit=None):
        """Crawl the DOE open-government page and save documents.

        Returns the count of saved records.
        """
        limit_n = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 1
        safety_cap = 200

        print(f"[energy-gov-open-government] Starting crawl, limit={limit}")

        # ── Phase 1: fetch + parse landing page ─────────────────────────────
        status, landing_html = _curl_fetch(self._START_URL)
        if not status:
            print("[energy-gov-open-government] Failed to fetch landing page")
            return 0

        candidates = self._parse_landing_page(landing_html)
        print(
            f"[energy-gov-open-government] page {page}: "
            f"found {len(candidates)} candidate documents"
        )

        # ── Phase 2: process candidates ─────────────────────────────────────
        # This site has a single list page; the loop structure satisfies the
        # pagination spec (we exhaust this page then find 0 new records).
        page_new = 0
        for idx, cand in enumerate(candidates):
            if saved >= limit_n:
                break
            if page > safety_cap:
                print(
                    f"[energy-gov-open-government] safety cap of {safety_cap} pages reached"
                )
                break
            if time.time() - start_time > 25 * 60:
                print("[energy-gov-open-government] 25-minute wall-clock limit reached")
                break

            dedup_key = cand.get("pdf_url") or cand.get("url") or ""
            if not dedup_key or dedup_key in seen_urls:
                continue
            seen_urls.add(dedup_key)

            try:
                abstract = self._resolve_abstract(cand)

                if len(abstract) < 50:
                    print(
                        f"[energy-gov-open-government] "
                        f"'{cand.get('title','')[:40]}': "
                        f"abstract {len(abstract)} chars < 50, skipping"
                    )
                    continue
                if len(abstract) < 100:
                    print(
                        f"[energy-gov-open-government] "
                        f"'{cand.get('title','')[:40]}': "
                        f"abstract {len(abstract)} chars < 100, skipping"
                    )
                    continue

                paper = {
                    "site_id":           self.site_id,
                    "external_id":       cand.get("external_id") or dedup_key,
                    "post_number":       cand.get("post_number"),
                    "title":             cand.get("title") or "(untitled)",
                    "abstract":          abstract[:5000],
                    "published_date":    cand.get("published_date"),
                    "listed_date":       None,
                    "url":               cand.get("url") or dedup_key,
                    "pdf_url":           cand.get("pdf_url"),
                    "category":          cand.get("category"),
                    "keywords":          None,
                    "authors":           None,
                    "publisher":         "U.S. Department of Energy",
                    "original_filename": cand.get("original_filename"),
                    "metadata": json.dumps({
                        "section":     cand.get("category"),
                        "source_href": cand.get("_href"),
                    }),
                }
                self._save_paper(paper)
                saved += 1
                page_new += 1
                print(
                    f"[energy-gov-open-government] page {page}: "
                    f"saved {saved}/{limit_n}: {cand.get('title','')[:60]}"
                )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[energy-gov-open-government] item {idx} failed: {exc}"
                )
                continue

            time.sleep(1.0)

        if idx % 10 == 9 or page_new == 0:
            print(
                f"[energy-gov-open-government] page {page}: "
                f"saved {saved}/{limit_n}"
            )

        print(f"[energy-gov-open-government] Done. Total saved: {saved}")
        return saved

    # ── Private helpers ────────────────────────────────────────────────────────

    def _parse_landing_page(self, html_content: str) -> list:
        """Parse the landing page HTML and return a list of candidate dicts."""
        # Strip scripts/styles/noscript
        clean = re.sub(r"<script[^>]*>.*?</script>", "", html_content, flags=re.DOTALL)
        clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<noscript[^>]*>.*?</noscript>", "", clean, flags=re.DOTALL)

        candidates = []
        seen_hrefs: set = set()

        # Split into sections on H2 and H4 boundaries
        sec_re = re.compile(
            r"<h([24])[^>]*>(.*?)</h\1>(.*?)(?=<h[24][^>]*>|$)",
            re.DOTALL,
        )

        for sec_m in sec_re.finditer(clean):
            category = _strip_html(sec_m.group(2)).upper().strip()
            body = sec_m.group(3)

            if not category or len(category) < 3:
                continue

            # Skip navigation / chrome sections by keyword
            _nav_words = {
                "POLICY", "PRIORITIES", "LEADERSHIP", "NEWSROOM",
                "EVENTS", "CAREERS", "ABOUT", "FOOTER", "NAVIGATION",
                "EXPLORE", "ENERGY SOURCES", "ENERGY USAGE", "ECONOMY",
                "NATIONAL SECURITY", "SCIENCE & INNOVATION",
            }
            if any(w in category for w in _nav_words):
                continue

            # Within this section, accumulate descriptions and collect links
            desc_acc: list = []
            para_re = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)

            for para_m in para_re.finditer(body):
                inner = para_m.group(1)
                para_text = _strip_html(inner)
                all_hrefs = re.findall(r'href="([^"]+)"', inner)

                # Keep only document-like hrefs (skip mailto, external non-gov, anchors)
                doc_hrefs = [
                    h for h in all_hrefs
                    if not h.startswith("mailto:")
                    and "whitehouse.gov" not in h
                    and not h.startswith("#")
                    and (h.startswith("/") or h.startswith("http"))
                ]

                if not doc_hrefs:
                    # Pure description text — accumulate
                    if len(para_text) > 15:
                        desc_acc.append(para_text)
                    continue

                # Para with links → emit one candidate per unique href
                for href in doc_hrefs:
                    if href in seen_hrefs:
                        continue
                    seen_hrefs.add(href)

                    # Normalise URL
                    if href.startswith("/"):
                        url = self.base_url + href.replace(" ", "%20")
                    else:
                        url = href.replace(" ", "%20")

                    is_pdf = url.lower().endswith(".pdf")

                    # Build abstract context:
                    #   priority 1 — the para text itself (if ≥100 chars)
                    #   priority 2 — accumulated descriptions from this section
                    #   priority 3 — augmented with section header prefix
                    if len(para_text) >= 100:
                        abs_ctx = para_text
                    elif desc_acc:
                        joined = " ".join(desc_acc)
                        abs_ctx = (
                            joined if len(joined) >= 100
                            else f"{category}: {joined}"
                        )
                    else:
                        abs_ctx = (
                            f"{category}: {para_text}" if para_text else ""
                        )

                    title = self._build_title(para_text, href, category)
                    pub_date = self._date_from_url(href)

                    orig_filename = None
                    if is_pdf:
                        fname = url.split("/")[-1].split("?")[0]
                        if fname:
                            orig_filename = fname

                    candidates.append({
                        "external_id":       href.replace(" ", "_"),
                        "_href":             href,
                        "post_number":       None,
                        "title":             title,
                        "_abs_ctx":          abs_ctx,
                        "category":          category,
                        "url":               url,
                        "pdf_url":           url if is_pdf else None,
                        "original_filename": orig_filename,
                        "published_date":    pub_date,
                    })

                # Reset desc_acc after each link paragraph so descriptions
                # don't bleed across unrelated document entries.
                desc_acc = []

        return candidates

    def _resolve_abstract(self, cand: dict) -> str:
        """Return the best available abstract for a candidate.

        If the pre-computed context is already ≥100 chars, return it directly.
        Otherwise, fetch the detail page (HTML only) and use its body text when
        longer.  Always returns whatever is available (may be < 100 chars; the
        caller decides whether to skip).
        """
        ctx = cand.get("_abs_ctx", "")
        if len(ctx) >= 100:
            return ctx

        url = cand.get("url", "")
        if url and not url.lower().endswith(".pdf"):
            status, page_html = _curl_fetch(url)
            if status and page_html:
                body_text = _extract_main_text(page_html)
                if len(body_text) > len(ctx):
                    return body_text

        return ctx

    @classmethod
    def _build_title(cls, link_text: str, href: str, category: str) -> str:
        """Return a human-friendly title for the document."""
        text = link_text.strip()

        for short, long_title in cls._TITLE_MAP.items():
            if short in text:
                return long_title

        if len(text) >= 15:
            return text

        # Derive from URL path segment
        path = href.split("?")[0].split("/")[-1]
        path = path.replace("-", " ").replace("_", " ").replace(".pdf", "").strip()
        if len(path) > 3:
            return path.title()

        return category.title()

    @staticmethod
    def _date_from_url(href: str) -> str:
        """Extract a YYYY-MM-DD date string from a URL if possible."""
        m = re.search(r"/(\d{4})[-/](\d{2})(?:/|[-_]|\b)", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"
        m = re.search(r"/(\d{4})/", href)
        if m:
            yr = int(m.group(1))
            if 1990 <= yr <= 2030:
                return f"{yr}-01-01"
        return None
