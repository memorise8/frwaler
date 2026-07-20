# -*- coding: utf-8 -*-
"""CRE (Commission de Régulation de l'Énergie) Open Data crawler.

Starting URL: https://www.cre.fr/documents/open-data.html

Discovery strategy:
  1. Atom feed (https://www.cre.fr/documents/open-data/feed.atom) — 25 entries
     with TYPO3 page UIDs used as external_id / post_number.
  2. HTML listing pages — catches items absent from the feed.
  3. Per-item detail-page fetch for full abstract and file links.
"""

from __future__ import annotations

import html as htmlmod
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from crawler.base_crawler import BaseCrawler

_SITE_ID = "cre-fr-documents"
_BASE_URL = "https://www.cre.fr"
_ATOM_FEED_URL = "https://www.cre.fr/documents/open-data/feed.atom"
_LIST_URL = "https://www.cre.fr/documents/open-data.html"

# Regex to skip non-content elements in detail page articles
_SKIP_RE = re.compile(
    r"^(?:Mis\s+[àa]\s+jour|Page\s+mise\s+[àa]\s+jour|Partager\s+sur"
    r"|Abonnez-vous|Restez\s+inform|©\s*Copyright"
    r"|Télécharger\s+les\s+fichiers)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Module-level helpers (no class dependency)
# ---------------------------------------------------------------------------

def _curl_get(url: str, max_retries: int = 3, timeout: int = 30) -> str | None:
    """Fetch *url* via curl with exponential backoff (1 s, 3 s, 9 s)."""
    delays = [1, 3, 9]
    for attempt in range(max_retries):
        try:
            cmd = [
                "curl", "--tls-max", "1.3", "-skL",
                "--max-time", str(timeout),
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
                "-H", (
                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/{max_retries} for {url}: {exc}")
        if attempt < max_retries - 1:
            time.sleep(delays[attempt])
    return None


def _parse_date(raw: str | None) -> str | None:
    """Normalise various date strings to ISO YYYY-MM-DD.

    Handles ``YYYY-MM-DDT...`` (Atom), ``DD/MM/YYYY`` (French site).
    """
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def _clean(html_str: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = htmlmod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class CreFrDocumentsCrawler(BaseCrawler):
    """Crawler for CRE Open Data (Commission de Régulation de l'Énergie)."""

    site_id = _SITE_ID
    site_name = "Custom: cre-fr-documents"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Atom feed
    # ------------------------------------------------------------------

    def _fetch_atom_entries(self) -> list[dict]:
        """Return a list of entry dicts from the Atom feed (newest-first)."""
        raw = _curl_get(_ATOM_FEED_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch Atom feed")
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            print(f"[{self.site_id}] Atom XML parse error: {exc}")
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries: list[dict] = []
        for entry in root.findall("atom:entry", ns):
            try:
                eid_el = entry.find("atom:id", ns)
                eid = (eid_el.text or "") if eid_el is not None else ""
                # TYPO3 feed IDs look like "…feed.atom:pages:247"
                page_id: str | None = eid.rsplit(":", 1)[-1] if ":" in eid else None
                if page_id and not page_id.isdigit():
                    page_id = None

                title_el = entry.find("atom:title", ns)
                title = (title_el.text or "").strip() if title_el is not None else ""

                updated_el = entry.find("atom:updated", ns)
                updated = _parse_date(
                    (updated_el.text or "") if updated_el is not None else ""
                )

                content_el = entry.find("atom:content", ns)
                feed_abstract = (
                    (content_el.text or "").strip() if content_el is not None else ""
                )

                link_el = entry.find("atom:link", ns)
                link = link_el.get("href", "") if link_el is not None else ""
                if link and not link.startswith("http"):
                    link = _BASE_URL + link

                if link:
                    entries.append({
                        "url": link,
                        "page_id": page_id,
                        "title": title,
                        "updated": updated,
                        "feed_abstract": feed_abstract,
                    })
            except Exception as exc:
                print(f"[{self.site_id}] Atom entry parse error: {exc}")
        return entries

    # ------------------------------------------------------------------
    # HTML listing
    # ------------------------------------------------------------------

    def _fetch_listing_urls(self, page_num: int = 1) -> list[str]:
        """Scrape one listing page and return unique absolute doc URLs.

        Returns an empty list when Cloudflare blocks the request or the
        page has no document links.
        """
        url = (
            _LIST_URL
            if page_num == 1
            else f"{_LIST_URL}?tx_solr%5Bpage%5D={page_num}"
        )
        raw = _curl_get(url)
        if not raw:
            return []
        # Cloudflare challenge page
        if "Just a moment" in raw[:2000]:
            print(f"[{self.site_id}] Listing page {page_num}: Cloudflare block")
            return []

        links = re.findall(r'href="(/documents/open-data/[^"#]+\.html)"', raw)
        seen: set[str] = set()
        result: list[str] = []
        for rel in links:
            # Must be exactly 3 slashes deep (not the listing page itself)
            if rel.count("/") != 3 or rel == "/documents/open-data.html":
                continue
            abs_url = _BASE_URL + rel
            if abs_url not in seen:
                seen.add(abs_url)
                result.append(abs_url)
        return result

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict | None:
        """Fetch and parse one detail page.

        Returns a dict with keys:
          title, listed_date, categories, abstract, file_links,
          pdf_url, original_filename
        or None on fetch failure.
        """
        raw = _curl_get(url)
        if not raw:
            return None

        # ---- Title (H1) ----
        title = ""
        h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.DOTALL)
        if h1_m:
            title = _clean(h1_m.group(1))
        if not title:
            og_m = re.search(
                r'property="og:title"\s+content="([^"]+)"', raw
            )
            if og_m:
                title = htmlmod.unescape(og_m.group(1))

        # ---- Date ("Mis à jour : DD/MM/YYYY") ----
        listed_date: str | None = None
        date_m = re.search(
            r"Mis\s+[àa]\s+jour\s*:.*?(\d{2}/\d{2}/\d{4})", raw, re.DOTALL
        )
        if date_m:
            listed_date = _parse_date(date_m.group(1))

        # ---- Article body → abstract + categories + files ----
        abstract_parts: list[str] = []
        categories: list[str] = []
        file_links: list[str] = []

        # Try BeautifulSoup with fallback chain: html5lib → lxml → html.parser
        soup = None
        try:
            from bs4 import BeautifulSoup  # type: ignore
            for parser in ("html5lib", "lxml", "html.parser"):
                try:
                    soup = BeautifulSoup(raw, parser)
                    break
                except Exception:
                    continue
        except ImportError:
            pass

        if soup is not None:
            try:
                article = soup.find("article")
                if article:
                    first_p_done = False
                    for elem in article.find_all(["p", "h2", "h3"]):
                        text = re.sub(
                            r"\s+", " ",
                            elem.get_text(separator=" ", strip=True)
                        )
                        if not text or len(text) < 5:
                            continue
                        if _SKIP_RE.search(text):
                            continue
                        # First <p> is the category tag line (short, e.g.
                        # "Open Data  Marchés de détail  Gaz")
                        if (
                            not first_p_done
                            and elem.name == "p"
                            and len(text) < 120
                        ):
                            categories = [
                                c.strip()
                                for c in re.split(r"\s{2,}", text)
                                if c.strip()
                            ]
                            first_p_done = True
                            continue
                        first_p_done = True
                        if len(text) >= 15:
                            abstract_parts.append(text)

                    for a_tag in article.find_all("a", href=True):
                        href: str = a_tag["href"]
                        if "/fileadmin/" in href:
                            abs_href = (
                                _BASE_URL + href
                                if not href.startswith("http")
                                else href
                            )
                            if abs_href not in file_links:
                                file_links.append(abs_href)
            except Exception as exc:
                print(
                    f"[{self.site_id}] BS4 parse error for {url}: {exc}"
                )
                abstract_parts = []

        # Regex fallback when BS4 is unavailable or failed
        if not abstract_parts:
            art_m = re.search(
                r"<article[^>]*>(.*?)</article>", raw, re.DOTALL
            )
            body = art_m.group(1) if art_m else raw
            paras = re.findall(r"<p[^>]*>(.*?)</p>", body, re.DOTALL)
            for i, p_html in enumerate(paras):
                text = _clean(p_html)
                if len(text) < 15:
                    continue
                # First paragraph is the category line
                if i == 0 and len(text) < 120:
                    categories = [
                        c.strip()
                        for c in re.split(r"\s{2,}", text)
                        if c.strip()
                    ]
                    continue
                if _SKIP_RE.search(text):
                    continue
                abstract_parts.append(text)

        if not file_links:
            file_links = [
                (_BASE_URL + f if not f.startswith("http") else f)
                for f in re.findall(r'href="(/fileadmin/[^"]+)"', raw)
            ]
        # Deduplicate file links while preserving order
        seen_fl: set[str] = set()
        file_links = [
            fl for fl in file_links
            if fl not in seen_fl and not seen_fl.add(fl)  # type: ignore[func-returns-value]
        ]

        abstract = "\n\n".join(abstract_parts)

        # Primary download link (may be XLSX/CSV/ZIP — not always a PDF)
        pdf_url = file_links[0] if file_links else None
        original_filename = (
            pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if pdf_url
            else None
        )

        return {
            "title": title,
            "listed_date": listed_date,
            "categories": categories,
            "abstract": abstract,
            "file_links": file_links,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:  # noqa: C901
        """Crawl CRE Open Data pages and persist documents to the DB.

        Collects URLs from the Atom feed (newest-first, with TYPO3 page IDs)
        and the HTML listing pages (catches items absent from the feed), then
        fetches each detail page for a full abstract and file links.

        Parameters
        ----------
        limit:
            Maximum number of documents to save.  ``None`` means no limit.
        """
        start_time = time.time()
        max_wall_secs = 25 * 60  # 25-minute hard budget
        limit_disp = str(limit) if limit is not None else "∞"

        # ---- Phase 1: discover all document URLs ----
        print(f"[{self.site_id}] Fetching Atom feed…")
        atom_entries = self._fetch_atom_entries()
        print(f"[{self.site_id}] Atom feed: {len(atom_entries)} entries")

        atom_by_url: dict[str, dict] = {e["url"]: e for e in atom_entries}
        seen_urls: set[str] = set(atom_by_url)

        # Walk HTML listing pages to find items not in the feed.
        extra_urls: list[str] = []
        for page_num in range(1, 201):  # safety cap: 200 pages (log at cap)
            if page_num == 200:
                print(
                    f"[{self.site_id}] Safety cap of 200 listing pages reached"
                )
            listing = self._fetch_listing_urls(page_num)
            if not listing:
                break  # Empty page or Cloudflare block = end of pagination
            newly_added = 0
            for url in listing:
                if url not in seen_urls:
                    seen_urls.add(url)
                    extra_urls.append(url)
                    newly_added += 1
            if page_num % 10 == 0:
                print(
                    f"[{self.site_id}] Listing page {page_num}: "
                    f"{len(extra_urls)} extra URLs so far"
                )
            # All URLs on this page already known → likely looped back to page 1
            if newly_added == 0 and page_num > 1:
                break

        if extra_urls:
            print(
                f"[{self.site_id}] HTML listing: {len(extra_urls)} additional URLs"
            )

        # Merged list: Atom-feed order (newest-first) then extras
        all_items: list[dict] = list(atom_entries)
        for url in extra_urls:
            all_items.append({
                "url": url,
                "page_id": None,
                "title": None,
                "updated": None,
                "feed_abstract": "",
            })

        print(
            f"[{self.site_id}] Total unique documents to process: {len(all_items)}"
        )

        # ---- Phase 2: fetch each detail page ----
        saved = 0
        seen_detail: set[str] = set()

        for idx, item in enumerate(all_items):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > max_wall_secs:
                print(
                    f"[{self.site_id}] 25-minute wall-clock budget reached, stopping."
                )
                break

            url: str = item["url"]
            if url in seen_detail:
                continue
            seen_detail.add(url)

            if (idx + 1) % 10 == 0:
                print(
                    f"[{self.site_id}] page {idx + 1}: "
                    f"saved {saved}/{limit_disp}"
                )

            time.sleep(self._delay)

            try:
                detail = self._fetch_detail(url)
                if not detail:
                    print(
                        f"[{self.site_id}] item {idx + 1} fetch failed: {url}"
                    )
                    continue

                # Title: detail page H1 → fallback to Atom feed title
                title: str = detail["title"] or item.get("title") or ""
                if not title:
                    print(
                        f"[{self.site_id}] item {idx + 1} has no title, "
                        f"skipping: {url}"
                    )
                    continue

                # Abstract: detail paragraphs, supplemented by feed description
                abstract: str = detail["abstract"]
                feed_abstract: str = item.get("feed_abstract") or ""
                if len(abstract) < 100 and feed_abstract:
                    if feed_abstract not in abstract:
                        abstract = (
                            (feed_abstract + "\n\n" + abstract).strip()
                            if abstract
                            else feed_abstract
                        )

                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] '{title[:40]}' abstract too short "
                        f"({len(abstract)} chars), skipping"
                    )
                    continue

                # external_id: numeric TYPO3 page UID from Atom feed
                # or URL slug for HTML-listing-only items
                page_id: str | None = item.get("page_id")
                if page_id and str(page_id).isdigit():
                    external_id = str(page_id)
                    post_number: str | None = str(page_id)
                else:
                    slug = re.sub(
                        r"\.html$", "", url.rstrip("/").split("/")[-1]
                    )
                    external_id = slug
                    post_number = None

                listed_date: str | None = (
                    detail["listed_date"] or item.get("updated")
                )
                published_date: str | None = (
                    item.get("updated") or listed_date
                )

                category = (
                    " > ".join(detail["categories"])
                    if detail["categories"]
                    else "Open Data"
                )

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "url": url,
                    "pdf_url": detail["pdf_url"],
                    "original_filename": detail["original_filename"],
                    "published_date": published_date,
                    "listed_date": listed_date,
                    "authors": None,
                    "publisher": "Commission de Régulation de l'Énergie (CRE)",
                    "department": None,
                    "journal": None,
                    "keywords": None,
                    "category": category,
                    "doi": None,
                    "metadata": json.dumps(
                        {
                            "posted_date": detail["listed_date"],
                            "originalFilename": detail["original_filename"],
                            "file_links": detail["file_links"],
                            "atom_page_id": page_id,
                            "category": category,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(
                    f"[{self.site_id}] Saved {saved}/{limit_disp}: "
                    f"{title[:60]}"
                )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx + 1} error: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
