# -*- coding: utf-8 -*-
"""통일부 뉴스레터 BBS crawler.

Target: https://www.unikorea.go.kr/web/unikorea/bbs/bbs_0000000000000181
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


def _make_soup(html: str):
    """Parse HTML with fallback parser chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup("", "html.parser")


class UnikoreaNewsletterCrawler(BaseCrawler):
    """통일부 뉴스레터 BBS crawler."""

    site_id = "unikorea-go-kr-web"
    site_name = "Custom: unikorea-go-kr-web"
    base_url = "https://www.unikorea.go.kr"

    _BBS_ID = "bbs_0000000000000181"
    _LIST_BASE = "https://www.unikorea.go.kr/web/unikorea/bbs/bbs_0000000000000181"
    _PAGE_SIZE = 15
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _BUDGET_MINUTES = 25

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with TLS 1.3 and retry. Returns decoded text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = r.stdout
                if raw and raw.strip():
                    return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = 2 ** attempt
                    print(f"[{self.site_id}] empty response attempt {attempt+1}/3, retry in {wait}s")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    print(f"[{self.site_id}] curl timeout attempt {attempt+1}/3, retrying")
                    time.sleep(3 ** attempt)
            except Exception as exc:
                if attempt < 2:
                    time.sleep(3 ** attempt)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _build_abstract(self, soup) -> str:
        """Build abstract from og:description, with image-map alt text supplement."""
        abstract = ""

        # Primary: og:description meta tag
        og = soup.find("meta", attrs={"property": "og:description"})
        if og:
            content = og.get("content", "")
            abstract = re.sub(r"^통일부,\s*", "", content).strip()

        # Fallback: name=description
        if len(abstract) < 50:
            nd = soup.find("meta", attrs={"name": "description"})
            if nd:
                content = nd.get("content", "")
                alt = re.sub(r"^통일부,\s*", "", content).strip()
                if len(alt) > len(abstract):
                    abstract = alt

        # Supplement: image-map area alt texts (newsletter content headings)
        board = soup.find("div", class_="board_content")
        if board:
            _SKIP = {"페이스북", "X", "유튜브", "블로그", "인스타", "트위터",
                     "twitter", "facebook", "youtube", "instagram"}
            areas = []
            for area in board.find_all("area"):
                alt = (area.get("alt") or "").strip()
                if alt and len(alt) > 3 and alt.lower() not in _SKIP:
                    areas.append(alt)
            if areas:
                supplement = " | ".join(areas)
                if abstract:
                    abstract = abstract + "\n\n[주요 내용] " + supplement
                else:
                    abstract = "[주요 내용] " + supplement

        # Last resort: visible text from board content
        if len(abstract) < 50 and board:
            text = self._strip_html(str(board))
            if len(text) > len(abstract):
                abstract = text

        return abstract

    def _parse_list_page(self, html: str) -> list:
        """Parse list page. Returns list of {post_id, title, url, listed_date}."""
        items = []
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] list page parse error: {exc}")
            return items

        for row in soup.select("tr"):
            link = row.select_one("td.title a.art")
            if not link:
                continue
            href = link.get("href", "")
            title = link.get_text(strip=True)

            m = re.search(r"/bbs_0000000000000181/([^/?&#]+)", href)
            if not m:
                continue
            post_id = m.group(1)

            date_cell = row.select_one("td.created")
            listed_date = date_cell.get_text(strip=True) if date_cell else ""

            full_url = (
                f"https://www.unikorea.go.kr{href}"
                if href.startswith("/") else href
            )
            items.append({
                "post_id": post_id,
                "title": title,
                "url": full_url,
                "listed_date": listed_date,
            })

        return items

    def _parse_detail(self, html: str, post_id: str) -> dict | None:
        """Parse detail page. Returns field dict or None on failure."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] detail page parse error ({post_id}): {exc}")
            return None

        # Title
        h3 = soup.find("h3", id="aticleTitle") or soup.find("h3", class_="detail-title")
        title = h3.get_text(strip=True) if h3 else ""

        # 등록일 (published date) and 작성자 (author) from <dl> structure
        published_date = ""
        author = ""
        for dl in soup.find_all("dl"):
            dt = dl.find("dt")
            if not dt:
                continue
            label = dt.get_text(strip=True)
            dd = dl.find("dd")
            val = dd.get_text(strip=True) if dd else ""
            if "등록일" in label and not published_date:
                published_date = val
            elif "작성자" in label and not author:
                author = val

        # Abstract
        abstract = self._build_abstract(soup)

        # PDF / file attachments
        pdf_url = None
        original_filename = None
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if re.search(r"\.(pdf|hwp|hwpx)(\?|$)", href, re.IGNORECASE):
                pdf_url = (
                    "https://www.unikorea.go.kr" + href
                    if href.startswith("/") else href
                )
                fname = href.rsplit("/", 1)[-1].split("?")[0]
                original_filename = fname if fname else None
                break

        return {
            "title": title,
            "published_date": published_date,
            "author": author,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 통일부 뉴스레터 BBS and save records to DB.

        Parameters
        ----------
        limit : int or None
            Maximum number of items to save. None = unlimited.
        """
        saved = 0
        page = 1
        seen_urls: set = set()
        start_ts = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if (time.time() - start_ts) / 60 >= self._BUDGET_MINUTES:
                print(
                    f"[{self.site_id}] {self._BUDGET_MINUTES}-min budget reached "
                    f"at page {page}. Exiting cleanly."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            list_url = (
                f"{self._LIST_BASE}?cp={page}"
                f"&searchKeyword=&searchCondition=&pageSize={self._PAGE_SIZE}"
                f"&sortOrder=BA_REGDATE&sortDirection=DESC"
                f"&bcId={self._BBS_ID}"
                f"&baNotice=false&baCommSelec=false&baOpenDay=false&baUse=true"
            )

            list_html = self._curl_get(list_url)
            if not list_html:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(list_html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # URL deduplication — detect silent paginator loops
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break

            for it in new_items:
                seen_urls.add(it["url"])

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                post_id = item["post_id"]
                detail_url = item["url"]

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(detail_url)
                    if not detail_html:
                        print(f"[{self.site_id}] item {post_id} failed: empty response")
                        continue

                    detail = self._parse_detail(detail_html, post_id)
                    if not detail:
                        print(f"[{self.site_id}] item {post_id} failed: parse returned None")
                        continue

                    title = detail.get("title") or item.get("title") or ""
                    abstract = detail.get("abstract", "")
                    published_date = detail.get("published_date") or item.get("listed_date") or ""
                    listed_date = item.get("listed_date") or published_date
                    author = detail.get("author", "")
                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename")

                    if not title:
                        print(f"[{self.site_id}] item {post_id} skipped: no title")
                        continue

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {post_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "post_number": post_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": author or None,
                        "publisher": "통일부",
                        "department": None,
                        "journal": None,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": None,
                        "keywords": "통일부,뉴스레터",
                        "category": "뉴스레터",
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "bbs_id": self._BBS_ID,
                            "post_id": post_id,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {post_id} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
