# -*- coding: utf-8 -*-
"""ADAPT Centre publications crawler (Trinity College Dublin).

Data source: WordPress REST API at
``https://www.adaptcentre.ie/wp-json/wp/v2/publications``.

The detail HTML pages on the site (``/publications/{slug}``) are blocked
(return 1 byte), so abstracts cannot come from the site itself. We
reconstruct them externally:

  1. Extract DOI from ``acf.url`` (doi.org, tandfonline, ACM, Springer ...).
  2. OpenAlex by DOI -> ``abstract_inverted_index`` -> reconstruct text.
  3. Crossref title search -> ``abstract`` field (clean HTML/JATS tags).
  4. OpenAlex title search -> ``abstract_inverted_index``.

Items whose abstract is shorter than ~50 chars after all tiers are
skipped (logged, never crashes the run).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import quote, unquote, urlparse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_WP_ENDPOINT = "https://www.adaptcentre.ie/wp-json/wp/v2/publications"
_PER_PAGE = 100
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_WAITS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 50
_API_DELAY = 1.0  # seconds between external API calls

_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>?#]+)", flags=re.I)


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value)
    text = text.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(value: str | None) -> str:
    """Remove HTML/JATS tags (Crossref abstracts are JATS XML)."""
    if not value:
        return ""
    # Drop JATS namespaced tags and HTML tags in one pass.
    text = re.sub(r"<[^>]+>", " ", value)
    return _clean_text(text)


def _make_soup(raw: str):
    """BeautifulSoup with html5lib -> lxml -> html.parser fallback chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception:
            continue
    return None


def _parse_acf_date(raw: str | None) -> str | None:
    """``acf.date`` is a YYYYMMDD string. Convert to YYYY-MM-DD."""
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    # Sometimes already formatted.
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _parse_wp_date(raw: str | None) -> str | None:
    """WP ``date`` is ISO-like (``2026-02-18T12:30:47``)."""
    if not raw:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(raw))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _split_authors(value: str | None) -> str | None:
    """Split a comma-separated author list, return ``"; "`` joined."""
    if not value:
        return None
    raw = _clean_text(value)
    if not raw:
        return None
    # Normalise " and " and various separators to commas.
    raw = re.sub(r"\s+\band\b\s+", ", ", raw, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s*[,;]\s*", raw) if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return "; ".join(out) if out else None


def _extract_doi(value: str | None) -> str | None:
    """Extract a DOI from a URL or free text.

    Handles ``doi.org/...``, ``tandfonline.com/doi/full/...``,
    ``dl.acm.org/doi/...``, ``link.springer.com/article/10.xxxx``,
    ``sciencedirect.com``, ``mdpi.com``, ``ieeexplore``, etc.
    """
    if not value:
        return None
    text = _clean_text(value)
    if not text:
        return None

    # Direct doi.org link — anything after the host is the DOI.
    try:
        p = urlparse(text)
        host = (p.netloc or "").lower()
        path = p.path or ""
        if host.endswith("doi.org") and path:
            candidate = path.lstrip("/")
            if candidate.lower().startswith("10."):
                return unquote(candidate).rstrip(".,);")
        if host.endswith("tandfonline.com") and "/doi/" in path:
            tail = path.split("/doi/", 1)[1]
            # tandfonline patterns: /doi/full/10.../doi/abs/10.../doi/pdf/10...
            for prefix in ("full/", "abs/", "pdf/", "epdf/", ""):
                if tail.startswith(prefix):
                    cand = tail[len(prefix):]
                    if cand.lower().startswith("10."):
                        return unquote(cand).rstrip(".,);")
        if host.endswith("dl.acm.org") and "/doi/" in path:
            cand = path.split("/doi/", 1)[1]
            for prefix in ("abs/", "full/", "pdf/", ""):
                if cand.startswith(prefix):
                    c = cand[len(prefix):]
                    if c.lower().startswith("10."):
                        return unquote(c).rstrip(".,);")
        if host.endswith("springer.com") and "/article/" in path:
            cand = path.split("/article/", 1)[1]
            if cand.lower().startswith("10."):
                return unquote(cand).rstrip(".,);")
    except Exception:
        pass

    # Generic regex fallback.
    m = _DOI_RE.search(text)
    if m:
        return m.group(1).rstrip(".,);")
    return None


def _abstract_from_inverted_index(index) -> str | None:
    """Reconstruct an abstract from OpenAlex ``abstract_inverted_index``.

    Index format: ``{"word": [pos1, pos2, ...], ...}``.
    """
    if not isinstance(index, dict) or not index:
        return None
    words: list[str | None] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if not isinstance(pos, int) or pos < 0:
                continue
            while len(words) <= pos:
                words.append(None)
            words[pos] = word
    text = " ".join(w for w in words if w)
    return _clean_text(text) or None


# ---------------------------------------------------------------------------
# the crawler
# ---------------------------------------------------------------------------


class AdaptcentreIeResearchCrawler(BaseCrawler):
    site_id = "adaptcentre-ie-research"
    site_name = "Custom: adaptcentre-ie-research"
    base_url = "https://www.adaptcentre.ie"

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, accept: str = "application/json",
                  max_time: int = 45) -> str | None:
        """GET ``url`` with curl + TLS 1.3 + 3-attempt backoff (1s, 3s, 9s)."""
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-sk", "-L", "--compressed",
            "--max-time", str(max_time),
            "-A", self.USER_AGENT,
            "-H", f"Accept: {accept}",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=max_time + 10
                )
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] empty response attempt {attempt}/3 for "
                    f"{url}: rc={result.returncode} err={err[:200]}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < len(_RETRY_WAITS):
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # WP list fetch
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict] | None:
        url = (
            f"{_WP_ENDPOINT}?per_page={_PER_PAGE}&page={page}"
            "&_fields=id,slug,link,date,title,acf,journal_type"
        )
        raw = self._curl_get(url, accept="application/json")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            print(f"[{self.site_id}] page {page}: invalid JSON ({exc})")
            return None
        if not isinstance(data, list):
            # WP returns ``{"code": "rest_post_invalid_page_number", ...}`` past last page.
            if isinstance(data, dict):
                print(
                    f"[{self.site_id}] page {page}: API returned "
                    f"{data.get('code') or 'non-list'} — treating as end-of-pages."
                )
            else:
                print(f"[{self.site_id}] page {page}: unexpected JSON type {type(data).__name__}")
            return []
        return data

    # ------------------------------------------------------------------
    # external abstract resolvers
    # ------------------------------------------------------------------

    def _abstract_openalex_by_doi(self, doi: str) -> str | None:
        """Tier 1: OpenAlex lookup by DOI."""
        if not doi:
            return None
        # Encode DOI as a path segment after ``https://doi.org/``.
        url = (
            "https://api.openalex.org/works/"
            f"https://doi.org/{quote(doi, safe='/:.-_()')}"
            "?select=abstract_inverted_index"
        )
        raw = self._curl_get(url, accept="application/json", max_time=30)
        time.sleep(_API_DELAY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return _abstract_from_inverted_index(data.get("abstract_inverted_index"))

    def _abstract_crossref_by_title(self, title: str) -> tuple[str | None, str | None]:
        """Tier 2: Crossref title search. Returns (abstract, doi)."""
        if not title:
            return None, None
        url = (
            "https://api.crossref.org/works?"
            f"query.title={quote(title)}&rows=1&select=DOI,abstract"
        )
        raw = self._curl_get(url, accept="application/json", max_time=30)
        time.sleep(_API_DELAY)
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
        try:
            items = data.get("message", {}).get("items", [])
        except AttributeError:
            return None, None
        if not items:
            return None, None
        rec = items[0] if isinstance(items[0], dict) else {}
        abstract_raw = rec.get("abstract")
        doi = rec.get("DOI")
        abstract = _strip_html(abstract_raw) if abstract_raw else None
        return abstract, (doi.strip() if isinstance(doi, str) else None)

    def _abstract_openalex_by_title(self, title: str) -> str | None:
        """Tier 3: OpenAlex full-text search by title."""
        if not title:
            return None
        url = (
            "https://api.openalex.org/works?"
            f"search={quote(title)}&per-page=1&select=abstract_inverted_index"
        )
        raw = self._curl_get(url, accept="application/json", max_time=30)
        time.sleep(_API_DELAY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return None
        first = results[0] if isinstance(results[0], dict) else {}
        return _abstract_from_inverted_index(first.get("abstract_inverted_index"))

    def _resolve_abstract(self, title: str, acf_url: str | None) -> tuple[str, str]:
        """Resolve an abstract for an item. Returns ``(abstract, doi)``.

        ``abstract`` is "" when no tier produced one >= _MIN_ABSTRACT_CHARS.
        """
        doi = _extract_doi(acf_url) or ""

        # Tier 1: OpenAlex by DOI.
        if doi:
            try:
                t1 = self._abstract_openalex_by_doi(doi)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] OpenAlex(DOI={doi}) failed: {exc}")
                t1 = None
            if t1 and len(t1) >= _MIN_ABSTRACT_CHARS:
                return t1, doi

        # Tier 2: Crossref title search.
        if title:
            try:
                t2, cr_doi = self._abstract_crossref_by_title(title)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] Crossref(title) failed: {exc}")
                t2, cr_doi = None, None
            if not doi and cr_doi:
                doi = cr_doi
            if t2 and len(t2) >= _MIN_ABSTRACT_CHARS:
                return t2, doi
            # If Crossref gave us a fresh DOI, try OpenAlex with it.
            if cr_doi and cr_doi != doi:
                try:
                    t1b = self._abstract_openalex_by_doi(cr_doi)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] OpenAlex(DOI={cr_doi}) failed: {exc}")
                    t1b = None
                if t1b and len(t1b) >= _MIN_ABSTRACT_CHARS:
                    return t1b, cr_doi

        # Tier 3: OpenAlex title search.
        if title:
            try:
                t3 = self._abstract_openalex_by_title(title)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] OpenAlex(title) failed: {exc}")
                t3 = None
            if t3 and len(t3) >= _MIN_ABSTRACT_CHARS:
                return t3, doi

        return "", doi

    # ------------------------------------------------------------------
    # per-item processing
    # ------------------------------------------------------------------

    def _process_item(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        wp_id = item.get("id")
        if wp_id is None:
            return None
        slug = item.get("slug") or ""
        link = item.get("link") or ""
        wp_date = item.get("date") or ""
        listed_date = _parse_wp_date(wp_date)

        title_obj = item.get("title") or {}
        if isinstance(title_obj, dict):
            title = _clean_text(_strip_html(title_obj.get("rendered") or ""))
        else:
            title = _clean_text(str(title_obj))

        acf = item.get("acf") or {}
        if not isinstance(acf, dict):
            acf = {}
        if not title:
            title = _clean_text(_strip_html(acf.get("title_of_paper") or ""))

        pub_name = _clean_text(acf.get("name_of_publication") or "") or ""
        authors_raw = acf.get("authors_list") or ""
        authors = _split_authors(authors_raw) or ""
        date_raw = acf.get("date") or ""
        published_date = _parse_acf_date(date_raw) or listed_date
        pub_type = _clean_text(acf.get("type") or "") or ""
        acf_url = _clean_text(acf.get("url") or "") or ""

        jt_ids = item.get("journal_type")
        if not isinstance(jt_ids, list):
            jt_ids = []

        if not title:
            print(f"[{self.site_id}] item wp_id={wp_id}: no title; skipping")
            return None

        abstract, doi = self._resolve_abstract(title, acf_url or None)
        if not abstract or len(abstract) < _MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] item wp_id={wp_id} skipped: "
                f"abstract too short ({len(abstract)} chars) — {title[:70]!r}"
            )
            return None

        meta_url = link or f"https://www.adaptcentre.ie/publications/{slug}/"
        if not meta_url:
            print(f"[{self.site_id}] item wp_id={wp_id}: no URL; skipping")
            return None

        metadata = {
            "wp_id": wp_id,
            "slug": slug,
            "journal_raw": pub_name,
            "journal_type_ids": jt_ids,
            "pub_type": pub_type,
            "acf_url": acf_url,
            "wp_date_raw": wp_date,
            "date_raw": date_raw,
            "authors_raw": authors_raw,
            "posted_date": listed_date,
            "originalFilename": None,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(wp_id),
            "post_number": str(wp_id),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "",
            "department": "",
            "journal": pub_name,
            "url": meta_url,
            "pdf_url": None,
            "keywords": "",
            "category": pub_type,
            "doi": doi or "",
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        started = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= _PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget reached; stopping.")
                break

            items = self._fetch_list_page(page)
            if items is None:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping.")
                break
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; done.")
                break

            new_count = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget reached mid-page; stopping.")
                    return saved

                link = ""
                wp_id = None
                if isinstance(item, dict):
                    link = item.get("link") or ""
                    wp_id = item.get("id")
                dedup_key = link or (f"wp:{wp_id}" if wp_id is not None else "")
                if not dedup_key:
                    continue
                if dedup_key in seen_urls:
                    continue
                seen_urls.add(dedup_key)
                new_count += 1

                try:
                    paper = self._process_item(item)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_or_inf}: "
                        f"{paper.get('title', '')[:70]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_id = (item or {}).get("id") if isinstance(item, dict) else None
                    print(f"[{self.site_id}] item wp_id={item_id} failed: {exc}")
                    continue

            if new_count == 0:
                print(f"[{self.site_id}] page {page}: 0 new items; done.")
                break

            if page % 10 == 0:
                print(f"[adaptcentre-ie-research] page {page}: saved {saved}/{limit_or_inf}")

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
