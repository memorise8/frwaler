# -*- coding: utf-8 -*-
"""SECIHTI Sala de Prensa crawler — WordPress REST API (custom post type)."""

import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

# Absolute import: spec_from_file_location has no package context.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402


# ---------------------------------------------------------------------------
# HTML stripping helpers
# ---------------------------------------------------------------------------

def _bs4_strip(html: str) -> str:
    """Strip HTML tags using bs4 with fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, parser)
            text = soup.get_text(separator=" ", strip=True)
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            continue
    # regex last resort
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&(?:[a-zA-Z]+|#\d+);", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw) -> str:
    """Normalise YYYYMMDD / ISO-datetime / YYYY-MM-DD → YYYY-MM-DD."""
    if not raw:
        return ""
    raw = str(raw).strip()
    if re.match(r"^\d{8}$", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if "T" in raw:
        return raw[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return raw


def _curl_get(url: str, params: dict = None) -> str | None:
    """GET via curl with up to 3 retries (1 s, 3 s, 9 s backoff)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", "Accept: application/json",
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace").strip()
            if raw:
                return raw
            wait = 3 ** attempt
            print(f"[secihti-mx-sala-de-prensa] empty response, retry in {wait}s...")
            time.sleep(wait)
        except Exception as exc:
            if attempt < 2:
                wait = 3 ** attempt
                print(f"[secihti-mx-sala-de-prensa] curl error: {exc}, retry in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[secihti-mx-sala-de-prensa] curl failed after 3 attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class SecihtiSalaDePrensaCrawler(BaseCrawler):
    """Crawls SECIHTI's press releases via the WordPress REST API."""

    site_id = "secihti-mx-sala-de-prensa"
    site_name = "Custom: secihti-mx-sala-de-prensa"
    base_url = "https://secihti.mx"

    _API_URL = "https://secihti.mx/wp-json/wp/v2/sala-de-prensa"
    _PAGE_SIZE = 100

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()

        while True:
            # 25-minute wall-clock budget
            if time.time() - start_time > 25 * 60:
                print(f"[{self.site_id}] 25-min budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 200:
                print(f"[{self.site_id}] Safety cap of 200 pages reached. Stopping.")
                break

            params = {
                "per_page": self._PAGE_SIZE,
                "page": page,
                "_embed": "1",
                "orderby": "date",
                "order": "desc",
            }

            time.sleep(self._delay)
            raw = _curl_get(self._API_URL, params)

            if raw is None:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON error at page {page}: {exc}. Stopping.")
                break

            if not isinstance(items, list) or not items:
                print(f"[{self.site_id}] No more items at page {page}. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    item_url = item.get("link", "")

                    # URL deduplication — guards against paginators that loop back to page 1
                    if item_url and item_url in seen_urls:
                        continue
                    if item_url:
                        seen_urls.add(item_url)

                    post_id = item.get("id")
                    external_id = str(post_id) if post_id is not None else None
                    slug = item.get("slug", "")

                    # Title
                    title_raw = (item.get("title") or {}).get("rendered", "")
                    title = _bs4_strip(title_raw) if title_raw else slug

                    # Advanced Custom Fields
                    acf = item.get("acf") or {}
                    pub_date_raw = acf.get("comunicado_fecha_publicacion") or ""
                    published_date = _parse_date(pub_date_raw)
                    comunicado_numero = str(acf.get("comunicado_numero") or "").strip()
                    # post_number: prefer the sequential press-release number
                    post_number = comunicado_numero if comunicado_numero else external_id
                    pdf_url = acf.get("comunicado_pdf") or ""

                    # Key points (repeater field)
                    puntos = acf.get("comunicado_puntos_clave") or []
                    key_points = []
                    for p in puntos:
                        pt = (p.get("comunicado_punto", "") if isinstance(p, dict)
                              else str(p))
                        if pt and pt.strip():
                            key_points.append(pt.strip())

                    # Abstract: key points header + full stripped content
                    content_html = (item.get("content") or {}).get("rendered", "") or ""
                    content_text = _bs4_strip(content_html)

                    if key_points:
                        abstract = ("Puntos clave: " + "; ".join(key_points)
                                    + "\n\n" + content_text)
                    else:
                        abstract = content_text
                    abstract = abstract.strip()

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping {external_id}: "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    # Dates
                    listed_date = _parse_date(item.get("date") or "")

                    # Authors and category from embedded taxonomy terms
                    authors = self._extract_term(item, "autor-comunicado-de-prensa")
                    if not authors:
                        prefix = "autor-comunicado-de-prensa-"
                        for cls in item.get("class_list") or []:
                            if cls.startswith(prefix):
                                authors = cls[len(prefix):].replace("-", " ").title()
                                break
                    if not authors:
                        authors = "SECIHTI"

                    category = self._extract_term(item, "categoria-comunicado-de-prensa")
                    keywords = ", ".join(key_points) if key_points else ""

                    # PDF original filename from URL path
                    original_filename = None
                    if pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in tail and len(tail) <= 200:
                            original_filename = tail

                    metadata_dict = {
                        "posted_date": item.get("date", ""),
                        "node_id": post_id,
                        "slug": slug,
                        "comunicado_numero": comunicado_numero,
                        "comunicado_puntos_clave": key_points,
                        "modified": item.get("modified", ""),
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": item_url,
                        "pdf_url": pdf_url or None,
                        "original_filename": original_filename,
                        "authors": authors,
                        "publisher": "SECIHTI",
                        "category": category,
                        "keywords": keywords,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_disp = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{limit_disp}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('id', '?')} failed: {exc}")
                    continue

            if page % 10 == 0:
                limit_disp = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_disp}")

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Taxonomy helper
    # ------------------------------------------------------------------

    def _extract_term(self, item: dict, taxonomy: str) -> str:
        """Return ';'-joined term names for *taxonomy* from _embedded data."""
        try:
            wp_terms = (item.get("_embedded") or {}).get("wp:term") or []
            for term_array in wp_terms:
                if not isinstance(term_array, list):
                    continue
                names = [
                    t["name"]
                    for t in term_array
                    if isinstance(t, dict)
                    and t.get("taxonomy") == taxonomy
                    and t.get("name")
                ]
                if names:
                    return "; ".join(names)
        except Exception:
            pass
        return ""
