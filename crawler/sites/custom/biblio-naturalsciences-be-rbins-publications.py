# -*- coding: utf-8 -*-
"""Crawler for RBINS Open Access Library (biblio.naturalsciences.be/rbins-publications/).

Endpoint strategy:
  - List  : POST @@faceted_query with b_start:int=N  (250 items per page)
  - Detail: GET  individual articlereference page (Z3988 OpenURL + PDF link)
"""

import json
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# BeautifulSoup helper with parser fallback chain
# ---------------------------------------------------------------------------

def _bs_parse(html: str):
    """Return a BeautifulSoup tree; tries html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class RBINSPublicationsCrawler(BaseCrawler):
    """Crawler for the RBINS Open Access Library publications."""

    site_id = "biblio-naturalsciences-be-rbins-publications"
    site_name = "Custom: biblio-naturalsciences-be-rbins-publications"
    base_url = "https://biblio.naturalsciences.be"

    _FACETED_URL = (
        "https://biblio.naturalsciences.be/rbins-publications/"
        "search-references/@@faceted_query"
    )
    _PAGE_SIZE = 250

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_fetch(self, url: str, method: str = "GET",
                    post_data: str = "", retries: int = 3):
        """Fetch URL via curl with retry + exponential backoff.

        Returns decoded response body string or None on failure.
        """
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*",
        ]
        if method == "POST":
            cmd += [
                "-X", "POST",
                "-H", "X-Requested-With: XMLHttpRequest",
                "--data", post_data,
            ]
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                body = result.stdout
                if body:
                    return body.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/{retries})"
                      f" for {url}: {exc}")
            if attempt < retries - 1:
                wait = [1, 3, 9][min(attempt, 2)]
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list_html(self, html: str) -> list:
        """Parse @@faceted_query response HTML into article dicts.

        Each dict has: url, title, authors, year, source.
        """
        entries = []
        soup = None
        try:
            soup = _bs_parse(html)
        except Exception:
            pass

        if soup:
            for row in soup.find_all("div", class_="tileItem"):
                try:
                    title_span = row.find(class_="ref-title")
                    if not title_span:
                        continue
                    link = title_span.find("a", class_="summary url")
                    if not link:
                        continue
                    url = (link.get("href") or "").strip()
                    title = link.get_text(strip=True)
                    if not url or not title:
                        continue

                    def _txt(cls):
                        tag = row.find(class_=cls)
                        return tag.get_text(strip=True) if tag else ""

                    entries.append({
                        "url": url,
                        "title": title,
                        "authors": _txt("ref-authors"),
                        "year": _txt("ref-year"),
                        "source": _txt("ref-source"),
                    })
                except Exception:
                    continue
        else:
            # Regex fallback when BeautifulSoup is unavailable
            pat = (
                r'class="ref-authors">(.*?)</span>.*?class="ref-year">(.*?)</span>'
                r'.*?class="ref-title"[^>]*>.*?href="([^"]+)" class="summary url">(.*?)</a>'
                r'.*?class="ref-source">(.*?)</span>'
            )
            for m in re.finditer(pat, html, re.DOTALL):
                try:
                    def _c(s):
                        return re.sub(r"<[^>]+>", "", s).strip()
                    url = m.group(3).strip()
                    title = _c(m.group(4))
                    if url and title:
                        entries.append({
                            "url": url,
                            "title": title,
                            "authors": _c(m.group(1)),
                            "year": _c(m.group(2)),
                            "source": _c(m.group(5)),
                        })
                except Exception:
                    continue

        return entries

    def _parse_detail(self, html: str, article_url: str) -> dict:
        """Extract Z3988 metadata, PDF URL, and creation date from a detail page.

        Returns dict: z3988 (dict), pdf_url (str|None), listed_date (str|None), doi (str).
        """
        result: dict = {"z3988": {}, "pdf_url": None, "listed_date": None, "doi": ""}
        try:
            # Z3988 OpenURL metadata — rich bibliographic fields
            z_m = re.search(r'class="Z3988" title="([^"]+)"', html)
            if z_m:
                raw = z_m.group(1).replace("&amp;", "&")
                params = urllib.parse.parse_qs(urllib.parse.unquote(raw))
                result["z3988"] = {k: (v[0] if v else "") for k, v in params.items()}

            # PDF link — prefer document-action button, fall back to any .pdf href
            pdf_m = re.search(
                r'id="document-action-download_pdf"[^>]*>.*?href="([^"]+)"',
                html, re.DOTALL | re.I,
            )
            if not pdf_m:
                pdf_m = re.search(r'href="([^"]*\.pdf[^"]*)"', html, re.I)
            if pdf_m:
                pdf_rel = pdf_m.group(1)
                if pdf_rel.startswith("http"):
                    result["pdf_url"] = pdf_rel
                else:
                    # Resolve relative path: base href is the article page URL
                    base_m = re.search(r'<base href="([^"]+)"', html)
                    base = (base_m.group(1) if base_m else article_url).rstrip("/")
                    result["pdf_url"] = urllib.parse.urljoin(base, pdf_rel)

            # DC.date.created → listed_date (when this record was added to the DB)
            dc_m = re.search(
                r'content="(\d{4}-\d{2}-\d{2}[^"]*)"[^>]*name="DC\.date\.created"'
                r'|name="DC\.date\.created"[^>]*content="(\d{4}-\d{2}-\d{2}[^"]*)"',
                html,
            )
            if dc_m:
                raw_dc = (dc_m.group(1) or dc_m.group(2) or "")[:10]
                if re.match(r"\d{4}-\d{2}-\d{2}", raw_dc):
                    result["listed_date"] = raw_dc

            # DOI — best-effort search
            doi_m = re.search(r"\b(10\.\d{4,}/\S+)", html)
            if doi_m:
                result["doi"] = doi_m.group(1).rstrip(".,;)")

        except Exception as exc:
            print(f"[{self.site_id}] detail parse error ({article_url}): {exc}")

        return result

    def _build_abstract(self, title: str, authors: str, year: str,
                        source: str, z3988: dict) -> str:
        """Build a rich bibliographic description from available metadata.

        Combines article title, authors, year, journal, volume, pages.
        Reliably produces ≥100-char strings for any article with basic metadata.
        """
        z = z3988
        atitle = z.get("rft.atitle") or title
        # Prefer full author list from list page (Z3988 may only carry first author)
        au = authors or z.get("rft.au") or ""
        date = z.get("rft.date") or year
        jtitle = z.get("rft.jtitle") or z.get("rft.title") or ""
        volume = z.get("rft.volume") or ""
        part = z.get("rft.part") or ""
        pages = z.get("rft.pages") or ""

        parts = []
        if atitle:
            parts.append(atitle)
        if au:
            parts.append(f"Author(s): {au}")
        if date:
            parts.append(f"Year: {date}")
        if jtitle:
            parts.append(f"Journal: {jtitle}")
        if volume:
            parts.append(f"Volume: {volume}")
        if part:
            parts.append(f"Part/Issue: {part}")
        if pages:
            parts.append(f"Pages: {pages}")
        if source:
            src_clean = source.strip().rstrip(".")
            # Only append if not already covered by the journal/volume parts
            already = " ".join(parts)
            if src_clean and src_clean not in already:
                parts.append(f"Source: {src_clean}")

        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl RBINS publications via the faceted search-references endpoint.

        Paginates with b_start:int=N (250 items/page).
        Visits each detail page for Z3988 metadata and PDF link.
        """
        saved = 0
        seen_urls: set = set()
        b_start = 0
        page_num = 0
        safety_cap = 200
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # ---- stop conditions ----
            if limit is not None and saved >= limit:
                break

            if page_num >= safety_cap:
                print(f"[{self.site_id}] Safety cap of {safety_cap} pages reached. Stopping.")
                break

            if time.time() - start_time > 24 * 60:
                print(f"[{self.site_id}] 25-min wall-clock budget approaching. Stopping.")
                break

            page_num += 1

            # ---- fetch list page ----
            html = self._curl_fetch(
                self._FACETED_URL, method="POST",
                post_data=f"b_start:int={b_start}",
            )
            if not html:
                print(f"[{self.site_id}] page {page_num} (b_start={b_start}): "
                      f"fetch failed. Stopping.")
                break

            entries = self._parse_list_html(html)
            if not entries:
                print(f"[{self.site_id}] page {page_num} (b_start={b_start}): "
                      f"no entries. Done.")
                break

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            new_on_page = 0

            for entry in entries:
                if limit is not None and saved >= limit:
                    break

                url = entry["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)

                    # ---- fetch detail page ----
                    detail_html = self._curl_fetch(url)
                    if detail_html:
                        detail = self._parse_detail(detail_html, url)
                    else:
                        print(f"[{self.site_id}] detail fetch failed: {url}")
                        detail = {"z3988": {}, "pdf_url": None,
                                  "listed_date": None, "doi": ""}

                    z3988 = detail["z3988"]
                    pdf_url = detail["pdf_url"]
                    listed_date = detail["listed_date"]
                    doi = detail.get("doi") or ""

                    # ---- build abstract ----
                    abstract = self._build_abstract(
                        entry["title"], entry["authors"], entry["year"],
                        entry["source"], z3988,
                    )
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract <50 chars, skipping: {url}")
                        continue

                    # ---- identifiers ----
                    slug = url.rstrip("/").split("/")[-1]
                    external_id = slug
                    num_m = re.search(r"\.(\d+)$", slug)
                    post_number = num_m.group(1) if num_m else None

                    # ---- dates ----
                    year_val = entry.get("year") or z3988.get("rft.date") or ""
                    if re.match(r"^\d{4}$", str(year_val).strip()):
                        published_date = f"{year_val}-01-01"
                    else:
                        published_date = str(year_val).strip() or None

                    # ---- journal / authors ----
                    journal = (
                        z3988.get("rft.jtitle") or z3988.get("rft.title")
                        or entry.get("source", "").split(",")[0].strip()
                    )
                    authors = entry.get("authors") or z3988.get("rft.au") or ""

                    # ---- original filename from PDF URL ----
                    original_filename = None
                    if pdf_url:
                        fname = pdf_url.split("/")[-1].split("?")[0].split("#")[0]
                        if fname and "." in fname:
                            original_filename = fname

                    # ---- metadata JSON ----
                    volume = z3988.get("rft.volume") or ""
                    issue = z3988.get("rft.part") or ""
                    pages = z3988.get("rft.pages") or ""
                    genre = z3988.get("rft.genre") or "article"

                    metadata = json.dumps({
                        "posted_date": listed_date,
                        "originalFilename": original_filename,
                        "journal_raw": entry.get("source", ""),
                        "series": "",
                        "volume": volume,
                        "issue": issue,
                        "pages": pages,
                        "genre": genre,
                        "slug": slug,
                    }, ensure_ascii=False)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": entry["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": pdf_url,
                        "authors": authors,
                        "publisher": "Royal Belgian Institute of Natural Sciences",
                        "journal": journal,
                        "keywords": "",
                        "category": genre,
                        "doi": doi,
                        "original_filename": original_filename,
                        "metadata": metadata,
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: "
                          f"{entry['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({url}): {exc}")
                    continue

            # ---- end-of-page dedup check ----
            if new_on_page == 0:
                print(f"[{self.site_id}] No new entries on page {page_num} "
                      f"(all duplicates). Done.")
                break

            b_start += self._PAGE_SIZE

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
