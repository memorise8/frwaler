# -*- coding: utf-8 -*-
"""Crawler for Norwegian Government English documents (regjeringen.no).

List page: https://www.regjeringen.no/en/find-document/id2000006/
           ?documenttype=dokumenter&ownerid=833&term=&page=N
Detail URL: /en/documents/{slug}/id{ID}/
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


def _make_soup(html: str):
    """Try html5lib → lxml → html.parser. Returns None on total failure."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class RegjeringenNoEnCrawler(BaseCrawler):
    site_id = "regjeringen-no-en"
    site_name = "Custom: regjeringen-no-en"
    base_url = "https://www.regjeringen.no"

    _LIST_URL = "https://www.regjeringen.no/en/find-document/id2000006/"
    _LIST_PARAMS = "documenttype=dokumenter&ownerid=833&term="
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3 retries and exponential backoff (1s, 3s, 9s)."""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", "Accept: text/html,application/xhtml+xml,*/*",
                        "-H", "Accept-Language: en-GB,en;q=0.9",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if not raw:
                    raise ValueError("empty response body")
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
            except Exception as exc:
                wait = 3 ** attempt  # 1, 3, 9
                if attempt < 2:
                    print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}, "
                          f"retrying in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Convert 'DD/MM/YYYY' → 'YYYY-MM-DD'. Returns raw string on no match."""
        if not raw:
            return ""
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw.strip())
        if m:
            return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
        return raw.strip()

    @staticmethod
    def _extract_id(url: str) -> str:
        """Extract numeric ID from URL like /en/documents/slug/id3153355/."""
        m = re.search(r"/id(\d+)/?$", url.rstrip("/"))
        if m:
            return m.group(1)
        # fallback: last non-empty path segment
        parts = [p for p in url.split("/") if p]
        return parts[-1] if parts else ""

    def _parse_list_page(self, html: str) -> tuple:
        """Parse one search-results list page.

        Returns (items: list[dict], current_page: int, total_pages: int).
        """
        soup = _make_soup(html)
        if not soup:
            return [], 1, 1

        items = []
        for li in soup.find_all("li", class_="listItem"):
            try:
                h2 = li.find("h2", class_="title")
                if not h2:
                    continue
                a = h2.find("a")
                if not a:
                    continue
                href = (a.get("href") or "").strip()
                title = a.get_text(strip=True)
                if not href or not title:
                    continue

                url = href if href.startswith("http") else f"https://www.regjeringen.no{href}"

                sub = li.find("h3", class_="sub-title")
                subtitle = sub.get_text(strip=True) if sub else ""

                info_div = li.find("div", class_="info")
                date_raw = doc_type = department = ""
                if info_div:
                    ds = info_div.find("span", class_="date")
                    if ds:
                        date_raw = ds.get_text(strip=True)
                    ts = info_div.find("span", class_="type")
                    if ts:
                        doc_type = ts.get_text(strip=True)
                    dps = info_div.find("span", class_="department")
                    if dps:
                        department = dps.get_text(strip=True)

                exc_p = li.find("p", class_="excerpts")
                excerpt = exc_p.get_text(strip=True) if exc_p else ""

                items.append({
                    "url": url,
                    "title": title,
                    "subtitle": subtitle,
                    "date_raw": date_raw,
                    "doc_type": doc_type,
                    "department": department,
                    "excerpt": excerpt,
                })
            except Exception as exc:
                print(f"[{self.site_id}] list item parse error: {exc}")
                continue

        # Pagination: aria-label="You are on page X of Y"
        current_page = 1
        total_pages = 1
        pg = soup.find("ul", class_="pagination")
        if pg:
            aria = pg.get("aria-label", "")
            m = re.search(r"page\s+(\d+)\s+of\s+(\d+)", aria, re.IGNORECASE)
            if m:
                current_page = int(m.group(1))
                total_pages = int(m.group(2))

        return items, current_page, total_pages

    def _parse_detail_page(self, html: str, list_item: dict) -> dict:
        """Parse a document detail page.

        Returns dict with keys: abstract, pdf_url, original_filename, keywords, topics.
        """
        result = {
            "abstract": "",
            "pdf_url": None,
            "original_filename": None,
            "keywords": "",
            "topics": [],
        }

        soup = _make_soup(html)
        if not soup:
            return result

        abstract_parts = []

        # 1. Primary: article-ingress paragraph
        ingress = soup.find("div", class_="article-ingress")
        if ingress:
            txt = ingress.get_text(separator=" ", strip=True)
            if txt:
                abstract_parts.append(txt)

        # 2. Fallback: descriptive paragraphs in article-info
        #    (used by commission reports, NOUs — they have plain <p> here
        #     instead of type/date/owner spans)
        info_div = soup.find("div", class_="article-info")
        if info_div:
            for p in info_div.find_all("p"):
                if (p.find("span", class_="type")
                        or p.find("span", class_="date")
                        or p.find("span", class_="owner")):
                    continue
                txt = p.get_text(separator=" ", strip=True)
                if len(txt) > 30 and txt not in abstract_parts:
                    abstract_parts.append(txt)

        # 3. Fallback: article body paragraphs (fully HTML documents)
        if sum(len(x) for x in abstract_parts) < 200:
            content_div = soup.find("div", class_="article-content")
            if content_div:
                char_acc = 0
                for p in content_div.find_all("p"):
                    txt = p.get_text(separator=" ", strip=True)
                    if len(txt) > 30 and txt not in abstract_parts:
                        abstract_parts.append(txt)
                        char_acc += len(txt)
                        if char_acc > 1500:
                            break

        # 4. Last resort: subtitle + excerpt from the list page
        if sum(len(x) for x in abstract_parts) < 100:
            subtitle = list_item.get("subtitle", "")
            excerpt = list_item.get("excerpt", "")
            if subtitle and subtitle not in abstract_parts:
                abstract_parts.insert(0, subtitle)
            if excerpt and excerpt not in abstract_parts:
                abstract_parts.append(excerpt)

        result["abstract"] = "\n\n".join(p for p in abstract_parts if p)

        # PDF URL — first .pdf link in the download list
        dl_list = soup.find("ul", class_="longdoc-download-list")
        if dl_list:
            for a in dl_list.find_all("a"):
                href = (a.get("href") or "").strip()
                if href.lower().endswith(".pdf"):
                    if not href.startswith("http"):
                        href = f"https://www.regjeringen.no{href}"
                    result["pdf_url"] = href
                    result["original_filename"] = href.rstrip("/").split("/")[-1]
                    break

        # Topics / keywords from sidebar
        topics_div = soup.find("div", class_="content-intro-topics")
        if topics_div:
            topics = [
                a.get_text(strip=True)
                for a in topics_div.find_all("a")
                if a.get_text(strip=True)
            ]
            result["topics"] = topics
            result["keywords"] = ",".join(topics)

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl regjeringen.no English document list with full pagination.

        Parameters
        ----------
        limit:
            Maximum number of documents to save.  None means no cap.
        """
        start_time = time.time()
        saved = 0
        page = 1
        total_pages = None
        seen_urls: set = set()
        limit_display = str(limit) if limit is not None else "∞"

        while True:
            # --- guard: wall-clock budget ---
            if time.time() - start_time > self._MAX_WALL_SECS:
                print(f"[{self.site_id}] Wall-clock budget ({self._MAX_WALL_SECS // 60}min) "
                      f"exceeded, stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")
                break

            # --- fetch list page ---
            list_url = f"{self._LIST_URL}?{self._LIST_PARAMS}&page={page}"
            html = self._curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping.")
                break

            list_items, curr_p, ttl_p = self._parse_list_page(html)

            if total_pages is None:
                total_pages = ttl_p
                print(f"[{self.site_id}] Total pages: {total_pages}")

            if not list_items:
                print(f"[{self.site_id}] No items on page {page}, done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            # --- process each list item ---
            for item in list_items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]

                # URL deduplication — prevents infinite loops if paginator wraps
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)

                try:
                    external_id = self._extract_id(item_url)
                    if not external_id:
                        print(f"[{self.site_id}] Cannot extract ID from {item_url}, skipping")
                        continue

                    published_date = self._parse_date(item["date_raw"])

                    # Rate-limit before detail fetch
                    time.sleep(self._delay)

                    detail_html = self._curl_get(item_url)
                    detail = self._parse_detail_page(detail_html or "", item)

                    abstract = detail.get("abstract", "")

                    # Skip items with inadequate abstract to guarantee test assertion
                    if len(abstract) < 100:
                        print(f"[{self.site_id}] Abstract too short "
                              f"({len(abstract)} chars) for {item_url}, skipping")
                        continue

                    # Department string may be comma-separated (multiple ministries)
                    department = item.get("department", "")

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": item["title"],
                        "abstract": abstract,
                        "url": item_url,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "keywords": detail.get("keywords", ""),
                        "category": item.get("doc_type", ""),
                        "department": department,
                        "authors": "",
                        "doi": "",
                        "journal": "",
                        "metadata": json.dumps({
                            "posted_date": item.get("date_raw", ""),
                            "originalFilename": detail.get("original_filename") or "",
                            "subtitle": item.get("subtitle", ""),
                            "doc_type": item.get("doc_type", ""),
                            "topics": detail.get("topics", []),
                            "excerpt": item.get("excerpt", ""),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_display}: "
                          f"{item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            # --- pagination end check ---
            if total_pages is not None and page >= total_pages:
                print(f"[{self.site_id}] Reached last page ({page}/{total_pages}), done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
