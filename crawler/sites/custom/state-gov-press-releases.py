# -*- coding: utf-8 -*-
"""US State Department press releases via WordPress REST API."""

import json
import os
import re
import time

from crawler.base_crawler import BaseCrawler


def _strip_html(html: str) -> str:
    """Strip HTML tags using BeautifulSoup with fallback chain."""
    if not html:
        return ""
    text = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, parser)
            for tag in soup.find_all(["script", "style", "nav", "head"]):
                tag.decompose()
            text = soup.get_text(separator=" ")
            break
        except Exception:
            continue
    if text is None:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class StateGovPressReleasesCrawler(BaseCrawler):
    """Crawler for US State Department press releases."""

    site_id = "state-gov-press-releases"
    site_name = "Custom: state-gov-press-releases"
    base_url = "https://www.state.gov"

    _API_BASE = "https://www.state.gov/wp-json/wp/v2/state_press_release"
    _PAGE_SIZE = 20
    _FIELDS = "id,slug,title,date,modified,link,content,excerpt,acf,class_list"

    def _fetch_page(self, page: int):
        """Fetch one API page. Returns (items, total_pages) or ([], 0) on failure."""
        params = {
            "per_page": self._PAGE_SIZE,
            "page": page,
            "_fields": self._FIELDS,
        }
        for attempt in range(3):
            try:
                if attempt > 0:
                    wait = 3 ** attempt
                    print(f"[{self.site_id}] Retry {attempt}/3 in {wait}s (page {page})...")
                    time.sleep(wait)
                resp = self._session.get(
                    self._API_BASE, params=params, timeout=30,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip, deflate",
                    },
                )
                resp.raise_for_status()
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                items = resp.json()
                if not isinstance(items, list):
                    return [], 0
                return items, total_pages
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} fetch error (attempt {attempt + 1}/3): {exc}")
        return [], 0

    def crawl(self, limit=None):
        seen_urls = set()
        saved = 0
        page = 1
        total_pages = None
        start_time = time.time()
        MAX_PAGES = 200

        while True:
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached, exiting cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached, stopping")
                break

            if total_pages is not None and page > total_pages:
                print(f"[{self.site_id}] Reached last page ({total_pages}), done")
                break

            items, tp = self._fetch_page(page)

            if total_pages is None and tp:
                total_pages = tp
                print(f"[{self.site_id}] Total pages: {total_pages}")

            if not items:
                print(f"[{self.site_id}] No items at page {page}, done")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    url = item.get("link") or ""
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    post_id = item.get("id")
                    title = (item.get("title") or {}).get("rendered", "").strip()
                    if not title:
                        continue

                    date_raw = item.get("date") or ""
                    published_date = date_raw[:10] if date_raw else ""

                    content_html = (item.get("content") or {}).get("rendered", "") or ""
                    abstract = _strip_html(content_html)

                    if len(abstract) < 100:
                        exc_html = (item.get("excerpt") or {}).get("rendered", "") or ""
                        abstract = _strip_html(exc_html)

                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] Short abstract ({len(abstract)} chars) "
                            f"for {url!r}, skipping"
                        )
                        continue

                    class_list = item.get("class_list") or []
                    department = ""
                    doc_type = ""
                    policy_issues = []
                    for cls in class_list:
                        if cls.startswith("state_bureaus-") and not department:
                            department = cls[len("state_bureaus-"):].replace("-", " ").title()
                        elif cls.startswith("state_document_type-") and not doc_type:
                            doc_type = cls[len("state_document_type-"):].replace("-", " ").title()
                        elif cls.startswith("state_policy_issues-"):
                            policy_issues.append(
                                cls[len("state_policy_issues-"):].replace("-", " ").title()
                            )

                    acf = item.get("acf") or {}
                    doc_date_raw = acf.get("document_date") or ""

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": str(post_id),
                        "post_number": str(post_id),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": url,
                        "pdf_url": None,
                        "doi": None,
                        "authors": "",
                        "publisher": "United States Department of State",
                        "department": department,
                        "journal": "",
                        "keywords": ",".join(policy_issues),
                        "category": doc_type,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": published_date,
                            "post_id": post_id,
                            "slug": item.get("slug") or "",
                            "document_date": doc_date_raw,
                            "policy_issues": policy_issues,
                            "document_type": doc_type,
                            "modified": item.get("modified") or "",
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = f"/{limit}" if limit is not None else ""
                    print(f"[{self.site_id}] Saved {saved}{limit_str}: {title[:60]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} already seen, done")
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
