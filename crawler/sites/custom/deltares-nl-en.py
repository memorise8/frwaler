# -*- coding: utf-8 -*-
"""Deltares NL English publications crawler.

Target:
  https://www.deltares.nl/en/expertise/publications
  ?languages=Dutch&languages=English&materials=ARTICLE

Site: Nuxt.js SSR frontend backed by Craft CMS + ElasticSearch.

Discovery: list pages at /en/expertise/publications?...&page={N}
           SSR HTML, ~12 publication slugs per page.
Detail:    /en/expertise/publications/{slug}
           SSR HTML with embedded window.__NUXT__ state that contains
           authors, postDate, materialType, digitalReferences (PDF URLs).
           Abstract and title come from <meta> tags.
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.deltares.nl"
_LIST_URL = f"{_BASE}/en/expertise/publications"
_LIST_PARAMS = "?languages=Dutch&languages=English&materials=ARTICLE"
_PAGE_CAP = 200
_BUDGET_SECS = 1500  # 25 minutes
_DELAY = 1.0  # seconds between detail fetches

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ─── helpers ───────────────────────────────────────────────────────────────────

def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl (TLS 1.3, skip cert) with exponential-backoff retry."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                 "-A", _UA, url],
                capture_output=True, text=True, errors="replace", timeout=35,
            )
            if result.stdout.strip():
                return result.stdout
        except Exception as exc:
            print(f"[deltares-nl-en] curl error attempt {attempt + 1}: {exc}")
        if attempt < retries - 1:
            wait = (1, 3, 9)[attempt]
            time.sleep(wait)
    return None


def _make_soup(html: str):
    """Parse HTML trying html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _meta(soup, name: str = None, prop: str = None) -> str:
    if soup is None:
        return ""
    tag = (soup.find("meta", attrs={"name": name}) if name
           else soup.find("meta", attrs={"property": prop}))
    return (tag.get("content", "") or "").strip() if tag else ""


def _decode_nuxt_url(s: str) -> str:
    """Decode \\u002F → / and similar escapes in a NUXT-state URL string."""
    return (s.replace(r"/", "/")
             .replace(r":", ":")
             .replace(r"&", "&")
             .replace(r"?", "?"))


# ─── crawler ───────────────────────────────────────────────────────────────────

class DeltaresNlEnCrawler(BaseCrawler):

    site_id = "deltares-nl-en"
    site_name = "Custom: deltares-nl-en"
    base_url = "https://www.deltares.nl"

    # ── list page ──────────────────────────────────────────────────────────────

    def _list_page_urls(self, page: int) -> list[str]:
        """Return publication detail URLs found on the given list page."""
        params = _LIST_PARAMS + (f"&page={page}" if page > 1 else "")
        raw = _curl_get(f"{_LIST_URL}{params}")
        if not raw:
            return []
        seen: set[str] = set()
        result: list[str] = []
        for slug in re.findall(
            r'href=["\'](?:https://www\.deltares\.nl)?'
            r'(/en/expertise/publications/[^"\'?#/][^"\'?#]*)["\']',
            raw,
        ):
            full = f"{_BASE}{slug}"
            if full not in seen:
                seen.add(full)
                result.append(full)
        return result

    # ── detail page ────────────────────────────────────────────────────────────

    def _parse_detail(self, url: str) -> dict | None:
        """Fetch and parse one publication detail page. Returns paper dict or None."""
        raw = _curl_get(url)
        if not raw:
            return None

        # BeautifulSoup for meta tags (with parser fallback)
        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[deltares-nl-en] BS4 error for {url}: {exc}")
            soup = None

        # ── Title ──────────────────────────────────────────────────────────────
        title = _meta(soup, name="twitter:title") or _meta(soup, prop="og:title")
        if not title:
            m = re.search(r"<title[^>]*>([^<]+)</title>", raw)
            title = m.group(1).strip() if m else ""
        title = re.sub(r"\s*[\|–—]\s*Deltares\s*$", "", title,
                       flags=re.IGNORECASE).strip()

        # ── Abstract from meta description ─────────────────────────────────────
        abstract = _meta(soup, name="description")
        if not abstract:
            m = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
                raw,
            )
            abstract = m.group(1).strip() if m else ""
        abstract = abstract.strip()

        if len(abstract) < 100:
            print(
                f"[deltares-nl-en] abstract too short "
                f"({len(abstract)} chars), skipping: {url}"
            )
            return None

        # ── NUXT state ─────────────────────────────────────────────────────────
        nuxt_m = re.search(r"window\.__NUXT__=(.+?);</script>", raw, re.DOTALL)
        nuxt = nuxt_m.group(1) if nuxt_m else ""

        # External/numeric ID from controlPanel URL:
        #   publicationPage/{ID}-{slug}?site=enDefault
        ext_id: str | None = None
        cp_m = re.search(r"publicationPage(?:\\u002F|/)+(\d+)-", nuxt)
        if cp_m:
            ext_id = cp_m.group(1)
        if not ext_id:
            # Fallback: use URL slug
            ext_id = url.rstrip("/").split("/")[-1]

        post_number: str | None = ext_id if (ext_id and ext_id.isdigit()) else None

        # Authors: [{initials:"S.",insertion:"van",lastName:"Asselen"}, ...]
        # insertion may be a string literal OR a variable reference (null/undefined)
        authors_list: list[str] = []
        for initials, insertion_lit, last in re.findall(
            r'\{initials:"([^"]*)",insertion:(?:"([^"]*)"|[^,{}]+)'
            r',lastName:"([^"]*)"\}',
            nuxt,
        ):
            parts = [p for p in [initials.strip(), insertion_lit.strip(),
                                 last.strip()] if p]
            if parts:
                authors_list.append(" ".join(parts))
        authors = "; ".join(authors_list) if authors_list else None

        # postDate: p.postDate="2024-01-01T01:00:00+01:00"
        post_date: str | None = None
        pd_m = re.search(r'\.postDate="(\d{4}-\d{2}-\d{2})', nuxt)
        if pd_m:
            post_date = pd_m.group(1)

        # materialType
        mat_m = re.search(r'\.materialType="([^"]+)"', nuxt)
        category = mat_m.group(1) if mat_m else "ARTICLE"

        # digitalReferences: [{name:"EP5491.pdf",description:"...",url:"https://..."}]
        pdf_url: str | None = None
        original_filename: str | None = None
        ref_m = re.search(
            r'digitalReferences=\[\{name:"([^"]+)",description:"[^"]*",'
            r'url:"([^"]+)"',
            nuxt,
        )
        if ref_m:
            original_filename = ref_m.group(1)
            raw_pdf = ref_m.group(2)
            decoded_pdf = _decode_nuxt_url(raw_pdf)
            if decoded_pdf.startswith("http"):
                pdf_url = decoded_pdf

        # ── Assemble paper dict ────────────────────────────────────────────────
        meta_dict: dict = {
            "posted_date": post_date,
            "materialType": category,
        }
        if original_filename:
            meta_dict["originalFilename"] = original_filename

        return {
            "site_id": self.site_id,
            "external_id": ext_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": post_date,
            "posted_date": post_date,
            "authors": authors,
            "publisher": "Deltares",
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": None,
            "category": category,
            "doi": None,
            "metadata": json.dumps(meta_dict, ensure_ascii=False),
        }

    # ── main crawl loop ────────────────────────────────────────────────────────

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"
        page = 1

        while page <= _PAGE_CAP:
            # Time budget
            if time.time() - start_time > _BUDGET_SECS:
                print("[deltares-nl-en] 25-min budget reached, stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if page == _PAGE_CAP:
                print(f"[deltares-nl-en] safety cap of {_PAGE_CAP} pages reached.")
            if page % 10 == 0:
                print(
                    f"[deltares-nl-en] page {page}: saved {saved}/{limit_str}"
                )

            page_urls = self._list_page_urls(page)
            if not page_urls:
                print(f"[deltares-nl-en] page {page}: no URLs found. Done.")
                break

            new_urls = [u for u in page_urls if u not in seen_urls]
            if not new_urls:
                print(f"[deltares-nl-en] page {page}: all URLs already seen. Done.")
                break

            for detail_url in new_urls:
                if limit is not None and saved >= limit:
                    break
                seen_urls.add(detail_url)

                try:
                    time.sleep(_DELAY)
                    paper = self._parse_detail(detail_url)
                    if not paper:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[deltares-nl-en] saved {saved}/{limit_str}: "
                        f"{paper.get('title', '')[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[deltares-nl-en] item failed ({detail_url}): {exc}")
                    continue

            page += 1

        print(f"[deltares-nl-en] Done. Total saved: {saved}")
        return saved
