# -*- coding: utf-8 -*-
"""PBOC Research Institute (pbocri.org.cn) Academic Papers crawler.

Target landing page: https://www.pbocri.org.cn/xslw.html

Two subsections crawled in order:
  1. 期刊论文 (Journal papers) via qklw.html  — HTML detail pages with full text
  2. 工作论文 (Working papers) via rmyhgzlw.html — direct PDF links, no HTML abstract
     (working papers are skipped: abstract < 100 chars)
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


def _make_soup(html):
    """BeautifulSoup with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


class PbocriOrgCnXslwhtmlCrawler(BaseCrawler):
    """中国人民银行金融研究所 学术论文 crawler."""

    site_id = "pbocri-org-cn-xslwhtml"
    site_name = "Custom: pbocri-org-cn-xslwhtml"
    base_url = "https://www.pbocri.org.cn"

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl (TLS ≤1.3) with 3 retries and exponential backoff.

        Returns decoded text or None on total failure.
        """
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw:
                    return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    print(f"[pbocri-org-cn-xslwhtml] Empty response (attempt {attempt+1}/3), "
                          f"retrying in {waits[attempt]}s...")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[pbocri-org-cn-xslwhtml] curl error: {exc}, "
                          f"retrying in {waits[attempt]}s...")
                    time.sleep(waits[attempt])
                else:
                    print(f"[pbocri-org-cn-xslwhtml] curl failed after 3 attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Parse a gdItem list page (works for both qklw and rmyhgzlw).

        Returns list of item dicts:
          doc_id, title, url, is_pdf, pdf_url, snippet, pub_date
        """
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[pbocri-org-cn-xslwhtml] List page parse error: {exc}")
            return []

        items = []
        for li in soup.select("ul.gdItem li"):
            h1 = li.find("h1")
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue

            href = a.get("href", "").strip()
            if not href or href.startswith("javascript:"):
                continue

            # Normalize to absolute URL
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://www.pbocri.org.cn" + href
            elif not href.startswith("http"):
                href = urljoin("https://www.pbocri.org.cn/", href)

            title = a.get_text(strip=True) or ""
            is_pdf = ".pdf" in href.lower()

            # Derive external_id
            if is_pdf:
                m = re.search(r"/([a-f0-9]{32})\.pdf", href, re.IGNORECASE)
                doc_id = m.group(1) if m else re.sub(r"[^a-zA-Z0-9_-]", "_", href.split("/")[-1][:64])
            else:
                m = re.search(r"/(\d+)\.html", href)
                doc_id = m.group(1) if m else None

            if not doc_id:
                continue

            snippet_p = li.find("p", class_="ellipsis2")
            snippet = snippet_p.get_text(strip=True) if snippet_p else ""

            # Date may be in a <span> (often commented out in the HTML but present)
            span = li.find("span")
            pub_date = span.get_text(strip=True) if span else ""

            items.append({
                "doc_id": doc_id,
                "title": title,
                "url": href,
                "is_pdf": is_pdf,
                "pdf_url": href if is_pdf else "",
                "snippet": snippet,
                "pub_date": pub_date,
            })
        return items

    def _parse_detail(self, html, doc_id):
        """Parse a journal paper detail page.

        Returns dict: title, author, pub_date, abstract — or None on failure.
        """
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[pbocri-org-cn-xslwhtml] Detail parse error ({doc_id}): {exc}")
            return None

        # Title
        title_el = soup.find("h1", class_="title")
        title = title_el.get_text(strip=True) if title_el else ""

        # Author and date from .info div
        author = ""
        pub_date = ""
        info_div = soup.find("div", class_="info")
        if info_div:
            info_text = info_div.get_text(separator=" ", strip=True)
            m = re.search(r"作者[：:]\s*(.+?)(?:\s{2,}|\s+发布时间|$)", info_text)
            if m:
                author = m.group(1).strip()
            m = re.search(r"发布时间[：:]\s*(\d{4}-\d{2}-\d{2})", info_text)
            if m:
                pub_date = m.group(1)

        # Full text from detail div (note: typo "detial" in the site's HTML)
        abstract = ""
        detail_div = soup.find("div", class_="detial")
        if detail_div:
            for tag in detail_div.find_all(["script", "style", "noscript"]):
                tag.decompose()
            abstract = detail_div.get_text(separator="\n", strip=True)
            abstract = re.sub(r"\n{3,}", "\n\n", abstract).strip()

        # Fallback: meta description tag
        if len(abstract) < 100:
            meta = soup.find("meta", attrs={"name": "description"})
            if meta:
                meta_content = meta.get("content", "").strip()
                if len(meta_content) > len(abstract):
                    abstract = meta_content

        return {
            "title": title,
            "author": author,
            "pub_date": pub_date,
            "abstract": abstract,
        }

    def _has_next_page(self, html):
        """Return True if there is an enabled next-page (») button."""
        try:
            soup = _make_soup(html)
        except Exception:
            return False
        pagebox = soup.find("div", id="pagebox")
        if not pagebox:
            return False
        pagination = pagebox.find("ul", class_="pagination")
        if not pagination:
            return False
        lis = pagination.find_all("li")
        if not lis:
            return False
        last_li = lis[-1]
        # disabled class means we are on the last page
        if "disabled" in last_li.get("class", []):
            return False
        return True

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 学术论文 sections and save journal articles.

        Parameters
        ----------
        limit:
            Maximum records to save. None means crawl all pages.
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # Sections in priority order: journal papers first (have real abstracts),
        # then working papers (direct PDFs — usually skipped for short abstract).
        sections = [
            ("期刊论文", "https://www.pbocri.org.cn/qklw.html"),
            ("工作论文", "https://www.pbocri.org.cn/rmyhgzlw.html"),
        ]

        for category, base_list_url in sections:
            if time.time() - start_time > self._WALL_SECONDS:
                print(f"[pbocri-org-cn-xslwhtml] 25-minute wall-clock limit reached. Exiting.")
                return saved
            if limit is not None and saved >= limit:
                return saved

            print(f"[pbocri-org-cn-xslwhtml] Crawling section: {category} ({base_list_url})")
            page = 1

            while True:
                # Wall-clock budget
                if time.time() - start_time > self._WALL_SECONDS:
                    print(f"[pbocri-org-cn-xslwhtml] 25-minute wall-clock limit reached. Exiting.")
                    return saved

                if limit is not None and saved >= limit:
                    break

                if page > self._MAX_PAGES:
                    print(f"[pbocri-org-cn-xslwhtml] Safety cap of {self._MAX_PAGES} pages reached.")
                    break

                if page % 10 == 0:
                    print(f"[pbocri-org-cn-xslwhtml] page {page}: saved {saved}/{limit_str}")

                list_url = base_list_url if page == 1 else f"{base_list_url}?page={page}"

                raw = None
                for attempt in range(3):
                    raw = self._curl_get(list_url)
                    if raw:
                        break
                    wait = [1, 3, 9][attempt]
                    if attempt < 2:
                        print(f"[pbocri-org-cn-xslwhtml] List page {page} empty "
                              f"(attempt {attempt+1}/3), retrying in {wait}s...")
                        time.sleep(wait)

                if not raw:
                    print(f"[pbocri-org-cn-xslwhtml] Could not fetch list page {page}. Stopping section.")
                    break

                items = self._parse_list_page(raw)
                if not items:
                    print(f"[pbocri-org-cn-xslwhtml] No items at page {page}. Done with section.")
                    break

                new_items = [it for it in items if it["url"] not in seen_urls]
                if not new_items:
                    print(f"[pbocri-org-cn-xslwhtml] Page {page}: all items already seen. Stopping.")
                    break
                for it in items:
                    seen_urls.add(it["url"])

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break

                    doc_id = item["doc_id"]

                    try:
                        if item["is_pdf"]:
                            # Working paper: only a PDF URL available, no HTML abstract.
                            # The title alone is typically < 100 chars → item is skipped.
                            abstract = item["title"]
                            if len(abstract) < 100:
                                print(f"[pbocri-org-cn-xslwhtml] WP {doc_id}: "
                                      f"abstract too short ({len(abstract)} chars), skipping")
                                continue

                            paper = {
                                "id": None,
                                "site_id": self.site_id,
                                "external_id": doc_id,
                                "title": item["title"],
                                "authors": json.dumps([], ensure_ascii=False),
                                "abstract": abstract,
                                "category": category,
                                "keywords": json.dumps([], ensure_ascii=False),
                                "published_date": item["pub_date"] or None,
                                "url": item["url"],
                                "pdf_url": item["pdf_url"],
                                "doi": "",
                                "department": "中国人民银行金融研究所",
                                "metadata": json.dumps({}, ensure_ascii=False),
                            }
                            self._save_paper(paper)
                            saved += 1
                            print(f"[pbocri-org-cn-xslwhtml] Saved {saved}/{limit_str}: "
                                  f"{item['title'][:60]}")

                        else:
                            # Journal paper: fetch detail page for full text.
                            time.sleep(self._delay)

                            detail_raw = None
                            for attempt in range(3):
                                detail_raw = self._curl_get(item["url"])
                                if detail_raw:
                                    break
                                wait = [1, 3, 9][attempt]
                                if attempt < 2:
                                    print(f"[pbocri-org-cn-xslwhtml] Detail {doc_id} fetch failed "
                                          f"(attempt {attempt+1}/3), retrying in {wait}s...")
                                    time.sleep(wait)

                            if not detail_raw:
                                print(f"[pbocri-org-cn-xslwhtml] item {doc_id} failed: "
                                      f"could not fetch detail")
                                continue

                            parsed = self._parse_detail(detail_raw, doc_id)
                            if parsed is None:
                                print(f"[pbocri-org-cn-xslwhtml] item {doc_id}: "
                                      f"detail parse returned None, skipping")
                                continue

                            abstract = parsed["abstract"]
                            if len(abstract) < 100:
                                print(f"[pbocri-org-cn-xslwhtml] item {doc_id}: "
                                      f"abstract too short ({len(abstract)} chars), skipping")
                                continue

                            title = parsed["title"] or item["title"]
                            pub_date = parsed["pub_date"] or item["pub_date"] or None
                            authors_list = [parsed["author"]] if parsed["author"] else []

                            paper = {
                                "id": None,
                                "site_id": self.site_id,
                                "external_id": doc_id,
                                "title": title,
                                "authors": json.dumps(authors_list, ensure_ascii=False),
                                "abstract": abstract,
                                "category": category,
                                "keywords": json.dumps([], ensure_ascii=False),
                                "published_date": pub_date,
                                "url": item["url"],
                                "pdf_url": "",
                                "doi": "",
                                "department": "中国人民银行金融研究所",
                                "metadata": json.dumps(
                                    {"snippet": item["snippet"]},
                                    ensure_ascii=False,
                                ),
                            }
                            self._save_paper(paper)
                            saved += 1
                            print(f"[pbocri-org-cn-xslwhtml] Saved {saved}/{limit_str}: "
                                  f"{title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[pbocri-org-cn-xslwhtml] item {doc_id} failed: {exc}")
                        continue

                if not self._has_next_page(raw):
                    print(f"[pbocri-org-cn-xslwhtml] No more pages after page {page}.")
                    break
                page += 1

        print(f"[pbocri-org-cn-xslwhtml] Done. Total saved: {saved}")
        return saved
