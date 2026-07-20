# -*- coding: utf-8 -*-
"""EMEP MSC-W Publications crawler.

Target: https://emep.int/mscw/mscw_publications.html

All publications appear on a single static HTML page — no pagination, no API.
Entries are grouped by year inside <a name="YYYY"> anchors, each publication
wrapped in a <dl>/<dt>/<dd> block.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402


# ---------------------------------------------------------------------------
# Module-level text helpers
# ---------------------------------------------------------------------------

def _iter_text_skip(tag, skip_names):
    """Yield raw text nodes from *tag*, skipping subtrees whose root name is in *skip_names*.

    Checks ``child.name is not None`` to distinguish Tag objects from
    NavigableString objects (both have a ``name`` attribute, but
    NavigableString.name is always None and has no ``.children``).
    """
    for child in tag.children:
        if hasattr(child, "name") and child.name is not None:
            if child.name in skip_names:
                continue
            yield from _iter_text_skip(child, skip_names)
        else:
            yield str(child)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


class EMEPMSCWCrawler(BaseCrawler):
    """Crawler for EMEP MSC-W Publications (single static HTML page)."""

    site_id = "emep-int-mscw"
    site_name = "Custom: emep-int-mscw"
    base_url = "https://emep.int"

    _LIST_URL = "https://emep.int/mscw/mscw_publications.html"
    _PAGE_BASE = "https://emep.int/mscw/"
    _MAX_RUNTIME = 1500  # 25 minutes in seconds

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET via curl with TLS tolerance and exponential back-off."""
        delays = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    ["curl", "-sk", "--tls-max", "1.3", "--max-time", "30", "-L", url],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if raw:
                    return raw.decode("utf-8", errors="replace")
            except Exception as exc:
                if attempt < retries - 1:
                    wait = delays[attempt]
                    print(
                        f"[emep-int-mscw] curl error (attempt {attempt + 1}/{retries}): "
                        f"{exc}; retry in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[emep-int-mscw] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        """Parse HTML with parser fallback chain: html5lib → lxml → html.parser."""
        from bs4 import BeautifulSoup

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _parse_dl(self, dl, year: str | None) -> dict | None:
        """Parse one <dl> publication block into a paper dict.

        A typical block looks like::

            <dl>
              <dt>MSC-W Technical Report 1/2025</dt>
              <dd><i>"Subtitle…"</i><br>
              <dd><i>EMEP Centres Joint Report for HELCOM</i><br>
                <font>Author A, Author B</font><br>
                <a href="…report.pdf">link text</a>
              </dd>
            </dl>

        Some entries have country-report dropdowns (<table>/<form>) and no
        direct PDF link; others are marked "(not available)".
        """
        dt = dl.find("dt")
        if not dt:
            return None
        title = _clean(dt.get_text(separator=" "))
        if not title:
            return None

        dds = dl.find_all("dd")

        subtitle_parts: list[str] = []
        extra_parts: list[str] = []
        pdf_url: str | None = None
        page_url: str | None = None

        # Tags whose subtrees are excluded from extra_parts extraction.
        # We skip <i> separately (captured in subtitle_parts), tables/forms
        # (country dropdowns), links (captured as pdf_url/page_url), and <br>.
        _EXTRA_SKIP = {"a", "table", "form", "select", "option", "i", "br"}

        for dd in dds:
            # 1. Italic text → subtitle / report-type description
            for i_tag in dd.find_all("i"):
                text = _clean(i_tag.get_text(separator=" "))
                if text and text not in subtitle_parts:
                    subtitle_parts.append(text)

            # 2. Links → pdf_url or page_url
            for a_tag in dd.find_all("a", href=True):
                href = a_tag["href"]
                if not href.startswith("http"):
                    href = urljoin(self._PAGE_BASE, href)
                lower = href.lower()
                if lower.endswith(".pdf"):
                    pdf_url = pdf_url or href
                elif (
                    "emep.int" in href
                    and "publ" in href
                    and not any(lower.endswith(e) for e in (".png", ".jpg", ".gif"))
                ):
                    page_url = page_url or href

            # 3. Non-italic, non-link direct children → extra text (authors, org, notes)
            for child in dd.children:
                if hasattr(child, "name") and child.name is not None:
                    # Tag node
                    if child.name in ("i", "a", "table", "form", "br", "p", "script"):
                        continue
                    text = _clean(" ".join(_iter_text_skip(child, _EXTRA_SKIP)))
                    if text and text not in subtitle_parts and text not in extra_parts:
                        extra_parts.append(text)
                else:
                    # NavigableString (inline text between tags, e.g. "(Russian translation: …)")
                    text = _clean(str(child).strip("() \t\n\r"))
                    if len(text) > 3 and text not in extra_parts:
                        extra_parts.append(text)

        # Build abstract from subtitle + extra
        all_parts = [p for p in subtitle_parts + extra_parts if p]
        abstract = "\n\n".join(all_parts)
        abstract = re.sub(r"\n{3,}", "\n\n", abstract).strip()

        # Resolve URL (prefer PDF, then HTML page, then synthetic anchor)
        url = page_url or pdf_url
        if not url:
            slug = re.sub(r"\W+", "_", title[:60]).strip("_")
            url = f"{self._LIST_URL}#{slug}"

        # Infer year from PDF path when the section header was missing
        if not year and pdf_url:
            m = re.search(r"/(\d{4})/", pdf_url)
            if m:
                year = m.group(1)

        published_date = f"{year}-01-01" if year else None

        # original_filename / external_id
        original_filename: str | None = None
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1]

        if original_filename:
            external_id = re.sub(r"\.pdf$", "", original_filename, flags=re.IGNORECASE)
        else:
            external_id = re.sub(r"\W+", "_", title[:80]).strip("_")

        # Category from title keywords
        tl = title.lower()
        if "status report" in tl:
            category = "Status Report"
        elif "technical report" in tl:
            category = "Technical Report"
        elif "data note" in tl:
            category = "Data Note"
        elif "note" in tl:
            category = "Note"
        elif "report" in tl:
            category = "Report"
        else:
            category = ""

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": category,
            "authors": "; ".join(extra_parts) if extra_parts else None,
            "publisher": "EMEP MSC-W; The Norwegian Meteorological Institute",
            "keywords": None,
            "doi": None,
            "metadata": json.dumps(
                {
                    "year": year,
                    "category": category,
                    "post_number": external_id,
                    "subtitle": subtitle_parts,
                    "extra": extra_parts,
                },
                ensure_ascii=False,
            ),
        }

    def _parse_page(self, html: str) -> list[dict]:
        """Parse the publications HTML page into a flat list of paper dicts."""
        soup = self._make_soup(html)
        if soup is None:
            print("[emep-int-mscw] Failed to parse HTML with all available parsers")
            return []

        entries: list[dict] = []
        current_year: str | None = None

        for tag in soup.find_all(True):
            # Track year section headers: <a name="2025">
            if tag.name == "a" and re.match(r"^\d{4}$", tag.get("name", "")):
                current_year = tag.get("name")
                continue

            # Process top-level <dl> blocks only
            if tag.name == "dl":
                if tag.find_parent("dl"):
                    continue  # skip nested <dl> (none expected, safety check)
                try:
                    entry = self._parse_dl(tag, current_year)
                    if entry:
                        entries.append(entry)
                except Exception as exc:
                    print(f"[emep-int-mscw] dl parse error: {exc}")

        return entries

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Fetch the EMEP MSC-W publications page and save papers up to *limit*.

        Since all content lives on a single HTML page there is no real
        pagination.  Progress is logged every 10 items to match the
        pagination-style contract in the spec.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        print(f"[emep-int-mscw] Fetching {self._LIST_URL}")
        html = self._curl_get(self._LIST_URL)
        if not html:
            print("[emep-int-mscw] Failed to fetch publications page; aborting.")
            return 0

        entries = self._parse_page(html)
        print(
            f"[emep-int-mscw] Parsed {len(entries)} publications; "
            f"limit={limit_str}"
        )

        for i, entry in enumerate(entries):
            # Honour limit
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget
            if time.time() - start_time > self._MAX_RUNTIME:
                print("[emep-int-mscw] Approaching 25-minute budget; stopping.")
                break

            # Progress log every 10 items (simulates per-page logging)
            if i > 0 and i % 10 == 0:
                print(
                    f"[emep-int-mscw] page 1: saved {saved}/{limit_str} "
                    f"({i}/{len(entries)} entries processed)"
                )

            # URL deduplication
            dedup_key = entry.get("pdf_url") or entry.get("url") or ""
            if dedup_key in seen_urls:
                print(f"[emep-int-mscw] Duplicate, skipping: {dedup_key[:80]}")
                continue
            if dedup_key:
                seen_urls.add(dedup_key)

            # Skip items with no useful abstract
            abstract = entry.get("abstract") or ""
            if len(abstract) < 50:
                print(
                    f"[emep-int-mscw] Short abstract ({len(abstract)} chars), "
                    f"skipping: {entry.get('title', '')[:60]}"
                )
                continue

            try:
                self._save_paper(entry)
                saved += 1
                print(
                    f"[emep-int-mscw] Saved {saved}/{limit_str}: "
                    f"{entry.get('title', '')[:60]}"
                )
            except Exception as exc:
                print(
                    f"[emep-int-mscw] Save error for "
                    f"'{entry.get('title', '')[:40]}': {exc}"
                )
                continue

        print(f"[emep-int-mscw] Done. Total saved: {saved}")
        return saved
