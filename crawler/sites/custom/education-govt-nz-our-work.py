# -*- coding: utf-8 -*-
"""
Crawler for education.govt.nz/our-work/publications/education-circulars

Uses the Algolia search API (discovered via browser network inspection)
to fetch all Education Circulars, then fetches each detail page for the
full body text from Next.js RSC T-blob payloads.
"""

import html as html_lib
import json
import re
import subprocess
import time
from datetime import datetime, timezone

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler


class EducationGovtNzOurWorkCrawler(BaseCrawler):
    site_id = "education-govt-nz-our-work"
    site_name = "Custom: education-govt-nz-our-work"
    base_url = "https://www.education.govt.nz"

    _ALGOLIA_URL = "https://zjyla4j4r6-dsn.algolia.net/1/indexes/*/queries"
    _ALGOLIA_APP_ID = "ZJYLA4J4R6"
    _ALGOLIA_API_KEY = os.environ.get("EDUCATION_GOVT_NZ_OUR_WORK_KEY", "")
    _ALGOLIA_INDEX = "date_desc_live"
    _ALGOLIA_FILTER = (
        'status:true AND entity_type:content AND '
        '(result.lvl1:"Pages > Guidance" OR result.lvl1:"Pages > Information") AND '
        'level_1:"our work" AND level_2:"publications" AND '
        'level_3:"education circulars" AND show_in_topic_search:true'
    )
    _HITS_PER_PAGE = 20

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-NZ,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
            except Exception as exc:
                if attempt < retries - 1:
                    print(f"[{self.site_id}] curl GET error (attempt {attempt+1}): {exc}")
                    time.sleep(3 * (attempt + 1))
                else:
                    print(f"[{self.site_id}] curl GET failed after {retries} attempts: {exc}")
        return None

    def _algolia_query(self, page=0, hits_per_page=20, retries=3):
        payload = json.dumps({
            "requests": [{
                "indexName": self._ALGOLIA_INDEX,
                "filters": self._ALGOLIA_FILTER,
                "hitsPerPage": hits_per_page,
                "page": page,
            }]
        }, ensure_ascii=False)
        cmd = [
            "curl", "-sk", "--max-time", "30",
            "-X", "POST",
            "-H", f"x-algolia-application-id: {self._ALGOLIA_APP_ID}",
            "-H", f"x-algolia-api-key: {self._ALGOLIA_API_KEY}",
            "-H", "Content-Type: application/json",
            "-d", payload,
            self._ALGOLIA_URL,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
                if r.stdout.strip():
                    return json.loads(r.stdout)
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
            except Exception as exc:
                if attempt < retries - 1:
                    print(f"[{self.site_id}] Algolia error (attempt {attempt+1}): {exc}")
                    time.sleep(3 * (attempt + 1))
                else:
                    print(f"[{self.site_id}] Algolia failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Abstract extraction from Next.js RSC HTML
    # ------------------------------------------------------------------

    def _extract_abstract(self, html):
        """Extract full plain-text abstract from Next.js RSC streaming HTML.

        The site renders via RSC: body content is delivered as T<hex>,<html>
        text blobs inside self.__next_f.push([1, "..."]) calls.  We collect
        all push payloads, find T-blobs, strip HTML tags and concatenate.
        """
        try:
            # Collect all RSC push payloads (raw JSON-encoded strings)
            parts = re.findall(
                r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
                html, re.DOTALL
            )
            combined = "\n".join(parts)

            # Find T-blob text segments: T<hex>,<html_content> up to next RSC key
            tblobs = re.findall(
                r'T[0-9a-f]+,(.*?)(?=\n[0-9a-zA-Z]+:|$)',
                combined, re.DOTALL
            )

            texts = []
            for blob in tblobs:
                # Decode JSON string escapes still present in the payload
                decoded = (blob
                           .replace('\\u003c', '<').replace('\\u003e', '>')
                           .replace('\\u0026', '&').replace('\\u2019', "’")
                           .replace('\\u2014', "—").replace('\\u201c', "“")
                           .replace('\\u201d', "”").replace('\\u00e2', 'â')
                           .replace('\\n', ' ').replace('\\"', '"'))
                # Strip HTML tags
                text = re.sub(r'<[^>]+>', ' ', html_lib.unescape(decoded))
                text = ' '.join(text.split())
                if len(text) > 30:
                    texts.append(text)

            if texts:
                return ' '.join(texts)[:4000]
        except Exception as exc:
            print(f"[{self.site_id}] abstract extraction error: {exc}")

        # Fallback: meta description
        m = re.search(r'"description","content":"([^"]+)"', html)
        if m:
            return m.group(1).replace('\\"', '"')
        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Education Circulars and save records.

        Paginates through the Algolia index, fetches each detail page for
        the full body text, and persists via _save_paper().
        """
        saved = 0
        seen_urls = set()
        limit_inf = limit is None
        start_time = time.time()
        page = 0
        max_pages = 200

        while True:
            # Wall-clock budget (25 min)
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached, stopping.")
                break

            if not limit_inf and saved >= limit:
                break

            if page >= max_pages:
                print(f"[{self.site_id}] Safety cap of {max_pages} pages reached.")
                break

            if page > 0 and page % 10 == 0:
                lim_str = str(limit) if not limit_inf else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            resp = self._algolia_query(page=page, hits_per_page=self._HITS_PER_PAGE)
            if not resp:
                print(f"[{self.site_id}] Algolia returned no response on page {page}, stopping.")
                break

            results = resp.get("results", [])
            if not results:
                break

            hits = results[0].get("hits", [])
            nb_pages = results[0].get("nbPages", 0)

            if not hits:
                print(f"[{self.site_id}] No hits on page {page}, done.")
                break

            for hit in hits:
                if not limit_inf and saved >= limit:
                    break

                try:
                    url_path = hit.get("url", "")
                    if not url_path:
                        continue

                    full_url = f"{self.base_url}{url_path}"
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    title = (hit.get("title") or "").strip()
                    if not title:
                        continue

                    # Drupal node ID from objectID e.g. "entity:node/1640:en"
                    obj_id = hit.get("objectID") or hit.get("search_api_id") or ""
                    m = re.search(r"node/(\d+)", obj_id)
                    node_id = m.group(1) if m else obj_id

                    # Date (Algolia uses millisecond epoch)
                    created_ms = hit.get("created") or hit.get("changed")
                    if created_ms:
                        try:
                            published_date = datetime.fromtimestamp(
                                int(created_ms) / 1000, tz=timezone.utc
                            ).strftime("%Y-%m-%d")
                        except Exception:
                            published_date = None
                    else:
                        published_date = None

                    # Keywords from tags field
                    tags_raw = hit.get("tags", "")
                    if isinstance(tags_raw, list):
                        keywords = ", ".join(tags_raw)
                    else:
                        keywords = str(tags_raw) if tags_raw else ""

                    # Short abstract from Algolia
                    algolia_abstract = (hit.get("content") or "").strip()

                    # Fetch detail page for full body text
                    time.sleep(self._delay)
                    detail_html = self._curl_get(full_url)
                    abstract = algolia_abstract

                    if detail_html:
                        try:
                            full_text = self._extract_abstract(detail_html)
                            if len(full_text) > len(abstract):
                                abstract = full_text
                        except Exception as exc:
                            print(f"[{self.site_id}] detail parse failed for {full_url}: {exc}")

                    # Skip if abstract too short
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] skip (short abstract {len(abstract)} chars): {title}")
                        continue

                    # Metadata: site-specific fields not mapped to top-level columns
                    metadata = {
                        "node_id": node_id,
                        "objectID": obj_id,
                        "school_types": hit.get("school_types", []),
                        "year_levels": hit.get("year_levels", []),
                        "te_reo_title": hit.get("te_reo_title", ""),
                        "breadcrumbs": hit.get("breadcrumbs", []),
                        "result_type": hit.get("result_type", ""),
                        "changed_ms": hit.get("changed"),
                    }

                    self._save_paper({
                        "external_id": node_id,
                        "post_number": node_id,
                        "title": title,
                        "abstract": abstract,
                        "url": full_url,
                        "pdf_url": None,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "publisher": "Ministry of Education New Zealand",
                        "authors": "",
                        "keywords": keywords,
                        "category": "Education Circular",
                        "original_filename": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {hit.get('objectID', '?')} failed: {exc}")
                    continue

            if page >= nb_pages - 1:
                break

            page += 1

        return saved
