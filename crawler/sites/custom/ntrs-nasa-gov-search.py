# -*- coding: utf-8 -*-
"""Crawler for NASA Technical Reports Server (NTRS) — Contractor Reports.

API endpoint: https://ntrs.nasa.gov/api/citations/search
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_BASE = "https://ntrs.nasa.gov"
_SEARCH_API = f"{_BASE}/api/citations/search"
_STI_TYPE = "Contractor%20Report%20(CR)"
_PAGE_SIZE = 100
_ABSTRACT_MIN_LEN = 100


def _curl_get(url, retries=3):
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk",
                 "-H", "Accept: application/json",
                 "-H", "User-Agent: Mozilla/5.0",
                 url],
                capture_output=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[ntrs-nasa-gov-search] curl rc={result.returncode} attempt {attempt+1}/{retries}")
        except subprocess.TimeoutExpired:
            print(f"[ntrs-nasa-gov-search] curl timeout attempt {attempt+1}/{retries}")
        except Exception as exc:
            print(f"[ntrs-nasa-gov-search] curl error attempt {attempt+1}/{retries}: {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


def _parse_date(val):
    if not val:
        return None
    try:
        return str(val)[:10]
    except Exception:
        return None


class NtrsNasaGovSearchCrawler(BaseCrawler):
    site_id = "ntrs-nasa-gov-search"
    site_name = "Custom: ntrs-nasa-gov-search"
    base_url = _BASE
    DELIVERY_ORDER = "arbitrary"

    def crawl(self, limit=None):
        saved = 0
        limit_or_inf = limit if limit is not None else "∞"
        seen_urls = set()
        start_time = time.time()
        MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        page_from = (self.delivery_cursor or {}).get("offset", 0)
        page_num = 0

        while True:
            if time.time() - start_time > MAX_WALL_SECS:
                print(f"[ntrs-nasa-gov-search] 25-min wall-clock budget reached, stopping")
                break

            if page_num >= MAX_PAGES:
                print(f"[ntrs-nasa-gov-search] safety cap of {MAX_PAGES} pages reached")
                break

            if limit is not None and saved >= limit:
                break

            url = (
                f"{_SEARCH_API}?stiTypeDetails={_STI_TYPE}"
                f"&page%5Bfrom%5D={page_from}&page%5Bsize%5D={_PAGE_SIZE}"
            )
            raw = _curl_get(url)
            if not raw:
                print(f"[ntrs-nasa-gov-search] fetch failed on page {page_num}, stopping")
                break

            try:
                data = json.loads(raw)
            except Exception as exc:
                print(f"[ntrs-nasa-gov-search] JSON parse error page {page_num}: {exc}")
                break

            results = data.get("results") or []
            if not results:
                print(f"[ntrs-nasa-gov-search] empty results on page {page_num}, done")
                self._mark_exhausted()
                break

            new_on_page = 0
            for item in results:
                if limit is not None and saved >= limit:
                    break

                try:
                    submission_id = item.get("id")
                    if not submission_id:
                        continue

                    detail_url = f"{_BASE}/citations/{submission_id}"
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    title = (item.get("title") or "").strip()
                    if not title:
                        print(f"[ntrs-nasa-gov-search] skip {submission_id}: no title")
                        continue

                    abstract = (item.get("abstract") or "").strip()
                    if len(abstract) < _ABSTRACT_MIN_LEN:
                        print(
                            f"[ntrs-nasa-gov-search] skip {submission_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Authors (deduplicated, order preserved)
                    author_names = []
                    seen_authors = set()
                    for aff in (item.get("authorAffiliations") or []):
                        meta = aff.get("meta") or {}
                        name = ((meta.get("author") or {}).get("name") or "").strip()
                        if name and name not in seen_authors:
                            author_names.append(name)
                            seen_authors.add(name)
                    authors = "; ".join(author_names) if author_names else None

                    # Publisher / organisations (deduplicated)
                    org_names = []
                    seen_orgs = set()
                    for aff in (item.get("authorAffiliations") or []):
                        meta = aff.get("meta") or {}
                        org_name = ((meta.get("organization") or {}).get("name") or "").strip()
                        if org_name and org_name not in seen_orgs:
                            org_names.append(org_name)
                            seen_orgs.add(org_name)
                    publisher = "; ".join(org_names) if org_names else None

                    # Dates
                    pub_date = None
                    pubs = item.get("publications") or []
                    if pubs:
                        pub_date = _parse_date(pubs[0].get("publicationDate"))
                    if not pub_date:
                        pub_date = _parse_date(item.get("submittedDate"))

                    listed_date = _parse_date(
                        item.get("distributionDate") or item.get("submittedDate")
                    )

                    # Keywords
                    kw_list = item.get("keywords") or []
                    keywords = ", ".join(str(k) for k in kw_list) if kw_list else None

                    # Category
                    cats = item.get("subjectCategories") or []
                    category = cats[0] if cats else None

                    # PDF download link
                    pdf_url = None
                    original_filename = None
                    downloads = item.get("downloads") or []
                    if downloads:
                        dl = downloads[0]
                        links = dl.get("links") or {}
                        pdf_rel = links.get("pdf") or links.get("original")
                        if pdf_rel:
                            if pdf_rel.startswith("/"):
                                pdf_url = f"{_BASE}{pdf_rel}"
                            else:
                                pdf_url = pdf_rel
                        original_filename = dl.get("name")

                    # Center
                    center = item.get("center") or {}

                    # Metadata: all raw fields not mapped above
                    meta_dict = {
                        "submission_id": submission_id,
                        "stiType": item.get("stiType"),
                        "stiTypeDetails": item.get("stiTypeDetails"),
                        "center_code": center.get("code"),
                        "center_name": center.get("name"),
                        "distribution": item.get("distribution"),
                        "otherReportNumbers": item.get("otherReportNumbers"),
                        "fundingNumbers": [
                            fn.get("number")
                            for fn in (item.get("fundingNumbers") or [])
                            if fn.get("number")
                        ],
                        "status": item.get("status"),
                        "disseminated": item.get("disseminated"),
                        "subjectCategories": cats,
                        "originalFilename": original_filename,
                        "posted_date": listed_date,
                        "category": category,
                    }
                    meta_dict = {k: v for k, v in meta_dict.items() if v is not None}

                    paper = {
                        "site_id": self.site_id,
                        "external_id": str(submission_id),
                        "post_number": str(submission_id),
                        "url": detail_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "authors": authors,
                        "publisher": publisher,
                        "keywords": keywords,
                        "category": category,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "metadata": meta_dict,
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ntrs-nasa-gov-search] item {item.get('id', '?')} failed: {exc}")
                    continue

            if page_num % 10 == 0:
                print(f"[ntrs-nasa-gov-search] page {page_num}: saved {saved}/{limit_or_inf}")

            self._advance_cursor({"offset": page_from + _PAGE_SIZE}, items_done=len(results))

            if new_on_page == 0:
                print(f"[ntrs-nasa-gov-search] all items on page {page_num} already seen, stopping")
                break

            page_from += _PAGE_SIZE
            page_num += 1

        print(f"[ntrs-nasa-gov-search] crawl complete: {saved} documents saved")
        return saved
