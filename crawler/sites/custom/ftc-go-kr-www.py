# -*- coding: utf-8 -*-
"""공정거래위원회 (FTC) 보도자료 crawler.

Target: https://www.ftc.go.kr/www/selectBbsNttList.do?bordCd=3&key=12&searchCtgry=01,02
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


class FtcGovKrWwwCrawler(BaseCrawler):
    """공정거래위원회 보도자료 / 참고자료 crawler."""

    site_id = "ftc-go-kr-www"
    site_name = "Custom: ftc-go-kr-www"
    base_url = "https://www.ftc.go.kr"

    _LIST_BASE = "https://www.ftc.go.kr/www/selectBbsNttList.do"
    _DETAIL_BASE = "https://www.ftc.go.kr/www/selectBbsNttView.do"
    _FILE_BASE = "https://www.ftc.go.kr/www/"
    _LIST_PARAMS = "pageUnit=10&searchCnd=all&key=12&bordCd=3&searchCtgry=01,02"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl (TLSv1.2) with 3 retries and exponential backoff.

        Returns decoded text or None on total failure.
        """
        cmd = [
            "curl", "-sk", "--tlsv1.2", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
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
                    print(f"[ftc-go-kr-www] Empty response (attempt {attempt+1}/3), "
                          f"retrying in {waits[attempt]}s...")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[ftc-go-kr-www] curl error: {exc}, "
                          f"retrying in {waits[attempt]}s...")
                    time.sleep(waits[attempt])
                else:
                    print(f"[ftc-go-kr-www] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return list of item dicts from a list-page HTML string."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[ftc-go-kr-www] List-page parse error: {exc}")
            return []

        items = []
        table = soup.find("table")
        if not table:
            return items

        for row in table.find_all("tr"):
            link = row.find("a", href=lambda x: x and "nttSn=" in str(x))
            if not link:
                continue
            href = link.get("href", "")
            m = re.search(r"nttSn=(\d+)", href)
            if not m:
                continue
            ntt_sn = m.group(1)
            title = link.find("span", class_="p-table__text")
            title = title.get_text(strip=True) if title else link.get_text(strip=True)

            cells = row.find_all("td")
            category = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            department = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            pub_date = cells[4].get_text(strip=True) if len(cells) > 4 else ""

            detail_url = (
                f"{self._DETAIL_BASE}?pageUnit=10&pageIndex=1"
                f"&searchCnd=all&key=12&bordCd=3&searchCtgry=01,02&nttSn={ntt_sn}"
            )
            items.append({
                "ntt_sn": ntt_sn,
                "title": title,
                "category": category,
                "department": department,
                "pub_date": pub_date,
                "url": detail_url,
            })
        return items

    def _parse_detail(self, html, ntt_sn):
        """Return dict with abstract, pdf_url, attached_files or None on parse failure."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[ftc-go-kr-www] Detail-page parse error ({ntt_sn}): {exc}")
            return None

        # Primary content cell
        content_td = soup.find("td", class_="p-table__content")
        if not content_td:
            content_td = soup.find("div", class_="bbs__view")
        abstract = content_td.get_text(separator="\n", strip=True) if content_td else ""

        # Attachment links (all files)
        pdf_url = ""
        attached_files = []
        for al in soup.find_all("a", class_="p-attach__link"):
            href = al.get("href", "")
            if not href:
                continue
            fname = al.get_text(strip=True)
            # Resolve relative URL
            if href.startswith("./"):
                full_url = self._FILE_BASE + href[2:]
            elif href.startswith("/"):
                full_url = "https://www.ftc.go.kr" + href
            else:
                full_url = urljoin(self._FILE_BASE, href)
            attached_files.append({"name": fname, "url": full_url})
            # First PDF wins
            if not pdf_url and (".pdf" in fname.lower() or ".pdf" in href.lower()):
                pdf_url = full_url

        return {
            "abstract": abstract,
            "pdf_url": pdf_url,
            "attached_files": attached_files,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl FTC press-release list pages and save each article.

        Parameters
        ----------
        limit:
            Maximum records to save. None means crawl all pages.
        """
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._WALL_SECONDS:
                print(f"[ftc-go-kr-www] 25-minute wall-clock limit reached. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[ftc-go-kr-www] Safety cap of {self._MAX_PAGES} pages reached. Exiting.")
                break

            if page % 10 == 1 and page > 1:
                print(f"[ftc-go-kr-www] page {page}: saved {saved}/{limit_str}")

            # Fetch list page with retries
            list_url = f"{self._LIST_BASE}?{self._LIST_PARAMS}&pageIndex={page}"
            raw = None
            for attempt in range(3):
                raw = self._curl_get(list_url)
                if raw:
                    break
                wait = [1, 3, 9][attempt]
                if attempt < 2:
                    print(f"[ftc-go-kr-www] List page {page} empty "
                          f"(attempt {attempt+1}/3), retrying in {wait}s...")
                    time.sleep(wait)

            if not raw:
                print(f"[ftc-go-kr-www] Could not fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[ftc-go-kr-www] No items at page {page}. Done.")
                break

            # URL deduplication — detect pagination loops
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[ftc-go-kr-www] Page {page}: all items already seen. Stopping.")
                break
            for it in items:
                seen_urls.add(it["url"])

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                ntt_sn = item["ntt_sn"]
                try:
                    time.sleep(self._delay)

                    # Fetch detail with retries
                    detail_raw = None
                    for attempt in range(3):
                        detail_raw = self._curl_get(item["url"])
                        if detail_raw:
                            break
                        wait = [1, 3, 9][attempt]
                        if attempt < 2:
                            print(f"[ftc-go-kr-www] Detail {ntt_sn} fetch failed "
                                  f"(attempt {attempt+1}/3), retrying in {wait}s...")
                            time.sleep(wait)

                    if not detail_raw:
                        print(f"[ftc-go-kr-www] item {ntt_sn} failed: could not fetch detail")
                        continue

                    parsed = self._parse_detail(detail_raw, ntt_sn)
                    if parsed is None:
                        print(f"[ftc-go-kr-www] item {ntt_sn} failed: detail parse returned None")
                        continue

                    abstract = parsed["abstract"]
                    if len(abstract) < 50:
                        print(f"[ftc-go-kr-www] item {ntt_sn}: abstract too short "
                              f"({len(abstract)} chars), skipping")
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": ntt_sn,
                        "title": item["title"],
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": item["category"],
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": item["pub_date"],
                        "url": item["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": "",
                        "department": item["department"],
                        "metadata": json.dumps(
                            {"attachedFiles": parsed["attached_files"]},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[ftc-go-kr-www] Saved {saved}/{limit_str}: "
                          f"{item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ftc-go-kr-www] item {ntt_sn} failed: {exc}")
                    continue

            page += 1

        print(f"[ftc-go-kr-www] Done. Total saved: {saved}")
        return saved
