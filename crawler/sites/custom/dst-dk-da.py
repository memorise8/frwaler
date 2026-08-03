# -*- coding: utf-8 -*-
"""Danmarks Statistik (DST) publications crawler.

Target: https://www.dst.dk/da/Statistik/udgivelser?pub=pub
List pagination: ?pub=pub&page={n}  (20 items/page, ~87 pages)
Detail URL:      https://www.dst.dk/pubomtale/{pub_id}
PDF URL:         https://www.dst.dk/pubfile/{pub_id}/{filename}
"""

import json
import os
import re
import subprocess
import time
from html import unescape

from crawler.base_crawler import BaseCrawler

_MONTHS_DA = {
    "januar": "01", "februar": "02", "marts": "03", "april": "04",
    "maj": "05", "juni": "06", "juli": "07", "august": "08",
    "september": "09", "oktober": "10", "november": "11", "december": "12",
}


class DstDkDaCrawler(BaseCrawler):
    """Crawler for Danmarks Statistik publications (da)."""

    site_id = "dst-dk-da"
    site_name = "Custom: dst-dk-da"
    base_url = "https://www.dst.dk"

    _LIST_URL = "https://www.dst.dk/da/Statistik/udgivelser?pub=pub&page={page}"
    _DETAIL_URL = "https://www.dst.dk/pubomtale/{pub_id}"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """Fetch URL via curl with 3-attempt exponential backoff (1s, 3s, 9s)."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.8",
            url,
        ]
        delays = [1, 3, 9]
        for attempt, delay in enumerate(delays):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                print(f"[{self.site_id}] empty response (attempt {attempt + 1}/3): {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}")
            if attempt < 2:
                time.sleep(delay)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(raw: str) -> str:
        """Strip tags, decode entities, collapse whitespace."""
        text = re.sub(r"<[^>]+>", " ", raw)
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_iso_date(raw: str) -> str:
        """Parse 'DD-MM-YYYY HH:MM' or 'DD. <month> YYYY' → 'YYYY-MM-DD'."""
        if not raw:
            return ""
        # Format: "23-03-2026 08:00"
        m = re.match(r"(\d{2})-(\d{2})-(\d{4})", raw.strip())
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        # Format: "23. marts 2026 kl. 08:00"
        m = re.match(r"(\d{1,2})\.\s+(\w+)\s+(\d{4})", raw.strip(), re.I)
        if m:
            day = m.group(1).zfill(2)
            mon = _MONTHS_DA.get(m.group(2).lower(), "01")
            return f"{m.group(3)}-{mon}-{day}"
        return ""

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[tuple[str, str]]:
        """Return [(pub_id, title), ...] from publications list page."""
        results = []
        for m in re.finditer(r'href="/pubomtale/(\d+)"[^>]*>([^<]+)<', html):
            pub_id = m.group(1)
            title = unescape(m.group(2).strip())
            if pub_id and title:
                results.append((pub_id, title))
        return results

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _extract_abstract(self, html: str) -> str:
        """Extract full description text from detail page.

        Scans all <p> tags in the showPubContainer section (which may
        span a large base64 image block), filters out navigation and JS
        fragments, and joins the rest.
        """
        # Start from showPubContainer; fall back to full page
        idx = html.find("showPubContainer")
        chunk = html[idx:] if idx >= 0 else html

        # Try BeautifulSoup first for robustness
        try:
            for parser in ("html5lib", "lxml", "html.parser"):
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(chunk[:500_000], parser)
                    break
                except Exception:
                    soup = None
                    continue
            if soup:
                parts = []
                for p in soup.find_all("p"):
                    text = p.get_text(separator=" ", strip=True)
                    if (len(text) >= 20
                            and "function" not in text
                            and "jQuery" not in text
                            and "Gå til oversigt" not in text):
                        parts.append(text)
                if parts:
                    return " ".join(parts)
        except Exception:
            pass

        # Regex fallback
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        parts = []
        for p in paragraphs:
            text = self._strip_html(p)
            if (len(text) >= 20
                    and "function" not in text
                    and "jQuery" not in text
                    and "Gå til oversigt" not in text):
                parts.append(text)
        return " ".join(parts)

    def _parse_detail(self, html: str, pub_id: str) -> dict | None:
        """Parse a publication detail page and return a paper dict."""
        # --- Title ---
        m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        if not m:
            m = re.search(r'og:title"[^>]+content="([^"]+)"', html)
        title = unescape(m.group(1)) if m else ""

        # --- og:description (short fallback abstract) ---
        m = re.search(r'property="og:description"\s+content="([^"]+)"', html)
        if not m:
            m = re.search(r'og:description"[^>]+content="([^"]+)"', html)
        og_desc = unescape(m.group(1)) if m else ""

        # --- Full abstract ---
        abstract = self._extract_abstract(html) or og_desc

        # --- Published/listed date ---
        # Primary: meta cludo:DstPubReleaseDateTime ("23-03-2026 08:00")
        m = re.search(r'DstPubReleaseDateTime"[^>]+content="([^"]+)"', html)
        if not m:
            # Fallback: visible release date div ("13. december 2024")
            m = re.search(r'publication__RelDate[^>]*>([^<]+)<', html)
        date_raw = m.group(1).strip() if m else ""
        pub_date = self._parse_iso_date(date_raw)

        # --- Category / publication type ---
        m = re.search(r'DstPubType"[^>]+content="([^"]+)"', html)
        category = m.group(1).strip() if m else ""
        if not category:
            m = re.search(r'pageType__Label[^>]*>([^<]+)<', html)
            category = m.group(1).strip() if m else ""

        # --- PDF URL (two HTML patterns) ---
        # Pattern 1: <a class="show-icon-download" href="/pubfile/...">
        m = re.search(r'show-icon-download[^>]+href="(/pubfile/[^"]+)"', html)
        if not m:
            # Pattern 2: onclick="window.open('/pubfile/...')"
            m = re.search(r"onclick=\"window\.open\('(/pubfile/[^']+)'\)", html)
        pdf_url = None
        original_filename = None
        if m:
            path = m.group(1)
            pdf_url = f"https://www.dst.dk{path}"
            seg = path.rstrip("/").split("/")[-1]
            original_filename = seg if seg.lower().endswith(".pdf") else seg + ".pdf"

        # --- Subject (e.g. "Sociale forhold") ---
        m = re.search(r'Publikationer\?subject=\d+"[^>]*>([^<]+)<', html)
        subject = unescape(m.group(1).strip()) if m else ""

        # --- ISBN (two HTML formats) ---
        isbn = ""
        m = re.search(r"ISBN\s*(?:pdf)?(?:</b>)?:\s*([0-9\-]+)", html, re.I)
        if m:
            isbn = m.group(1).strip()

        # --- Page count ---
        pages = ""
        m = re.search(r"Antal sider(?:</b>)?:\s*(\d+)", html)
        if m:
            pages = m.group(1)

        # --- Series / publication series name ---
        series = ""
        # New format: <b>Titel</b>: Kriminalitet
        m = re.search(r"<b>Titel</b>:\s*([^<]+)<", html)
        if m:
            series = unescape(m.group(1).strip())
        if not series:
            # Old format: plain text after <h4>Kolofon</h4>
            idx = html.find("Kolofon</h4>")
            if idx >= 0:
                kol = re.sub(r"<[^>]+>", " ", html[idx:idx + 400])
                kol = unescape(re.sub(r"\s+", " ", kol))
                lines = [ln.strip() for ln in kol.split("  ") if ln.strip() and ln.strip() != "Kolofon"]
                if lines:
                    series = lines[0]

        url = f"https://www.dst.dk/pubomtale/{pub_id}"
        return {
            "site_id": self.site_id,
            "external_id": pub_id,
            "post_number": pub_id,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date,
            "listed_date": pub_date,
            "posted_date": pub_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": "Danmarks Statistik",
            "keywords": subject,
            "authors": None,
            "journal": None,
            "department": None,
            "doi": None,
            "metadata": json.dumps({
                "posted_date": date_raw,
                "originalFilename": original_filename,
                "isbn_pdf": isbn,
                "pages": pages,
                "subject": subject,
                "series": series,
                "pub_type": category,
                "category": category,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl DST publications list and detail pages.

        Walks pages until limit is reached, no new items are found, or the
        200-page safety cap is hit. Wall-clock budget: 25 minutes.
        """
        saved = 0
        seen_ids: set[str] = set()
        limit_or_inf = limit if limit is not None else "∞"
        start_time = time.time()

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute wall-clock budget exceeded. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._LIST_URL.format(page=page)
            html = self._curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] page {page}: failed to fetch list, stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{self.site_id}] page {page}: no items found. Done.")
                break

            # URL deduplication — detect infinite pagination loops
            new_items = [(pid, ttl) for pid, ttl in items if pid not in seen_ids]
            if not new_items:
                print(f"[{self.site_id}] page {page}: all items already seen. Done.")
                break
            for pid, _ in new_items:
                seen_ids.add(pid)

            for pub_id, list_title in new_items:
                if limit is not None and saved >= limit:
                    break

                time.sleep(self._delay)

                try:
                    detail_html = self._curl_get(
                        self._DETAIL_URL.format(pub_id=pub_id)
                    )
                    if not detail_html:
                        print(f"[{self.site_id}] {pub_id}: detail fetch failed, skipping.")
                        continue

                    paper = self._parse_detail(detail_html, pub_id)
                    if paper is None:
                        print(f"[{self.site_id}] {pub_id}: parse returned None, skipping.")
                        continue

                    # Use list-page title as fallback
                    if not paper.get("title"):
                        paper["title"] = list_title

                    abstract = paper.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] {pub_id}: abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                        f"{paper['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] {pub_id} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Reached {self._MAX_PAGES}-page safety cap. Stopping.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
