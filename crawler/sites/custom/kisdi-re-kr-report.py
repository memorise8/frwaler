# -*- coding: utf-8 -*-
"""KISDI Premium Report (www.kisdi.re.kr) crawler.

List:   GET  https://www.kisdi.re.kr/report/list.do?key=KEY&arrMasterId=MID&pageIndex=N
Detail: POST https://www.kisdi.re.kr/report/view.do?key=KEY&masterId=MID&arrMasterId=MID&artId=AID
        body: pageIndex=1
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_KEY = "m2101113024153"
_MASTER_ID = "3934560"
_LIST_URL = "https://www.kisdi.re.kr/report/list.do"
_DETAIL_URL = "https://www.kisdi.re.kr/report/view.do"
_FILE_DOWN = "https://www.kisdi.re.kr/report/fileDown.do"


def _make_soup(html: str):
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class KisdiReKrReportCrawler(BaseCrawler):
    site_id = "kisdi-re-kr-report"
    site_name = "Custom: kisdi-re-kr-report"
    base_url = "https://www.kisdi.re.kr"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, method: str, url: str, post_fields: dict | None = None) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-X", method,
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Referer: https://www.kisdi.re.kr/report/list.do",
        ]
        if post_fields:
            cmd += ["-H", "Content-Type: application/x-www-form-urlencoded"]
            for k, v in post_fields.items():
                cmd += ["--data-urlencode", f"{k}={v}"]
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[kisdi-re-kr-report] Empty response (attempt {attempt + 1}/3), "
                          f"retry in {wait}s...")
                    time.sleep(wait)
            except Exception as e:
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[kisdi-re-kr-report] curl error (attempt {attempt + 1}/3): {e}, "
                          f"retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[kisdi-re-kr-report] curl failed after 3 attempts: {e}")
        return None

    def _fetch_list(self, page: int) -> str | None:
        url = f"{_LIST_URL}?key={_KEY}&arrMasterId={_MASTER_ID}&pageIndex={page}"
        return self._curl("GET", url)

    def _fetch_detail(self, art_id: str) -> str | None:
        url = (f"{_DETAIL_URL}?key={_KEY}&masterId={_MASTER_ID}"
               f"&arrMasterId={_MASTER_ID}&artId={art_id}")
        return self._curl("POST", url, {"pageIndex": "1"})

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list(self, html: str) -> list:
        """Extract artId strings from goView('MASTERID', 'ARTID') calls."""
        return re.findall(r"goView\('\d+',\s*'(\d+)'\)", html)

    def _parse_detail(self, html: str, art_id: str) -> dict | None:
        try:
            soup = _make_soup(html)
        except Exception as e:
            print(f"[kisdi-re-kr-report] soup error for {art_id}: {e}")
            return None

        h4 = soup.find("h4", id="h4Title")
        if not h4:
            return None
        title = h4.get_text(strip=True)
        if not title:
            return None

        author = ""
        published_date = ""
        category = ""
        journal = ""
        volume = ""
        data_ul = soup.find("ul", id="data")
        if data_ul:
            for li in data_ul.find_all("li"):
                strong = li.find("strong")
                if not strong:
                    continue
                label = strong.get_text(strip=True)
                val = li.get_text(strip=True)[len(label):].strip()
                if label == "저자":
                    author = val
                elif label == "발행일":
                    published_date = val
                elif label == "분류정보":
                    category = val
                elif label == "게재지":
                    journal = val
                elif label == "권호":
                    volume = val

        keywords = []
        tag_div = soup.find("div", class_="tag")
        if tag_div:
            keywords = [sp.get_text(strip=True) for sp in tag_div.find_all("span")]
            keywords = [k.strip() for k in keywords if k.strip()]

        tab1 = soup.find(id="tab1")
        abstract = ""
        if tab1:
            abstract = tab1.get_text(separator=" ", strip=True)
            abstract = re.sub(r"^요약\s*", "", abstract).strip()

        tab2 = soup.find(id="tab2")
        toc = ""
        if tab2:
            toc = tab2.get_text(separator=" ", strip=True)
            toc = re.sub(r"^목차\s*", "", toc).strip()

        if abstract and toc:
            full_abstract = abstract + "\n\n[목차] " + toc
        elif toc:
            full_abstract = "[목차] " + toc
        else:
            full_abstract = abstract

        authors_list = [a.strip() for a in re.split(r"[,，]", author) if a.strip()] if author else []
        return {
            "title": title,
            "authors": json.dumps(authors_list, ensure_ascii=False),
            "abstract": full_abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "pdf_url": f"{_FILE_DOWN}?key={_KEY}&arrMasterId={_MASTER_ID}&id={art_id}",
            "metadata": json.dumps(
                {"journal": journal, "volume": volume, "toc": toc},
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        seen_urls = set()
        saved = 0
        page = 1
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - start_time > MAX_SECONDS:
                print(f"[kisdi-re-kr-report] 25-minute budget reached. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > MAX_PAGES:
                print(f"[kisdi-re-kr-report] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[kisdi-re-kr-report] page {page}: saved {saved}/{limit_str}")

            raw = self._fetch_list(page)
            if not raw:
                print(f"[kisdi-re-kr-report] Failed to fetch page {page}. Stopping.")
                break

            art_ids = self._parse_list(raw)
            if not art_ids:
                print(f"[kisdi-re-kr-report] No items on page {page}. Done.")
                break

            new_ids = [a for a in art_ids if a not in seen_urls]
            if not new_ids:
                print(f"[kisdi-re-kr-report] Page {page} all already seen. Done.")
                break

            for art_id in art_ids:
                if limit is not None and saved >= limit:
                    break

                if art_id in seen_urls:
                    continue
                seen_urls.add(art_id)

                try:
                    time.sleep(self._delay)

                    detail_html = self._fetch_detail(art_id)
                    if not detail_html:
                        print(f"[kisdi-re-kr-report] item {art_id} failed: no response")
                        continue

                    parsed = self._parse_detail(detail_html, art_id)
                    if not parsed:
                        print(f"[kisdi-re-kr-report] item {art_id} failed: parse returned None")
                        continue

                    abstract = parsed.get("abstract", "")
                    if len(abstract) < 100:
                        print(f"[kisdi-re-kr-report] item {art_id} skipped: "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    url = (f"https://www.kisdi.re.kr/report/view.do"
                           f"?key={_KEY}&masterId={_MASTER_ID}"
                           f"&arrMasterId={_MASTER_ID}&artId={art_id}")

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": art_id,
                        "title": parsed["title"],
                        "authors": parsed["authors"],
                        "abstract": abstract,
                        "category": parsed.get("category", ""),
                        "keywords": parsed.get("keywords", "[]"),
                        "published_date": parsed.get("published_date", ""),
                        "url": url,
                        "pdf_url": parsed.get("pdf_url", ""),
                        "doi": "",
                        "department": "",
                        "metadata": parsed.get("metadata", "{}"),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[kisdi-re-kr-report] Saved {saved}/{limit_str}: "
                          f"{parsed['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kisdi-re-kr-report] item {art_id} failed: {exc}")
                    continue

            page += 1

        print(f"[kisdi-re-kr-report] Done. Total saved: {saved}")
        return saved
