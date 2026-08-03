# -*- coding: utf-8 -*-
"""finance-ni.gov.uk publications crawler (Drupal 10 HTML scrape)."""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402


class FinanceNiGovUkPublicationsCrawler(BaseCrawler):
    site_id = "finance-ni-gov-uk-publications"
    site_name = "Custom: finance-ni-gov-uk-publications"
    base_url = "https://www.finance-ni.gov.uk"

    _LIST_URL = "https://www.finance-ni.gov.uk/publications"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _CRAWL_TIMEOUT = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------ #
    # Network                                                              #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*",
            "-H", "Accept-Language: en-GB,en;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error ({url}): {exc}")
            if attempt < 2:
                wait = (2 ** attempt)
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------ #
    # Parsing helpers                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_soup(raw: str):
        from bs4 import BeautifulSoup
        for parser in ("lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _node_id(html: str) -> str | None:
        m = re.search(r"node[/-](\d+)", html[:12000])
        return m.group(1) if m else None

    def _parse_list_page(self, raw: str) -> tuple[list[dict], bool]:
        """Return (items, has_next)."""
        soup = self._make_soup(raw)
        if not soup:
            return [], False
        items = []
        for card in soup.find_all("a", class_="card"):
            href = card.get("href", "")
            if not href:
                continue
            url = href if href.startswith("http") else f"{self.base_url}{href}"
            title_el = card.find(class_="card__title")
            title = self._clean(title_el.get_text()) if title_el else ""
            if not title:
                continue
            summ_el = card.find(class_="card__content") or card.find(class_="card__summary")
            card_summary = self._clean(summ_el.get_text()) if summ_el else ""
            date_el = card.find("time")
            listed_date = date_el.get("datetime", "")[:10] if date_el else ""
            cat_el = card.find(class_="field-publication-type")
            category = self._clean(cat_el.get_text()) if cat_el else ""
            items.append({
                "url": url,
                "title": title,
                "card_summary": card_summary,
                "listed_date": listed_date,
                "category": category,
            })
        has_next = bool(soup.find("a", rel="next"))
        return items, has_next

    def _parse_detail(self, raw: str, card_summary: str) -> dict:
        soup = self._make_soup(raw)
        if not soup:
            return {}
        main = soup.find("main") or soup

        # Published date
        pub_date = ""
        dt_el = main.find(class_="published-date")
        if dt_el:
            t = dt_el.find("time")
            if t:
                pub_date = t.get("datetime", "")[:10]

        # Topics
        topics = []
        for el in main.find_all(class_="site-topics--item"):
            t = self._clean(el.get_text()).strip(",")
            if t:
                topics.append(t)

        # PDF links — collect all file attachments
        pdf_url = None
        original_filename = None
        all_files = []
        doc_titles = []
        for a in main.find_all("a", class_="file-link"):
            href = a.get("href", "")
            if not href:
                continue
            full_url = href if href.startswith("http") else f"{self.base_url}{href}"
            all_files.append(full_url)
            # Link title: strip the .meta span (file type / size)
            a_clone = self._make_soup(str(a))
            if a_clone:
                meta_span = a_clone.find(class_="meta")
                if meta_span:
                    meta_span.decompose()
                lt = self._clean(a_clone.get_text())
                if lt:
                    doc_titles.append(lt)
            if pdf_url is None and ".pdf" in href.lower():
                pdf_url = full_url
                path = urlparse(full_url).path
                fname = path.split("/")[-1].split("?")[0]
                try:
                    original_filename = unquote(fname)
                except Exception:
                    original_filename = fname

        # Abstract: page-summary + doc titles (to reliably clear 100 chars)
        summ_el = main.find(class_="page-summary")
        page_summary = self._clean(summ_el.get_text()) if summ_el else ""
        if not page_summary:
            page_summary = card_summary

        abstract_parts = [page_summary] if page_summary else []
        if doc_titles:
            abstract_parts.append("Documents: " + "; ".join(doc_titles))
        abstract = " ".join(abstract_parts)

        node_id = self._node_id(raw)

        return {
            "abstract": abstract,
            "pub_date": pub_date,
            "topics": topics,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "all_files": all_files,
            "node_id": node_id,
        }

    # ------------------------------------------------------------------ #
    # Crawl                                                                #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        page = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - start_time > self._CRAWL_TIMEOUT:
                print(f"[{self.site_id}] 25-minute budget exceeded at page {page}. Stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            list_url = f"{self._LIST_URL}?page={page}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items, has_next = self._parse_list_page(raw)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break
            for it in items:
                seen_urls.add(it["url"])

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            for it in new_items:
                if limit is not None and saved >= limit:
                    break
                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(it["url"])
                    if not detail_raw:
                        print(f"[{self.site_id}] Failed to fetch detail: {it['url']}")
                        continue

                    d = self._parse_detail(detail_raw, it["card_summary"])
                    abstract = d.get("abstract", "")

                    if len(abstract) < 100:
                        print(f"[{self.site_id}] Skipping (abstract {len(abstract)} chars): {it['title'][:60]}")
                        continue

                    node_id = d.get("node_id")
                    slug = urlparse(it["url"]).path.rstrip("/").split("/")[-1]
                    external_id = node_id or slug
                    topics = d.get("topics", [])
                    pub_date = d.get("pub_date") or it["listed_date"]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": node_id,
                        "title": it["title"],
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": it["listed_date"],
                        "url": it["url"],
                        "pdf_url": d.get("pdf_url"),
                        "original_filename": d.get("original_filename"),
                        "category": it["category"],
                        "keywords": ",".join(topics),
                        "publisher": "Department of Finance, Northern Ireland",
                        "metadata": json.dumps({
                            "node_id": node_id,
                            "slug": slug,
                            "topics": topics,
                            "all_files": d.get("all_files", []),
                            "posted_date": it["listed_date"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {it['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {it.get('url', '?')} failed: {exc}")
                    continue

            if not has_next:
                print(f"[{self.site_id}] No next page after page {page}. Done.")
                break
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
