# -*- coding: utf-8 -*-
"""EDX NETL DOE Gov — LCA Unit Process Library group crawler.

Starting URL: https://edx.netl.doe.gov/group/lca-unit-process-library
API: CKAN 3 /api/3/action/group_package_show (limit + offset pagination)
Total packages: ~535
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_GROUP_ID = "lca-unit-process-library"
_PAGE_SIZE = 100
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))


def _curl_get(url: str, *, user_agent: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with exponential backoff (1s, 3s, 9s)."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {user_agent}",
        "-H", "Accept: application/json, */*;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[edx-netl-doe-gov-group] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[edx-netl-doe-gov-group] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[edx-netl-doe-gov-group] curl failed after {retries} attempts: {exc}")
    return None


def _fix_resource_url(raw_url: str, base_url: str) -> str:
    """Normalise resource URLs from the CKAN API.

    group_package_show returns  /storage/f/edx/...
    package_show returns        http://storage/f/edx/...
    Both need to become        https://edx.netl.doe.gov/storage/f/edx/...
    """
    if not raw_url:
        return ""
    if raw_url.startswith("/"):
        return base_url + raw_url
    if raw_url.startswith("http://storage/"):
        return base_url + raw_url[len("http://storage"):]
    return raw_url


class EdxNetlDoeGovGroupCrawler(BaseCrawler):
    site_id = "edx-netl-doe-gov-group"
    site_name = "Custom: edx-netl-doe-gov-group"
    base_url = "https://edx.netl.doe.gov"

    def _fetch_page(self, offset: int) -> list | None:
        url = (
            f"{self.base_url}/api/3/action/group_package_show"
            f"?id={_GROUP_ID}&limit={_PAGE_SIZE}&offset={offset}"
        )
        raw = _curl_get(url, user_agent=self.USER_AGENT)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[edx-netl-doe-gov-group] JSON decode error at offset {offset}: {exc}")
            return None
        if not data.get("success"):
            print(f"[edx-netl-doe-gov-group] API error at offset {offset}: {data.get('error')}")
            return None
        result = data.get("result")
        if not isinstance(result, list):
            return None
        return result

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = limit if limit is not None else "∞"

        for page_num in range(_MAX_PAGES):
            # Respect limit
            if limit is not None and saved >= limit:
                break

            # 25-minute wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[edx-netl-doe-gov-group] 25-minute budget reached. Exiting cleanly.")
                break

            if page_num == _MAX_PAGES - 1:
                print(f"[edx-netl-doe-gov-group] Safety cap of {_MAX_PAGES} pages reached. Stopping.")

            offset = page_num * _PAGE_SIZE
            packages = self._fetch_page(offset)

            if packages is None:
                print(f"[edx-netl-doe-gov-group] Failed to fetch offset {offset}. Stopping.")
                break

            if not packages:
                print(f"[edx-netl-doe-gov-group] No more packages at offset {offset}. Done.")
                break

            if page_num % 10 == 0:
                print(f"[edx-netl-doe-gov-group] page {page_num}: saved {saved}/{limit_display}")

            new_on_page = 0
            for pkg in packages:
                if limit is not None and saved >= limit:
                    break

                try:
                    pkg_name = pkg.get("name", "")
                    detail_url = f"{self.base_url}/dataset/{pkg_name}"

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    pkg_id = pkg.get("id", "")
                    title = (pkg.get("title") or "").strip()

                    # ---- extras dict ----
                    extras: dict[str, str] = {}
                    for e in (pkg.get("extras") or []):
                        if isinstance(e, dict) and e.get("key"):
                            extras[e["key"]] = e.get("value", "")

                    # ---- tags ----
                    tags = [
                        t["name"] for t in (pkg.get("tags") or [])
                        if isinstance(t, dict) and t.get("name")
                    ]

                    # ---- abstract ----
                    notes = (pkg.get("notes") or "").strip()
                    citation = extras.get("citation", "")
                    abstract_parts = []
                    if notes:
                        abstract_parts.append(notes)
                    if citation:
                        abstract_parts.append(f"Citation: {citation}")
                    abstract = "\n\n".join(abstract_parts)

                    if len(abstract) < 50:
                        print(f"[edx-netl-doe-gov-group] skipping '{title[:50]}': "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    # ---- OSTI ID → post_number ----
                    osti_match = re.search(r"OSTI\s*ID[:\s]+(\d+)", notes + " " + citation)
                    post_number = osti_match.group(1) if osti_match else pkg_name

                    # ---- dates ----
                    published_date = extras.get("publication_date", "")
                    # Normalise to YYYY-MM-DD just in case it arrives in a different format
                    if published_date:
                        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", published_date)
                        published_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else published_date

                    metadata_created = pkg.get("metadata_created") or ""
                    listed_date = metadata_created[:10] if metadata_created else ""

                    # ---- authors / publisher ----
                    authors = (
                        extras.get("point_of_contact")
                        or pkg.get("maintainer")
                        or ""
                    )
                    publisher = "National Energy Technology Laboratory (NETL)"

                    # ---- resources ----
                    resources = pkg.get("resources") or []
                    pdf_url = None
                    original_filename = None
                    for res in resources:
                        if not isinstance(res, dict):
                            continue
                        fmt = (res.get("format") or "").upper()
                        rname = (res.get("name") or res.get("filename") or "")
                        if fmt == "PDF" or rname.lower().endswith(".pdf"):
                            raw_url = res.get("url") or ""
                            pdf_url = _fix_resource_url(raw_url, self.base_url) or None
                            original_filename = rname or None
                            break

                    # ---- keywords ----
                    keywords = ", ".join(tags) if tags else None

                    # ---- category from tags ----
                    category = next(
                        (t for t in tags if "Life Cycle Analysis" in t),
                        (tags[0] if tags else None),
                    )

                    # ---- metadata dict ----
                    meta_dict: dict = {
                        "posted_date": listed_date,
                        "originalFilename": original_filename,
                        "ckan_id": pkg_id,
                        "ckan_name": pkg_name,
                        "maintainer": pkg.get("maintainer") or "",
                        "maintainer_email": pkg.get("maintainer_email") or "",
                        "program_or_project": extras.get("program_or_project") or "",
                        "project_number": extras.get("project_number") or "",
                        "poc_email": extras.get("poc_email") or "",
                        "netl_product": extras.get("netl_product") or "",
                        "osti_id": osti_match.group(1) if osti_match else None,
                        "citation": citation,
                    }
                    # Drop empty/None values to keep metadata lean
                    meta_dict = {k: v for k, v in meta_dict.items() if v}

                    paper = {
                        "site_id": self.site_id,
                        "external_id": pkg_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "authors": authors,
                        "publisher": publisher,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "keywords": keywords,
                        "category": category,
                        "doi": extras.get("doi") or None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(meta_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1
                    print(f"[edx-netl-doe-gov-group] saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[edx-netl-doe-gov-group] item {pkg.get('name', '?')} failed: {exc}")
                    continue

            # If all packages on this page were already seen, stop to prevent infinite loop
            if new_on_page == 0 and len(packages) > 0:
                print(f"[edx-netl-doe-gov-group] All items on page {page_num} already seen. Stopping.")
                break

            time.sleep(self._delay)

        print(f"[edx-netl-doe-gov-group] Done. Total saved: {saved}")
        return saved
