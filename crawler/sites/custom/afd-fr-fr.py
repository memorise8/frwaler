# -*- coding: utf-8 -*-
"""AFD (Agence Française de Développement) publications crawler.

Target: https://www.afd.fr/fr/publications/liste?words=&type%5B170%5D=170

Approach:
  - Paginate HTML list pages (?page=N, N=0..37+)
  - Each card has a link to a detail page (/fr/ressources/...)
  - Detail pages carry rich metadata in a Drupal settings JSON blob:
      content_nid, content_field_publication_date, content_field_resource_type,
      content_field_collection, content_field_download, content_field_metatags, ...
  - Abstract comes from <meta name="description"> (typically 400+ chars)
  - PDF link comes from first <a href="*.pdf"> in the page
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup as _BS
    _BS_AVAILABLE = True
except ImportError:
    _BS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML; fallback chain: html5lib → lxml → html.parser."""
    if not _BS_AVAILABLE:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, max_time: int = 30, retries: int = 3):
    """GET url via curl with exponential backoff. Returns decoded text or None."""
    waits = [1, 3, 9]
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", str(max_time), "-L",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=max_time + 5)
            raw = result.stdout
            if raw:
                return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[afd-fr-fr] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            time.sleep(waits[attempt])
    return None


def _extract_drupal_data(html: str) -> dict:
    """Return pianoAnalytics.dataLayer from the embedded Drupal settings JSON."""
    m = re.search(
        r'data-drupal-selector="drupal-settings-json">(.*?)</script>',
        html, re.DOTALL
    )
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
        return d.get("pianoAnalytics", {}).get("dataLayer", {})
    except (json.JSONDecodeError, AttributeError, KeyError):
        return {}


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class AfdFrFrCrawler(BaseCrawler):
    """Crawler for AFD (Agence Française de Développement) research publications."""

    site_id = "afd-fr-fr"
    site_name = "Custom: afd-fr-fr"
    base_url = "https://www.afd.fr"

    _LIST_URL = "https://www.afd.fr/fr/publications/liste"
    _LIST_QUERY = "words=&type%5B170%5D=170"

    def crawl(self, limit=None):
        """Paginate the publications list and save each item.

        Walks pages 0.. until (a) saved >= limit, (b) no new records,
        or (c) safety cap of 200 pages or 25-minute wall-clock budget.
        """
        start_time = time.time()
        MAX_RUNTIME = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
        MAX_PAGES = 200

        saved = 0
        seen_urls: set = set()
        page = 0
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # ---- termination guards ----
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_RUNTIME:
                print(f"[afd-fr-fr] 25-minute wall-clock budget reached at page {page}. Exiting cleanly.")
                break
            if page >= MAX_PAGES:
                print(f"[afd-fr-fr] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            if page > 0 and page % 10 == 0:
                print(f"[afd-fr-fr] page {page}: saved {saved}/{limit_str}")

            # ---- fetch list page ----
            list_url = f"{self._LIST_URL}?{self._LIST_QUERY}&page={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[afd-fr-fr] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[afd-fr-fr] Parse error on list page {page}: {exc}. Stopping.")
                break

            if soup is None:
                print(f"[afd-fr-fr] Could not build soup for list page {page}. Stopping.")
                break

            cards = soup.find_all("div", class_="views-row")
            if not cards:
                print(f"[afd-fr-fr] No cards on page {page}. Done.")
                break

            # ---- collect new links from this page ----
            new_links = []
            for card in cards:
                link_el = card.find("a", class_="fr-card__link")
                if not link_el:
                    continue
                href = (link_el.get("href") or "").strip()
                if not href:
                    continue
                full_url = href if href.startswith("http") else self.base_url + href
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    new_links.append(full_url)

            if not new_links:
                print(f"[afd-fr-fr] No new links on page {page}. Done.")
                break

            # ---- process each detail page ----
            for detail_url in new_links:
                if limit is not None and saved >= limit:
                    break
                try:
                    ok = self._process_item(detail_url)
                    if ok:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[afd-fr-fr] item {detail_url} failed: {exc}")
                time.sleep(self._delay)

            page += 1

        print(f"[afd-fr-fr] Done. Total saved: {saved}")
        return saved

    def _process_item(self, detail_url: str) -> bool:
        """Fetch one detail page, extract metadata, and save. Returns True if saved."""
        raw = _curl_get(detail_url)
        if not raw:
            print(f"[afd-fr-fr] Could not fetch detail page {detail_url}. Skipping.")
            return False

        # Drupal JSON — the richest metadata source
        data = _extract_drupal_data(raw)

        try:
            soup = _make_soup(raw)
        except Exception:
            soup = None

        # ---- Title ----
        title = str(data.get("content_title", "")).strip()
        if not title and soup:
            for tag, attrs in [
                ("meta", {"property": "og:title"}),
                ("meta", {"name": "title"}),
                ("h1", {}),
            ]:
                el = soup.find(tag, attrs)
                if el:
                    val = (el.get("content") or el.get_text(strip=True) or "").strip()
                    if val:
                        title = val
                        break
        if not title:
            print(f"[afd-fr-fr] No title for {detail_url}. Skipping.")
            return False

        # ---- Abstract ----
        abstract = ""
        if soup:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc:
                abstract = (meta_desc.get("content") or "").strip()
        if not abstract and soup:
            # Fallback: extract readable text from the <main> element
            main_el = soup.find("main")
            if main_el:
                for tag in main_el.find_all(["script", "style"]):
                    tag.decompose()
                abstract = re.sub(r"\s+", " ", main_el.get_text(" ", strip=True))[:2000]

        if len(abstract) < 50:
            print(f"[afd-fr-fr] Abstract too short ({len(abstract)} chars) for {detail_url}. Skipping.")
            return False

        # ---- Node ID (external_id / post_number) ----
        nid = str(data.get("content_nid", "")).strip()
        if not nid:
            # Fallback: use the URL slug as a stable identifier
            nid = detail_url.rstrip("/").split("/")[-1]

        # ---- Published date ----
        pub_date = str(data.get("content_field_publication_date", "")).strip()
        if not pub_date and soup:
            t_el = soup.find("time", attrs={"datetime": True})
            if t_el:
                dt_match = re.match(r"(\d{4}-\d{2}-\d{2})", t_el.get("datetime", ""))
                if dt_match:
                    pub_date = dt_match.group(1)

        # ---- Keywords ----
        keywords = ""
        mt_raw = data.get("content_field_metatags", "")
        if mt_raw:
            try:
                mt = json.loads(mt_raw) if isinstance(mt_raw, str) else mt_raw
                keywords = (mt.get("keywords") or "").strip()
            except Exception:
                pass
        if not keywords and soup:
            mk = soup.find("meta", attrs={"name": "keywords"})
            if mk:
                keywords = (mk.get("content") or "").strip()

        # ---- PDF URL + original filename ----
        pdf_url = ""
        original_filename = ""
        if soup:
            pdf_a = soup.find("a", href=re.compile(r"\.pdf(\?|#|$)", re.I))
            if pdf_a:
                href = (pdf_a.get("href") or "").strip()
                if href:
                    pdf_url = href if href.startswith("http") else self.base_url + href
                    original_filename = pdf_url.split("/")[-1].split("?")[0]

        # content_field_download is the bare filename (e.g. "pb24_vf_web.pdf")
        dl_fn = str(data.get("content_field_download", "")).strip()
        if dl_fn and not original_filename:
            original_filename = dl_fn

        # ---- Classification / taxonomy ----
        resource_type = str(data.get("content_field_resource_type", "")).strip()
        collection = str(data.get("content_field_collection", "")).strip()
        doc_number = str(data.get("content_field_number", "")).strip()
        thematic = data.get("content_field_thematic", [])
        region = data.get("content_field_region_country", [])
        research_prog = str(data.get("content_field_research_program", "")).strip()
        page_count = str(data.get("content_field_page_number", "")).strip()

        # ---- Metadata JSON ----
        meta_dict: dict = {"posted_date": pub_date or None}
        if original_filename or dl_fn:
            meta_dict["originalFilename"] = original_filename or dl_fn
        if nid:
            meta_dict["node_id"] = nid
        if resource_type:
            meta_dict["resource_type"] = resource_type
        if collection:
            meta_dict["collection"] = collection
        if doc_number:
            meta_dict["document_number"] = doc_number
        if thematic:
            meta_dict["thematic"] = thematic if isinstance(thematic, list) else [thematic]
        if region:
            meta_dict["region_country"] = region if isinstance(region, list) else [region]
        if research_prog:
            meta_dict["research_program"] = research_prog
        if page_count:
            meta_dict["page_count"] = page_count

        paper = {
            "site_id": self.site_id,
            "external_id": nid,
            "url": detail_url,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date or None,
            "posted_date": pub_date or None,
            "authors": None,
            "publisher": "AFD - Agence Française de Développement",
            "journal": collection or None,
            "keywords": keywords or None,
            "pdf_url": pdf_url or None,
            "original_filename": original_filename or dl_fn or None,
            "category": resource_type or None,
            "metadata": json.dumps(meta_dict, ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f"[afd-fr-fr] Saved [{nid}]: {title[:70]}")
        return True
