# -*- coding: utf-8 -*-
"""Crawler for OSR Statistics Authority publications list.

Target: https://osr.statisticsauthority.gov.uk/publications-list/
API: WordPress REST API /wp-json/wp/v2/publication
"""

import json
import subprocess
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from crawler.base_crawler import BaseCrawler


class OSRPublicationsListCrawler(BaseCrawler):
    site_id = "osr-statisticsauthority-gov-uk-publications-list"
    site_name = "Custom: osr-statisticsauthority-gov-uk-publications-list"
    base_url = "https://osr.statisticsauthority.gov.uk"

    _API_BASE = "https://osr.statisticsauthority.gov.uk/wp-json/wp/v2"
    _DETAIL_DELAY = 1.0
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_MINUTES = 25

    def _curl_get(self, url, retries=3):
        """Fetch URL with curl, return bytes or None after all retries fail."""
        for attempt in range(retries):
            if attempt > 0:
                wait = 3 ** attempt  # 3s, 9s
                print(f"[{self.site_id}] retrying in {wait}s: {url}")
                time.sleep(wait)
            try:
                result = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30", url],
                    capture_output=True,
                    timeout=40,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                print(f"[{self.site_id}] curl rc={result.returncode} (attempt {attempt+1}/{retries}): {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/{retries}): {exc}")
        return None

    def _parse_html(self, raw):
        """Parse HTML with fallback: html5lib → lxml → html.parser."""
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:
                raw = str(raw)
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    def _get_abstract(self, url):
        """Fetch detail page and extract abstract text from entry-content div."""
        raw = self._curl_get(url)
        if not raw:
            return None
        soup = self._parse_html(raw)
        if not soup:
            return None
        content_div = soup.find(class_="entry-content")
        if not content_div:
            content_div = soup.find("main") or soup.find("article")
        if not content_div:
            return None
        paragraphs = []
        for p in content_div.find_all("p"):
            txt = p.get_text(separator=" ", strip=True)
            if len(txt) > 30:
                paragraphs.append(txt)
        if paragraphs:
            return " ".join(paragraphs[:12])
        return content_div.get_text(separator=" ", strip=True)

    def _get_pdf_info(self, post_id):
        """Return (pdf_url, original_filename) from WP media API, or (None, None)."""
        url = f"{self._API_BASE}/media?parent={post_id}&mime_type=application/pdf"
        raw = self._curl_get(url)
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
            if data:
                m = data[0]
                pdf_url = m.get("source_url")
                title = m.get("title", {}).get("rendered", "").strip()
                slug = m.get("slug", "").strip()
                filename = title or slug or ""
                if pdf_url and not filename:
                    filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                if filename and not filename.lower().endswith(".pdf"):
                    filename += ".pdf"
                return pdf_url or None, filename or None
        except Exception:
            pass
        return None, None

    def _load_pub_type_map(self):
        """Return dict mapping publication-type term ID → slug."""
        raw = self._curl_get(f"{self._API_BASE}/publication-type?per_page=100")
        if not raw:
            return {}
        try:
            terms = json.loads(raw)
            return {t["id"]: t.get("slug", str(t["id"])) for t in terms}
        except Exception:
            return {}

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float("inf")

        pub_type_map = self._load_pub_type_map()

        page = 1
        while page <= self._MAX_PAGES:
            # Wall-clock safety
            elapsed_min = (time.time() - start_time) / 60.0
            if elapsed_min >= self._MAX_WALL_MINUTES:
                print(f"[{self.site_id}] Wall-clock limit ({self._MAX_WALL_MINUTES}m) reached at page {page}, stopping.")
                break

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Safety page cap ({self._MAX_PAGES}) reached, stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = (
                f"{self._API_BASE}/publication"
                f"?per_page=100&page={page}&orderby=date&order=desc&_embed=1"
            )
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping.")
                break

            try:
                items = json.loads(raw)
            except Exception as exc:
                print(f"[{self.site_id}] JSON parse error on page {page}: {exc}")
                break

            if not isinstance(items, list) or len(items) == 0:
                print(f"[{self.site_id}] Empty page {page}, done.")
                break

            new_on_page = 0
            for item in items:
                if saved >= limit_or_inf:
                    break

                try:
                    detail_url = item.get("link", "")
                    if not detail_url or detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    wp_id = item.get("id")
                    title = item.get("title", {}).get("rendered", "").strip()
                    date_str = (item.get("date") or "")[:10]  # YYYY-MM-DD

                    # publication-type taxonomy → category
                    pub_type_ids = item.get("publication-type") or []
                    category_slugs = [pub_type_map.get(tid, str(tid)) for tid in pub_type_ids]
                    category = "; ".join(category_slugs)

                    # Themes + country from embedded terms → keywords
                    theme_names = []
                    try:
                        for term_list in item.get("_embedded", {}).get("wp:term", []):
                            for t in term_list:
                                if t.get("taxonomy") in ("themes", "country"):
                                    name = t.get("name", "").strip()
                                    if name:
                                        theme_names.append(name)
                    except Exception:
                        pass
                    keywords = ", ".join(theme_names)

                    # Fetch abstract from detail page
                    time.sleep(self._DETAIL_DELAY)
                    abstract = self._get_abstract(detail_url)
                    if not abstract or len(abstract) < 100:
                        print(
                            f"[{self.site_id}] skipping {detail_url}: "
                            f"abstract too short ({len(abstract) if abstract else 0} chars)"
                        )
                        continue

                    # Fetch PDF
                    time.sleep(0.3)
                    pdf_url, original_filename = self._get_pdf_info(wp_id)

                    metadata = json.dumps(
                        {
                            "wp_id": wp_id,
                            "slug": item.get("slug", ""),
                            "modified": item.get("modified", ""),
                            "pub_type_ids": pub_type_ids,
                            "producer": item.get("producer", []),
                            "country": item.get("country", []),
                            "themes_raw": item.get("themes", []),
                        },
                        ensure_ascii=False,
                    )

                    self._save_paper(
                        {
                            "site_id": self.site_id,
                            "external_id": str(wp_id),
                            "post_number": str(wp_id),
                            "title": title,
                            "abstract": abstract,
                            "published_date": date_str,
                            "posted_date": date_str,
                            "listed_date": date_str,
                            "url": detail_url,
                            "meta_url": detail_url,
                            "pdf_url": pdf_url,
                            "original_filename": original_filename,
                            "keywords": keywords,
                            "category": category,
                            "publisher": "Office for Statistics Regulation",
                            "authors": "",
                            "metadata": metadata,
                        }
                    )
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('id', '?')} failed: {exc}")
                    continue

            if saved >= limit_or_inf:
                break

            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page}, done.")
                break

            page += 1

        print(f"[{self.site_id}] Crawl complete: saved {saved} items.")
        return saved
