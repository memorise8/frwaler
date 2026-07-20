# -*- coding: utf-8 -*-
"""NKVTS English Academic Articles crawler.

Discovers articles via the Algolia search API (public read-only key embedded
in the publications page), then fetches each detail page for the full abstract,
authors, DOI, and journal name.

Starting URL (for reference):
  https://www.nkvts.no/english/publications/?sd-post_type_label%5B%5D=Academic+Articles&pg=1
"""

import json
import os
import re
import sys
import time

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from crawler.base_crawler import BaseCrawler  # noqa: E402

# ---------------------------------------------------------------------------
# Algolia credentials (public, read-only search key exposed in page HTML)
# ---------------------------------------------------------------------------
_ALGOLIA_APP_ID = "A4EOX51ZO9"
_ALGOLIA_API_KEY = os.environ.get("NKVTS_NO_ENGLISH_KEY", "")
_ALGOLIA_INDEX = "wp_searchable_posts"
_ALGOLIA_URL = (
    f"https://{_ALGOLIA_APP_ID}-dsn.algolia.net"
    f"/1/indexes/{_ALGOLIA_INDEX}/query"
)
_HITS_PER_PAGE = 50


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Build a BeautifulSoup with a parser fallback chain."""
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


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_authors(raw: str) -> str:
    """Convert citation author string to '; '-separated format.

    Input:  'Kjærvik, S. L., Thomson, N. D.'
            'Andersson, E. S., Skar, A. M. S., & Jensen, T. K.'
    Output: 'Kjærvik, S. L.; Thomson, N. D.'
    """
    raw = raw.strip().rstrip(",").replace(" & ", ", ").replace("&", ", ")
    parts = [p.strip() for p in raw.split(", ")]

    # Detect initial blocks: one or more "X." segments (capital + optional lower + period)
    _initials_re = re.compile(r"^[A-Za-zÀ-ÿ][a-zà-ÿ]?\.(?:\s[A-Za-zÀ-ÿ][a-zà-ÿ]?\.)*$")

    authors: list[str] = []
    pending: str | None = None

    for part in parts:
        if not part:
            continue
        if _initials_re.match(part):
            if pending is not None:
                authors.append(f"{pending}, {part}")
                pending = None
            elif authors:
                authors[-1] = authors[-1] + " " + part
        else:
            if pending is not None:
                authors.append(pending)
            pending = part

    if pending is not None:
        authors.append(pending)

    cleaned = [a.strip().rstrip(",") for a in authors if a.strip()]
    return "; ".join(cleaned) if cleaned else ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class NKVTSEnglishCrawler(BaseCrawler):
    """Crawler for NKVTS English Academic Articles."""

    site_id = "nkvts-no-english"
    site_name = "Custom: nkvts-no-english"
    base_url = "https://www.nkvts.no"

    # ------------------------------------------------------------------
    # Algolia list API
    # ------------------------------------------------------------------

    def _algolia_page(self, page: int) -> dict | None:
        """Query Algolia for Academic Articles at 0-based page index.

        Returns parsed JSON dict or None on failure (3 retries with backoff).
        """
        payload = {
            "query": "",
            "hitsPerPage": _HITS_PER_PAGE,
            "page": page,
            "facetFilters": ["post_type_label:Academic Articles"],
        }
        for attempt in range(3):
            wait = (1, 3, 9)[attempt]
            try:
                resp = self._session.post(
                    _ALGOLIA_URL,
                    json=payload,
                    headers={
                        "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
                        "X-Algolia-API-Key": _ALGOLIA_API_KEY,
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                print(
                    f"[{self.site_id}] Algolia p{page} attempt {attempt+1}/3: {exc}"
                )
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Detail page fetch + parse
    # ------------------------------------------------------------------

    def _fetch_html(self, url: str) -> str | None:
        """Fetch a URL and return HTML text. 3 retries with exponential backoff."""
        for attempt in range(3):
            wait = (1, 3, 9)[attempt]
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                try:
                    return resp.text
                except Exception:
                    return resp.content.decode("utf-8", errors="replace")
            except Exception as exc:
                print(
                    f"[{self.site_id}] GET {url[:70]} attempt {attempt+1}/3: {exc}"
                )
                if attempt < 2:
                    time.sleep(wait)
        return None

    def _parse_detail(self, url: str, algolia_content: str = "") -> dict:
        """Fetch and parse a detail page.

        Returns a dict with keys:
          title, abstract, authors, journal, doi, category, citation
        """
        result = {
            "title": "",
            "abstract": "",
            "authors": "",
            "journal": "",
            "doi": "",
            "category": "",
            "citation": "",
        }

        html = self._fetch_html(url)
        if not html:
            clean_ac = re.sub(r"^Abstract\s*", "", algolia_content or "").strip()
            result["abstract"] = clean_ac
            return result

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] soup error for {url}: {exc}")
            soup = None

        if soup is None:
            result["abstract"] = re.sub(r"^Abstract\s*", "", algolia_content or "").strip()
            return result

        # ---- Title ----
        h1 = soup.find("h1", class_="wp-block-post-title")
        if h1:
            result["title"] = _clean(h1.get_text())

        # ---- Category ----
        htype = soup.find(class_="entry-header-type")
        if htype:
            a = htype.find("a")
            if a:
                result["category"] = _clean(a.get_text())

        # ---- Citation paragraph (authors / journal / DOI) ----
        entry_header = soup.find(class_="entry-header")
        citation_p = entry_header.find("p") if entry_header else None

        if citation_p:
            citation_text = _clean(citation_p.get_text(" "))
            result["citation"] = citation_text

            # DOI from anchor href first (most reliable)
            doi_anchor = citation_p.find(
                "a", href=lambda h: h and "doi.org" in h
            )
            if doi_anchor:
                doi_href = doi_anchor.get("href", "")
                m = re.search(r"10\.\d{4,}/\S+", doi_href)
                if m:
                    result["doi"] = m.group(0).rstrip(".,;)")
            if not result["doi"]:
                m = re.search(r"10\.\d{4,}/[^\s\"'<>]+", citation_text)
                if m:
                    result["doi"] = m.group(0).rstrip(".,;)")

            # Journal from <i> tag
            i_tag = citation_p.find("i")
            if i_tag:
                result["journal"] = _clean(i_tag.get_text())

            # Authors: everything before the first "(YYYY)", parsed into "; " format
            year_m = re.search(r"\(\d{4}\)", citation_text)
            if year_m:
                raw = citation_text[: year_m.start()].strip().rstrip(",").strip()
                result["authors"] = _parse_authors(raw)

        # ---- Abstract: entry-content-col (preferred) ----
        content_col = soup.find(class_="entry-content-col")
        if content_col:
            text = _clean(content_col.get_text(" "))
            text = re.sub(r"^Abstract\s*", "", text).strip()
            if len(text) >= 100:
                result["abstract"] = text

        # ---- Fallback: Algolia content field ----
        if len(result["abstract"]) < 100 and algolia_content:
            cleaned = re.sub(r"^Abstract\s*", "", algolia_content).strip()
            if len(cleaned) >= 100:
                result["abstract"] = cleaned

        # ---- Last resort: full citation text as abstract ----
        if len(result["abstract"]) < 100:
            cit = result["citation"]
            if len(cit) >= 100:
                result["abstract"] = cit

        # ---- Authors fallback: researcher list ----
        if not result["authors"]:
            researchers = soup.find(class_="entry-researchers-col")
            if researchers:
                names = [
                    _clean(h3.get_text()) for h3 in researchers.find_all("h3")
                ]
                result["authors"] = "; ".join(names)

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NKVTS English Academic Articles via Algolia + detail pages.

        Paginates through the Algolia index (Academic Articles facet),
        fetches each detail page, and persists via _save_paper.

        Parameters
        ----------
        limit:
            Maximum number of articles to save. None = unlimited.
        """
        saved = 0
        seen_urls: set = set()
        seen_post_ids: set = set()
        crawl_start = time.time()
        limit_str = str(limit) if limit is not None else "∞"
        max_pages = 200  # safety cap (actual ~16 for 766 articles)

        algolia_page = 0
        nb_pages_total: int | None = None

        while algolia_page < max_pages:

            # ---- Budget check ----
            elapsed_min = (time.time() - crawl_start) / 60
            if elapsed_min >= 24.5:
                print(
                    f"[{self.site_id}] Approaching 25-minute budget "
                    f"({elapsed_min:.1f}min). Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            if algolia_page % 10 == 0:
                print(
                    f"[{self.site_id}] page {algolia_page}: "
                    f"saved {saved}/{limit_str}"
                )

            # ---- Fetch Algolia page (light throttle on list API) ----
            time.sleep(0.3)
            data = self._algolia_page(algolia_page)
            if data is None:
                print(
                    f"[{self.site_id}] Algolia page {algolia_page} failed after "
                    "3 retries. Stopping."
                )
                break

            hits = data.get("hits", [])
            nb_pages_total = data.get("nbPages", 0)

            if not hits:
                print(
                    f"[{self.site_id}] No hits at Algolia page {algolia_page}. Done."
                )
                break

            # Detect end of pagination
            if algolia_page >= nb_pages_total:
                print(
                    f"[{self.site_id}] Passed last Algolia page "
                    f"({nb_pages_total}). Done."
                )
                break

            if algolia_page == max_pages - 1:
                print(
                    f"[{self.site_id}] Safety cap of {max_pages} pages reached."
                )

            # ---- Process each hit ----
            for hit in hits:
                if limit is not None and saved >= limit:
                    break

                if (time.time() - crawl_start) / 60 >= 24.5:
                    print(
                        f"[{self.site_id}] Time budget reached mid-page. Stopping."
                    )
                    break

                post_id = hit.get("post_id")
                if not post_id:
                    continue

                # Deduplicate: Algolia may split long posts into multiple records
                if post_id in seen_post_ids:
                    continue
                seen_post_ids.add(post_id)

                permalink = (hit.get("permalink") or "").strip()
                if not permalink:
                    print(
                        f"[{self.site_id}] No permalink for post_id={post_id}, skipping"
                    )
                    continue

                if permalink in seen_urls:
                    continue
                seen_urls.add(permalink)

                post_title = hit.get("post_title", "")
                post_date_raw = hit.get("post_date", "") or ""
                published_year = hit.get("published_year")
                algolia_content = hit.get("content", "") or ""

                # Published date from Algolia post_date "YYYY-MM-DD HH:MM:SS"
                published_date = ""
                m = re.match(r"(\d{4}-\d{2}-\d{2})", post_date_raw)
                if m:
                    published_date = m.group(1)
                elif published_year:
                    published_date = f"{published_year}-01-01"

                # ---- Fetch + parse detail page (isolated per-item) ----
                try:
                    time.sleep(self._delay)
                    detail = self._parse_detail(permalink, algolia_content)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {post_id} failed: {exc}")
                    continue

                title = detail["title"] or _clean(post_title)
                abstract = detail["abstract"]

                # Skip items whose abstract is too short
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] Skipping {post_id}: abstract too short "
                        f"({len(abstract)} chars) — {title[:50]}"
                    )
                    continue

                meta = {
                    "posted_date": published_date,
                    "post_id": post_id,
                    "published_year": published_year,
                    "citation": detail["citation"],
                    "post_type": hit.get("post_type", ""),
                }
                if detail["doi"]:
                    meta["doi"] = detail["doi"]

                paper = {
                    "site_id": self.site_id,
                    "external_id": str(post_id),
                    "url": permalink,
                    "title": title,
                    "abstract": abstract,
                    "authors": detail["authors"] or None,
                    "publisher": "NKVTS",
                    "journal": detail["journal"] or None,
                    "pdf_url": None,
                    "doi": detail["doi"] or None,
                    "category": detail["category"] or None,
                    "published_date": published_date or None,
                    "posted_date": published_date or None,
                    "keywords": None,
                    "original_filename": None,
                    "metadata": json.dumps(meta, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Save failed for {post_id}: {exc}")
                    continue

            algolia_page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
