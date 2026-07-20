# -*- coding: utf-8 -*-
"""Crawler for National Oceanography Centre (NOC) publications.

Scrapes https://www.noc.ac.uk/publications (Drupal 11 HTML listing, ?page=N,
12 items/page) and fetches abstracts from NORA (NERC Open Research Archive,
ePrints-based) via <meta name="eprints.*"> tags.
"""

import html as _html_mod
import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


def _make_soup(raw: str):
    """Build BeautifulSoup with parser fallback: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


class NocAcUkPublicationsCrawler(BaseCrawler):
    """National Oceanography Centre publications crawler."""

    site_id = "noc-ac-uk-publications"
    site_name = "Custom: noc-ac-uk-publications"
    base_url = "https://noc.ac.uk"

    _LIST_URL = "https://www.noc.ac.uk/publications"
    _MAX_PAGES = 200  # safety cap — ~8 500 total records at 12/page

    # ------------------------------------------------------------------ helpers

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential backoff (1 s, 3 s, 9 s)."""
        for attempt in range(3):
            try:
                res = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
                     "-A", self.USER_AGENT, url],
                    capture_output=True,
                    timeout=35,
                )
                text = res.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3) {url}: {exc}")
            if attempt < 2:
                wait = (attempt + 1) ** 2  # 1 s, 4 s
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] Retrying in {wait}s…")
                time.sleep(wait)
        print(f"[{self.site_id}] Failed after 3 attempts: {url}")
        return None

    @staticmethod
    def _eprints_meta(html: str, name: str) -> list[str]:
        """Return all content= values for <meta name="eprints.NAME">."""
        return [
            _html_mod.unescape(v)
            for v in re.findall(
                rf'<meta\s+name="eprints\.{re.escape(name)}"\s+content="([^"]*)"',
                html,
            )
        ]

    @staticmethod
    def _eprints_meta1(html: str, name: str) -> str:
        """Like _eprints_meta but returns the first value or ''."""
        vals = re.findall(
            rf'<meta\s+name="eprints\.{re.escape(name)}"\s+content="([^"]*)"',
            html,
        )
        return _html_mod.unescape(vals[0]) if vals else ""

    # ------------------------------------------------------------------ parsing

    def _parse_listing_page(self, html: str) -> list[dict]:
        """Parse one NOC publications listing page → list of item dicts."""
        soup = _make_soup(html)
        if soup is None:
            return []

        items = []
        # Each publication is wrapped in <div class="mb-5 pb-5 node-publication-listing">
        for block in soup.find_all(
            "div",
            class_=lambda c: c and "node-publication-listing" in c and "mb-5" in c,
        ):
            try:
                h3 = block.find("h3")
                title = h3.get_text(strip=True) if h3 else ""
                if not title:
                    continue

                p = block.find("p")
                if not p:
                    continue

                # NORA URL + eprint ID
                nora_url = ""
                eprint_id = ""
                doi = ""
                for a in p.find_all("a", href=True):
                    href = a["href"]
                    if "nora.nerc.ac.uk/id/eprint/" in href:
                        nora_url = href.rstrip("/")
                        m = re.search(r"/eprint/(\d+)", href)
                        if m:
                            eprint_id = m.group(1)
                    elif "doi.org/" in href and not doi:
                        doi = href.split("doi.org/", 1)[-1].strip()

                # Authors from <span class="person_name">
                authors = "; ".join(
                    s.get_text(strip=True)
                    for s in p.find_all("span", class_="person_name")
                )

                # Journal from <em>
                em = p.find("em")
                journal = em.get_text(strip=True) if em else ""

                # Year from paragraph plain text
                p_text = p.get_text(separator=" ", strip=True)
                year_m = re.search(r"\b(19|20)\d{2}\b", p_text)
                year = year_m.group(0) if year_m else ""

                # Publication type from the right-hand column
                pub_type = ""
                col_right = block.find(
                    "div", class_=lambda c: c and "col-right" in c
                )
                if col_right:
                    for pp in col_right.find_all("p"):
                        txt = pp.get_text(strip=True)
                        if txt and txt != year:
                            pub_type = txt
                            break

                items.append({
                    "title": title,
                    "authors": authors,
                    "nora_url": nora_url,
                    "eprint_id": eprint_id,
                    "doi": doi,
                    "journal": journal,
                    "year": year,
                    "pub_type": pub_type,
                })
            except Exception as exc:
                print(f"[{self.site_id}] listing parse error: {exc}")
                continue

        return items

    def _fetch_nora_detail(self, url: str) -> dict | None:
        """Fetch a NORA ePrints detail page and return extracted metadata dict."""
        html = self._curl_get(url)
        if not html:
            return None
        try:
            m1 = self._eprints_meta1
            abstract = m1(html, "abstract")
            keywords = m1(html, "keywords")
            date = m1(html, "date")
            doi_raw = m1(html, "id_number")
            official_url = m1(html, "official_url")
            ep_type = m1(html, "type")
            creators = self._eprints_meta(html, "creators_name")
            journal = m1(html, "publication")
            volume = m1(html, "volume")
            number = m1(html, "number")

            doi = doi_raw.replace("doi:", "").strip() if doi_raw else ""
            if not doi and official_url and "doi.org/" in official_url:
                doi = official_url.split("doi.org/", 1)[-1].strip()

            # PDF link on NORA detail page
            pdf_url = ""
            pdf_m = re.search(
                r'href="(https?://nora\.nerc\.ac\.uk[^"]+\.pdf)"', html, re.I
            )
            if pdf_m:
                pdf_url = pdf_m.group(1)

            return {
                "abstract": abstract,
                "keywords": keywords,
                "date": date,
                "doi": doi,
                "ep_type": ep_type,
                "creators": creators,
                "journal": journal,
                "volume": volume,
                "number": number,
                "pdf_url": pdf_url,
            }
        except Exception as exc:
            print(f"[{self.site_id}] NORA parse error ({url}): {exc}")
            return None

    # ------------------------------------------------------------------ crawl

    def crawl(self, limit=None):
        """Crawl NOC publications listing, saving items with ≥50-char abstracts."""
        saved = 0
        page = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break

            if page >= self._MAX_PAGES:
                print(
                    f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping."
                )
                break

            if time.time() - start_time > 25 * 60:
                print(
                    f"[{self.site_id}] 25-minute budget reached at page {page}. Stopping."
                )
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_url = (
                f"{self._LIST_URL}?page={page}" if page > 0 else self._LIST_URL
            )
            html = self._curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            items = self._parse_listing_page(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # URL-based deduplication to detect pagination loops
            new_items = []
            for item in items:
                key = item.get("nora_url") or item.get("doi") or item.get("title", "")
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                new_items.append(item)

            if not new_items:
                print(
                    f"[{self.site_id}] All items on page {page} already seen. "
                    "Pagination loop detected. Stopping."
                )
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    title = item["title"]
                    eprint_id = item.get("eprint_id", "")
                    nora_url = item.get("nora_url", "")

                    nora = None
                    if nora_url and "nora.nerc.ac.uk" in nora_url:
                        time.sleep(self._delay)
                        nora = self._fetch_nora_detail(nora_url)

                    abstract = (nora.get("abstract") if nora else "") or ""

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skip (abstract {len(abstract)} chars): "
                            f"{title[:60]}"
                        )
                        continue

                    doi = (
                        item.get("doi")
                        or (nora.get("doi") if nora else "")
                        or ""
                    )
                    journal = (
                        item.get("journal")
                        or (nora.get("journal") if nora else "")
                        or ""
                    )
                    authors = item.get("authors", "")
                    if not authors and nora and nora.get("creators"):
                        authors = "; ".join(nora["creators"])
                    keywords = (nora.get("keywords") if nora else "") or ""
                    pdf_url = (nora.get("pdf_url") if nora else "") or None

                    published_date = ""
                    if nora and nora.get("date"):
                        dm = re.match(r"(\d{4}-\d{2}-\d{2})", nora["date"])
                        if dm:
                            published_date = dm.group(1)
                        else:
                            dm = re.match(r"(\d{4})", nora["date"])
                            if dm:
                                published_date = dm.group(1)
                    if not published_date and item.get("year"):
                        published_date = item["year"]

                    # Extract original filename from PDF URL
                    original_filename = None
                    if pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in tail:
                            original_filename = tail

                    metadata: dict = {
                        "pub_type": item.get("pub_type", ""),
                        "ep_type": (nora.get("ep_type") if nora else "") or "",
                    }
                    if nora:
                        if nora.get("volume"):
                            metadata["volume"] = nora["volume"]
                        if nora.get("number"):
                            metadata["issue"] = nora["number"]
                    if doi:
                        metadata["doi"] = doi

                    paper = {
                        "site_id": self.site_id,
                        "external_id": eprint_id or doi,
                        "post_number": eprint_id or None,
                        "title": title,
                        "abstract": abstract,
                        "authors": authors,
                        "publisher": "National Oceanography Centre",
                        "journal": journal,
                        "url": nora_url or (f"https://doi.org/{doi}" if doi else ""),
                        "pdf_url": pdf_url,
                        "doi": doi,
                        "keywords": keywords,
                        "category": item.get("pub_type", ""),
                        "published_date": published_date,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
